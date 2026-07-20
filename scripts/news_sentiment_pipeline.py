"""News-sentiment -> market-reaction pipeline (cycle 19, user request).

User: "a search tool and an llm embedded in pipeline that will look for keywords
like taxes, war, employment rate, inflation and make correlation between those and
the markets reaction to positive and negative results."

Architecture (three pluggable stages, each swappable for a real provider):

  1. NewsFetcher   -- the "search tool". Pulls news articles/tone for a theme +
                      time window. Backends:
                       * GDELT DOC 2.0 API (free, no key, deterministic tone +
                         theme tags) -- rate-limited from some IPs (429).
                       * Local constructed-calendar fallback (deterministic proxy
                         of scheduled macro events: NFP / FOMC / CPI).
                       * (Plug in any news API by subclassing NewsFetcher.)
  2. SentimentClassifier -- the "llm". Classifies a news item as positive/negative
                       (+ theme). Backends:
                       * LLM API (OpenAI-compatible chat completions; reads
                         NEWS_LLM_BASE_URL + NEWS_LLM_API_KEY + NEWS_LLM_MODEL
                         from env). NON-deterministic -- fine for LIVE signal
                         generation, WRONG for a reproducible backtest.
                       * Deterministic tone fallback (GDELT `tone` field, or a
                         keyword-polarity scorer) -- reproducible, the right tool
                         for a historical backtest.
  3. CorrelationEngine -- aligns each news event (timestamp + theme + sentiment)
                       to the forward H-hour gold/oil reaction, walk-forward 3
                       folds, trades in the direction of the sentiment sign, net
                       of 30 bps RT cost, through the project's DSR / Hansen-SPA /
                       CSCV-PBO gates.

HONESTY NOTES (read before trusting a positive):
- An LLM is the WRONG sentiment tool for a backtest (stochastic -> non-reproducible
  -> unstable DSR/PBO). It belongs in the LIVE pipeline. The backtest uses the
  deterministic tone backend.
- Keyword + theme + sentiment-direction selection on the SAME historical data is
  data-snooping. The DSR n_trials and the family PBO must absorb the search space.
  Per cycle 18, richer keyword/sentiment search -> MORE in-sample correlations
  that die OOS. Expect IS correlations; the gates decide if any survive.
- No external data is reachable from this environment right now (GDELT 429), so
  the default run uses the constructed-calendar fallback -- which reproduces the
  cycle-18 event-trade result THROUGH the new pipeline (proving the machinery),
  NOT a real news-sentiment edge. A real verdict needs GDELT reachable or a news
  API key (see CLI --source).

NO LIVE TRADING. Read-only research / backtest.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from statistics import NormalDist, mean, variance

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.history_manager import HISTORY_DIR
from scripts.quantum_loop import deflated_sharpe
from scripts.validation_audit import cscv_pbo, spa_pvalue

COST_RT = 0.0030
HOLD_HOURS = 4
N_MIN_OOS = 20

# Keyword -> GDELT theme code mapping (GDELT 2.0 GKG themes). Used to query GDELT
# and to tag the constructed-calendar fallback.
THEME_KEYWORDS: dict[str, dict] = {
    "inflation":   {"gdelt_theme": "ECON_INFLATION",      "keywords": ["inflation", "cpi", "price surge", "rate hike"]},
    "employment":  {"gdelt_theme": "ECON_EMPLOYMENT_UNEMP", "keywords": ["employment", "jobs", "unemployment", "payrolls", "nfp"]},
    "taxes":       {"gdelt_theme": "TAX_FNCACT",          "keywords": ["tax", "tariff", "fiscal"]},
    "war":         {"gdelt_theme": "ARMEDCONFLICT",       "keywords": ["war", "conflict", "military", "sanctions", "geopolitical"]},
}

FOMC_DATES = [
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12", "2024-07-31",
    "2024-09-18", "2024-11-07", "2024-12-18",
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30",
    "2025-09-17", "2025-10-29", "2025-12-17",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
]


# --------------------------------------------------------------------------- #
# 1. NewsFetcher (the "search tool")                                          #
# --------------------------------------------------------------------------- #
class NewsFetcher:
    """Base interface: return a list of news events {time, theme, text, tone?}."""

    def fetch(self, theme: str, start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
        raise NotImplementedError


class GDELTFetcher(NewsFetcher):
    """GDELT DOC 2.0 API. Free, no key. Has tone + theme tags. Rate-limited."""

    def __init__(self, max_retries: int = 3):
        self.max_retries = max_retries

    def _call(self, url: str) -> dict | None:
        for attempt in range(self.max_retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=25) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(2.0 * (attempt + 1))  # backoff (used in live runs)
                    continue
                return None
            except Exception:
                return None
        return None

    def fetch(self, theme: str, start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
        code = THEME_KEYWORDS[theme]["gdelt_theme"]
        q = urllib.parse.quote(f"theme:{code}")
        s = start.strftime("%Y%m%d%H%M%S")
        e = end.strftime("%Y%m%d%H%M%S")
        url = (f"https://api.gdeltproject.org/api/v2/doc/doc?query={q}"
               f"&mode=ArtList&maxrecords=250&format=json&startdatetime={s}&enddatetime={e}")
        data = self._call(url)
        if not data:
            return []
        out = []
        for a in data.get("articles", []):
            sd = a.get("seendate")  # GDELT format YYYYMMDDTHHMMSSZ
            try:
                t = pd.to_datetime(sd, format="%Y%m%dT%H%M%SZ", utc=True)
            except Exception:
                continue
            out.append({"time": t, "theme": theme, "text": a.get("title", ""),
                        "tone": None, "source": "gdelt"})
        return out


class LocalCalendarFetcher(NewsFetcher):
    """Deterministic fallback: scheduled macro events as 'news'."""

    def fetch(self, theme: str, start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
        out = []
        idx = pd.date_range(start, end, freq="h", tz="UTC")
        for t in idx:
            nfp = (t.weekday() == 4 and t.day <= 7 and 11 <= t.hour <= 14)
            fomc = any(t.strftime("%Y-%m-%d") == d and 17 <= t.hour <= 20 for d in FOMC_DATES)
            cpi = (t.weekday() in (1, 2) and 8 <= t.day <= 14 and 11 <= t.hour <= 14)
            if theme == "employment" and nfp:
                out.append({"time": t, "theme": theme, "text": "NFP release", "tone": None, "source": "nfp"})
            elif theme == "inflation" and (cpi or fomc):
                out.append({"time": t, "theme": theme, "text": "CPI/FOMC release", "tone": None, "source": "cpi_fomc"})
            elif theme == "war" and fomc:
                out.append({"time": t, "theme": theme, "text": "FOMC geopolitical context", "tone": None, "source": "fomc"})
        return out


class GDELTDumpFetcher(NewsFetcher):
    """Reads a parquet produced by scripts/gdelt_fetch.py (run on a non-429 IP).

    Columns expected: time(UTC), theme, title, [tone], source.
    This is how real GDELT news enters the backtest when the quant server itself
    is GDELT-rate-limited (429).
    """

    def __init__(self, path: str | Path):
        self.df = pd.read_parquet(path)
        if "time" in self.df.columns:
            self.df["time"] = pd.to_datetime(self.df["time"], utc=True)
        self.df = self.df.sort_values("time")

    def fetch(self, theme: str, start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
        sub = self.df[(self.df["theme"] == theme) & (self.df["time"] >= start) & (self.df["time"] < end)]
        out = []
        for _, r in sub.iterrows():
            out.append({"time": r["time"], "theme": theme,
                        "text": r.get("title", "") or r.get("text", ""),
                        "tone": r.get("tone") if "tone" in r else None,
                        "source": "gdelt-dump"})
        return out


class RSSNewsFetcher(NewsFetcher):
    """Free financial RSS feeds — no API key, no GDELT 429 problem.

    Verified working 2026-06-29 (returns real <item> titles): MarketWatch top
    stories, Investing.com Forex news (news_1), Investing.com Stock Market news
    (news_25). CNBC forex / Forex Factory / Investing news_28|45 were 403/404
    and are NOT in the default list.

    HONESTY: RSS carries only the most recent ~20-50 items per feed (days to
    weeks, not years), so this is a LIVE/recent-news source, not a deep
    historical backtest corpus. For a long backtest use --source gdelt-dump
    (a parquet fetched from a non-429 IP). For live signal context, RSS is the
    right free tool. Items with no parseable <pubDate> are dropped (can't align
    them to price without a timestamp).
    """

    DEFAULT_FEEDS = [
        "https://feeds.marketwatch.com/marketwatch/topstories/",
        "https://www.investing.com/rss/news_1.rss",   # Forex News
        "https://www.investing.com/rss/news_25.rss",  # Stock Market News
    ]

    def __init__(self, feeds: list[str] | None = None, timeout: int = 15):
        self.feeds = feeds or self.DEFAULT_FEEDS
        self.timeout = timeout
        self._cache: list[dict] | None = None  # raw items cached across theme calls

    def _fetch_all(self) -> list[dict]:
        if self._cache is not None:
            return self._cache
        import xml.etree.ElementTree as ET
        from email.utils import parsedate_to_datetime
        items: list[dict] = []
        for url in self.feeds:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    xml = r.read()
                root = ET.fromstring(xml)
                for it in root.iter("item"):
                    title_el = it.find("title")
                    pub_el = it.find("pubDate")
                    if title_el is None or not (title_el.text or "").strip():
                        continue
                    title = title_el.text.strip()
                    t = None
                    if pub_el is not None and pub_el.text:
                        try:
                            dt = parsedate_to_datetime(pub_el.text)
                            t = pd.Timestamp(dt)
                            t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
                        except Exception:
                            t = None
                    items.append({"title": title, "time": t, "source_url": url})
            except Exception:
                continue  # skip unreachable feed, keep going
        self._cache = items
        return items

    def fetch(self, theme: str, start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
        kws = [k.lower() for k in THEME_KEYWORDS[theme]["keywords"]]
        out: list[dict] = []
        for it in self._fetch_all():
            low = it["title"].lower()
            if not any(k in low for k in kws):
                continue
            t = it["time"]
            if t is None:
                continue
            try:
                ts = pd.Timestamp(t)
                ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
            except Exception:
                continue
            if start <= ts < end:
                out.append({"time": ts, "theme": theme, "text": it["title"],
                            "tone": None, "source": "rss"})
        return out


# --------------------------------------------------------------------------- #
# 2. SentimentClassifier (the "llm")                                          #
# --------------------------------------------------------------------------- #
class SentimentClassifier:
    """Base interface: return a sentiment in [-1, 1] for a news item."""

    def classify(self, item: dict) -> float:
        raise NotImplementedError


class LLMClassifier(SentimentClassifier):
    """OpenAI-compatible chat completion. Reads creds from env. NON-deterministic.

    Intended for LIVE signal generation, not reproducible backtests.
    """

    def __init__(self):
        self.base = os.environ.get("NEWS_LLM_BASE_URL", "https://api.openai.com/v1")
        self.key = os.environ.get("NEWS_LLM_API_KEY")
        self.model = os.environ.get("NEWS_LLM_MODEL", "gpt-4o-mini")

    def is_available(self) -> bool:
        return bool(self.key)

    def classify(self, item: dict) -> float:
        if not self.key:
            return 0.0
        prompt = (f"Classify the financial news sentiment for GOLD impact on a "
                  f"scale -1 (bearish) to +1 (bullish). Theme={item['theme']}. "
                  f"Headline: '{item['text']}'. Reply with one number only.")
        body = json.dumps({"model": self.model, "messages": [
            {"role": "system", "content": "You output a single float in [-1,1]."},
            {"role": "user", "content": prompt}],
            "temperature": 0.0}).encode()
        req = urllib.request.Request(f"{self.base}/chat/completions",
            data=body, headers={"Authorization": f"Bearer {self.key}",
                                "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                txt = json.load(r)["choices"][0]["message"]["content"].strip()
            return max(-1.0, min(1.0, float(txt)))
        except Exception:
            return 0.0


class OllamaClassifier(SentimentClassifier):
    """LLM via Ollama (http://127.0.0.1:11434). No OpenAI-style API key needed.

    Default model kimi-k2.5:cloud — served THROUGH the local Ollama client but
    hosted on ollama.com (already authenticated on this box). Verified 2026-06-29:
    ~7s/call, correct polarity, clean numeric output. Cloud models are
    non-deterministic -> fine for LIVE news-signal generation, NOT for a
    reproducible backtest. For a reproducible backtest, install a fast LOCAL
    model and pass --ollama-model e.g. qwen3:1.7b / llama3.2:1b (qwen3:4b on this
    CPU is ~120s/call — too slow). qwen3 models emit  reasoning that is
    stripped before the score is parsed.
    """

    def __init__(self, model: str | None = None, base: str | None = None, timeout: int = 60):
        self.base = base or os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
        self.model = model or os.environ.get("OLLAMA_MODEL", "kimi-k2.5:cloud")
        self.timeout = timeout

    def is_available(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.base}/api/tags")
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.load(r)
            names = [m.get("name", "") for m in data.get("models", [])]
            prefix = self.model.split(":")[0]
            return any(n == self.model or n.startswith(prefix + ":") or n == prefix for n in names)
        except Exception:
            return False

    def classify(self, item: dict) -> float:
        import re as _re
        prompt = (f"Classify the financial-news sentiment for GOLD (XAUUSD) impact "
                  f"on a scale -1.0 (bearish for gold) to +1.0 (bullish for gold). "
                  f"Theme={item.get('theme')}. Headline: '{item.get('text', '')}'. "
                  f"Reply with ONE number in [-1.0, 1.0] and nothing else.")
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You output a single float in [-1.0, 1.0]. No words, no reasoning."},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": {"temperature": 0.0},
        }).encode()
        req = urllib.request.Request(f"{self.base}/api/chat", data=body,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                txt = json.load(r).get("message", {}).get("content", "").strip()
            # qwen3 emits <think>...</think> reasoning before the answer; drop it,
            # then take the last number in the remaining text (the actual score).
            txt = _re.sub(r"<think>.*?</think>", "", txt, flags=_re.S).strip()
            nums = _re.findall(r"-?\d+(?:\.\d+)?", txt)
            if not nums:
                return 0.0
            return max(-1.0, min(1.0, float(nums[-1])))
        except Exception:
            return 0.0


class DeterministicToneClassifier(SentimentClassifier):
    """Reproducible fallback: GDELT tone if present, else keyword polarity."""

    POS = ["surge", "jump", "rise", "strong", "beat", "robust", "rally", "gain", "hike"]
    NEG = ["fall", "drop", "plunge", "weak", "miss", "cut", "slump", "loss", "fear", "crisis"]

    def classify(self, item: dict) -> float:
        if item.get("tone") is not None:
            try:
                # GDELT tone: negative score = negative sentiment
                return -1.0 * float(item["tone"]) / 10.0
            except Exception:
                pass
        text = (item.get("text") or "").lower()
        score = sum(1 for w in self.POS if w in text) - sum(1 for w in self.NEG if w in text)
        return max(-1.0, min(1.0, float(score)))


# --------------------------------------------------------------------------- #
# 3. Correlation engine + gates                                               #
# --------------------------------------------------------------------------- #
def _load_h1(symbol: str) -> pd.DataFrame:
    df = pd.read_parquet(HISTORY_DIR / f"{symbol}_M5.parquet")
    if "time" not in df.columns:
        df = df.rename(columns={df.columns[0]: "time"})
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time").sort_index()
    h1 = df[["close"]].resample("1h", label="left", closed="left").last().dropna()
    return h1


def _ci95_lo(rs: list[float]) -> float:
    n = len(rs)
    if n < 2:
        return 0.0
    m = mean(rs); v = variance(rs, m)
    return m - NormalDist().inv_cdf(0.975) * math.sqrt(v / n)


def _folds(idx: pd.DatetimeIndex, k: int):
    start, end = idx.min(), idx.max()
    span = (end - start).total_seconds()
    return [(start, start + pd.Timedelta(seconds=span * (i + 1) / (k + 1)),
             start + pd.Timedelta(seconds=span * (i + 1) / (k + 1)),
             start + pd.Timedelta(seconds=span * (i + 2) / (k + 1)) if i < k - 1 else end)
            for i in range(k)]


def main() -> int:
    ap = argparse.ArgumentParser(description="News-sentiment -> market-reaction pipeline")
    ap.add_argument("--target", default="XAUUSDm")
    ap.add_argument("--themes", nargs="*", default=list(THEME_KEYWORDS))
    ap.add_argument("--source", choices=["gdelt", "gdelt-dump", "rss", "local", "auto"], default="auto",
                    help="auto = try GDELT, then RSS, then local calendar; rss = free financial RSS feeds (MarketWatch + Investing)")
    ap.add_argument("--dump-path", type=Path, default=ROOT / "data" / "gdelt_news.parquet",
                    help="Parquet file to read when --source gdelt-dump")
    ap.add_argument("--sentiment", choices=["llm", "ollama", "deterministic", "auto"], default="auto",
                    help="auto = Ollama (local, free) if available, else cloud LLM if key set, else deterministic; "
                         "ollama = local LLM at temperature 0 (reproducible, OK for backtest AND live)")
    ap.add_argument("--ollama-model", default=os.environ.get("OLLAMA_MODEL", "kimi-k2.5:cloud"),
                    help="Ollama model for --sentiment ollama (default kimi-k2.5:cloud = fast cloud; "
                         "use qwen3:1.7b / llama3.2:1b for a reproducible local backtest)")
    ap.add_argument("--mode", choices=["sentiment", "presence"], default="sentiment",
                    help="sentiment = trade in IS sentiment-return correlation direction "
                         "(needs VARYING sentiment -> real headlines); "
                         "presence = trade each event hour in the IS mean-forward-return "
                         "direction (event-timing signal; works on the calendar fallback)")
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--cost-rt", type=float, default=COST_RT)
    ap.add_argument("--out", type=Path, default=ROOT / "news_sentiment_report.json")
    args = ap.parse_args()

    # --- load price ---
    h1 = _load_h1(args.target)
    fwd = h1["close"].shift(-HOLD_HOURS) / h1["close"] - 1.0
    start, end = h1.index.min(), h1.index.max()
    print(f"Target {args.target}: H1 window {start} -> {end} ({len(h1)} bars)")

    # --- choose fetcher ---
    fetcher: NewsFetcher
    used_source = args.source
    if args.source == "gdelt-dump":
        if not args.dump_path.exists():
            print(f"GDELT dump not found: {args.dump_path}")
            return 1
        fetcher = GDELTDumpFetcher(args.dump_path)
        used_source = "gdelt-dump"
        print(f"News source: GDELT dump ({args.dump_path})")
    elif args.source == "rss":
        fetcher = RSSNewsFetcher()
        used_source = "rss"
        print("News source: RSS (MarketWatch + Investing.com; free, no key, real recent headlines)")
    elif args.source in ("gdelt", "auto"):
        gf = GDELTFetcher()
        probe = gf.fetch(args.themes[0], start, end)
        if probe:
            fetcher = gf
            used_source = "gdelt"
            print(f"News source: GDELT (probe returned {len(probe)} {args.themes[0]} articles)")
        elif args.source == "auto":
            # GDELT unreachable (429) -> try free RSS before the constructed-calendar proxy
            fetcher = RSSNewsFetcher()
            used_source = "rss-fallback"
            print("News source: GDELT unreachable (429/empty) -> RSS fallback (real recent headlines)")
        else:
            fetcher = LocalCalendarFetcher()
            used_source = "local-fallback"
            print("News source: GDELT unreachable (429/empty) -> local constructed-calendar fallback")
    else:
        fetcher = LocalCalendarFetcher()
        used_source = "local"
        print("News source: local constructed-calendar (forced)")

    # --- choose sentiment classifier ---
    llm = LLMClassifier()
    ollama = OllamaClassifier(model=args.ollama_model)
    if args.sentiment == "llm" and not llm.is_available():
        print("WARNING: --sentiment llm but NEWS_LLM_API_KEY unset -> deterministic fallback")
    if args.sentiment == "ollama" and not ollama.is_available():
        print(f"WARNING: --sentiment ollama but {ollama.model} not reachable at {ollama.base} "
              f"-> deterministic fallback (is `ollama serve` running?)")
    clf: SentimentClassifier
    used_sent = args.sentiment
    if args.sentiment == "auto":
        if ollama.is_available():
            clf = ollama; used_sent = "ollama"
        elif llm.is_available():
            clf = llm; used_sent = "llm"
        else:
            clf = DeterministicToneClassifier(); used_sent = "deterministic"
    elif args.sentiment == "ollama" and ollama.is_available():
        clf = ollama; used_sent = "ollama"
    elif args.sentiment == "llm" and llm.is_available():
        clf = llm; used_sent = "llm"
    else:
        clf = DeterministicToneClassifier(); used_sent = "deterministic"
    extra = ""
    if used_sent == "ollama":
        extra = f" (model={ollama.model})"
    elif used_sent == "llm":
        extra = f" (model={llm.model})"
    print(f"Sentiment classifier: {used_sent}{extra}")

    # --- collect + classify news events per theme ---
    events: list[dict] = []
    for theme in args.themes:
        evs = fetcher.fetch(theme, start, end)
        for ev in evs:
            ev["sentiment"] = clf.classify(ev)
            events.append(ev)
        print(f"  theme={theme:11s} events={len(evs)}")
    if not events:
        print("No news events. Exiting.")
        return 1

    # --- align each event to forward H-hour return; trade in sentiment direction ---
    # walk-forward: learn the sentiment->direction mapping sign on TRAIN (does
    # positive sentiment predict positive forward return?), apply on TEST.
    folds = _folds(h1.index, args.folds)
    theme_oos: dict[str, list[float]] = defaultdict(list)
    theme_fold_pos: dict[str, list[bool]] = defaultdict(list)
    all_ev_count = len(events)

    for tr_s, tr_e, te_s, te_e in folds:
        tr_ev = [e for e in events if tr_s <= e["time"] < tr_e]
        te_ev = [e for e in events if te_s <= e["time"] < te_e]
        # per-theme IS signal direction
        is_sign: dict[str, int] = {}
        for theme in args.themes:
            trs = [e for e in tr_ev if e["theme"] == theme]
            if args.mode == "sentiment":
                trs = [e for e in trs if abs(e["sentiment"]) > 1e-6]
            if len(trs) < 5:
                continue
            rs = []
            for e in trs:
                if e["time"] in fwd.index:
                    rs.append((e["sentiment"], float(fwd.loc[e["time"]])))
            if len(rs) < 5:
                continue
            if args.mode == "sentiment":
                # correlate sentiment with forward return
                mx = mean([a for a, _ in rs]); my = mean([b for _, b in rs])
                cov = sum((a - mx) * (b - my) for a, b in rs) / len(rs)
                is_sign[theme] = 1 if cov > 0 else -1
            else:  # presence: IS mean forward return on event hours
                my = mean([b for _, b in rs])
                if abs(my) < 1e-9:
                    continue
                is_sign[theme] = 1 if my > 0 else -1
        # OOS: trade each test event in the IS direction
        for theme in args.themes:
            sign = is_sign.get(theme)
            if sign is None:
                continue
            trades = []
            for e in te_ev:
                if e["theme"] != theme:
                    continue
                if e["time"] not in fwd.index:
                    continue
                if args.mode == "sentiment":
                    if abs(e["sentiment"]) < 1e-6:
                        continue
                    direction = sign * (1 if e["sentiment"] > 0 else -1)
                else:  # presence: every event hour, IS direction
                    direction = sign
                net = direction * float(fwd.loc[e["time"]]) - args.cost_rt
                trades.append(net)
            if trades:
                theme_oos[theme].extend(trades)
                theme_fold_pos[theme].append(mean(trades) > 0)

    # --- per-theme stats + gates ---
    def _sharpe(rs):
        if len(rs) < 2:
            return 0.0
        m = mean(rs); sd = math.sqrt(variance(rs, m))
        return m / sd if sd > 0 else 0.0
    themes_traded = [t for t, v in theme_oos.items() if len(v) >= N_MIN_OOS]
    sr_var = variance([_sharpe(theme_oos[t]) for t in themes_traded]) if len(themes_traded) > 1 else 0.0
    n_trials = max(len(args.themes), 1)

    rows = []
    for t in themes_traded:
        rs = theme_oos[t]
        rows.append({
            "theme": t, "n": len(rs), "net_mean": mean(rs), "ci95_lo": _ci95_lo(rs),
            "sharpe": _sharpe(rs),
            "dsr": deflated_sharpe(rs, n_trials=n_trials, sr_var_across_trials=sr_var),
            "folds_pos": f"{sum(theme_fold_pos[t])}/{len(theme_fold_pos[t])}",
        })
    rows.sort(key=lambda r: r["dsr"], reverse=True)

    print("\n=== News-sentiment -> market-reaction (OOS, net of cost) ===")
    print(f"{'theme':12s} {'n':>5s} {'mean':>9s} {'CIlo':>9s} {'SR':>6s} {'DSR':>6s} folds")
    for r in rows:
        print(f"{r['theme']:12s} {r['n']:5d} {r['net_mean']:+.5f} {r['ci95_lo']:+.5f} "
              f"{r['sharpe']:+.3f} {r['dsr']:.3f} {r['folds_pos']}")

    matrix = [theme_oos[t] for t in themes_traded]
    pbo = cscv_pbo(matrix, n_blocks=8) if len(matrix) >= 2 else {"pbo": None}
    spa = spa_pvalue(matrix, n_boot=2000) if len(matrix) >= 2 else {"p_consistent": None}

    winners = [r for r in rows if r["dsr"] >= 0.95 and r["ci95_lo"] > 0
               and int(r["folds_pos"].split("/")[0]) >= math.ceil(args.folds / 2)]
    report = {
        "target": args.target, "window": [str(start), str(end)],
        "mode": args.mode, "news_source": used_source, "sentiment_backend": used_sent,
        "themes_scanned": args.themes, "events_total": all_ev_count,
        "themes_traded_oos": len(themes_traded), "n_trials": n_trials,
        "cost_rt": args.cost_rt, "hold_hours": HOLD_HOURS,
        "family_pbo": pbo.get("pbo"), "family_spa_p": spa.get("p_consistent"),
        "winners": winners, "rows": rows,
        "caveats": [
            "LLM sentiment is non-deterministic -> wrong for backtests; deterministic backend used for reproducibility",
            "keyword/theme/sentiment-direction selection on same data = data-snooping; DSR n_trials + PBO absorb it",
            f"news_source={used_source}: if not 'gdelt', this is a constructed-calendar proxy, NOT real news",
            "a real news-sentiment verdict needs GDELT reachable (non-rate-limited IP) or a news API key",
        ],
    }
    args.out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nFamily PBO={pbo.get('pbo')}  SPA p={spa.get('p_consistent')}")
    print(f"ORGANIC WINNERS (DSR>=0.95, CI95 lo>0, majority folds): {len(winners)}")
    if winners:
        for w in winners:
            print(f"  WINNER: theme={w['theme']} n={w['n']} mean={w['net_mean']:+.5f} DSR={w['dsr']:.3f}")
    else:
        print("  (none) -- no news-sentiment theme has a positive OOS edge net of cost")
        print("          that survives multiple-testing.")
    print(f"Report written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
