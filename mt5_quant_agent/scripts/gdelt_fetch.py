"""Standalone GDELT news fetcher -- run this on YOUR machine (non-429-blocked IP).

The MT5 quant server's egress IP is rate-limited by GDELT (HTTP 429 on every
request), so the real-news backtest cannot pull from there. This script is
self-contained: run it anywhere with `pip install pandas pyarrow` (urllib is stdlib,
no requests/anthropic deps). It pulls GDELT DOC 2.0 article lists for the four
keyword themes (inflation / employment / taxes / war) over the backtest window,
chunks politely (monthly, with a delay to stay under GDELT's rate limit), and writes
a single parquet the news-sentiment pipeline can consume.

Usage (on your machine, from the repo root):
    pip install pandas pyarrow
    python scripts/gdelt_fetch.py --start 2025-01-28 --end 2026-06-26 \
        --out data/gdelt_news.parquet

Then copy data/gdelt_news.parquet back onto the quant server at the same path and
run:
    python scripts/news_sentiment_pipeline.py --source gdelt-dump \
        --dump-path data/gdelt_news.parquet --sentiment deterministic --mode sentiment

Output schema (parquet):
    time        datetime64[ns, UTC]   article seen time
    theme       str                   inflation | employment | taxes | war
    title       str                   article headline (the LLM/keyword input)
    domain      str                   source domain
    url         str                   article url
    language    str                   language code
    source      str                   "gdelt"

Notes / honesty:
- GDELT DOC `mode=ArtList` returns up to `maxrecords` (capped at 250) articles per
  request, NEWEST FIRST. Monthly chunking + maxrecords=250 gives a capped sample per
  month/theme, not an exhaustive census. For a backtest that is fine (thousands of
  events), but it IS a sample -- record this caveat in any writeup.
- ArtList does NOT return per-article tone. Sentiment is computed downstream by the
  pipeline's keyword-polarity scorer (reproducible) or LLM (live only). If you want
  GDELT GKG tone per record, that requires the raw GKG CSV dumps (much larger); out
  of scope here.
- The query uses `theme:<GKG_THEME>` which maps each keyword to its GDELT 2.0 theme
  code. Add the keyword terms too with `OR term` if you want broader recall.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

# Mirror of THEME_KEYWORDS in news_sentiment_pipeline.py (kept local so this script
# is standalone -- if you edit themes, edit both).
THEME_GDELT = {
    "inflation": "ECON_INFLATION",
    "employment": "ECON_EMPLOYMENT_UNEMP",
    "taxes": "TAX_FNCACT",
    "war": "ARMEDCONFLICT",
}

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


def fetch_chunk(theme: str, start: str, end: str, maxrecords: int) -> list[dict]:
    code = THEME_GDELT[theme]
    q = urllib.parse.quote(f"theme:{code}")
    url = (f"{GDELT_URL}?query={q}&mode=ArtList&maxrecords={maxrecords}"
           f"&format=json&sort=DateDesc&startdatetime={start}&enddatetime={end}")
    req = urllib.request.Request(url, headers={"User-Agent": "mt5-quant-research/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except Exception as e:
        print(f"  ! {theme} {start}->{end}: request failed ({e})")
        return []
    out = []
    for a in data.get("articles", []):
        sd = a.get("seendate")  # YYYYMMDDTHHMMSSZ
        try:
            t = pd.to_datetime(sd, format="%Y%m%dT%H%M%SZ", utc=True)
        except Exception:
            continue
        out.append({"time": t, "theme": theme, "title": a.get("title", ""),
                    "domain": a.get("domain", ""), "url": a.get("url", ""),
                    "language": a.get("language", ""), "source": "gdelt"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Standalone GDELT news fetcher (run on a non-rate-limited IP)")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--themes", nargs="*", default=list(THEME_GDELT))
    ap.add_argument("--out", type=Path, default=Path("data/gdelt_news.parquet"))
    ap.add_argument("--maxrecords", type=int, default=250,
                    help="GDELT caps ArtList at 250; this is a per-chunk sample cap")
    ap.add_argument("--chunk", choices=["month", "week"], default="month",
                    help="time chunk size (monthly = 4 themes x N months requests)")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds between requests (stay under GDELT's rate limit)")
    args = ap.parse_args()

    start = pd.Timestamp(args.start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC")
    chunks = []
    freq = "MS" if args.chunk == "month" else "W-MON"
    bounds = list(pd.date_range(start, end, freq=freq)) + [end]
    for i in range(len(bounds) - 1):
        cs, ce = bounds[i], min(bounds[i + 1], end)
        if cs >= ce:
            continue
        chunks.append((cs.strftime("%Y%m%d%H%M%S"), ce.strftime("%Y%m%d%H%M%S")))

    print(f"Fetching GDELT {args.themes} {start.date()}->{end.date()} "
          f"({len(chunks)} {args.chunk} chunks x {len(args.themes)} themes, "
          f"maxrecords={args.maxrecords}, delay={args.delay}s)")

    all_rows: list[dict] = []
    for theme in args.themes:
        n_theme = 0
        for cs, ce in chunks:
            rows = fetch_chunk(theme, cs, ce, args.maxrecords)
            all_rows.extend(rows)
            n_theme += len(rows)
            print(f"  {theme:11s} {cs[:8]}..{ce[:8]}: +{len(rows)} (theme total {n_theme})")
            time.sleep(args.delay)
        print(f"{theme:11s} DONE: {n_theme} articles")

    if not all_rows:
        print("No articles fetched. Check connectivity / try a smaller window.")
        return 1

    df = pd.DataFrame(all_rows).drop_duplicates(subset=["time", "url"]).sort_values("time")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(args.out, index=False)
    print(f"\nWrote {len(df)} unique articles to {args.out}")
    print("Per-theme counts:")
    print(df.groupby("theme").size().to_string())
    print(f"\nNext: copy {args.out} to the quant server and run:")
    print(f"  python scripts/news_sentiment_pipeline.py --source gdelt-dump "
          f"--dump-path {args.out} --sentiment deterministic --mode sentiment")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())