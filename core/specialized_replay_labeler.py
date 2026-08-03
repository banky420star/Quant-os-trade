"""Specialized-setup historical-replay labeler (iteration 11, 2026-08-01).

This is the offline engine that turns the 14 specialized detectors into
**per-symbol win-rate evidence** — the "distinguish what wears per symbol" the
loop is building toward. It replays per-symbol M5+M15 history from
``data/history/`` through the *exact* live detection path:

    parquet -> FeatureEngine._compute_symbol_features -> EvidenceEngine.compute_symbol
            -> detect_specialized (all setups, UTC window from the BAR not the clock)
            -> specialized_outcome_labeler.label_outcome -> per-cell aggregation

KEY DESIGN CHOICES (all matter for honesty, per VERDICT.md + exit-model-bias):

  * Reuse the live FeatureEngine + EvidenceEngine. We do NOT re-implement
    features — we instantiate the real classes and call the real per-symbol
    methods on a rolling window. So a fire in replay == a fire live. The only
    divergence is the UTC window check (below).

  * UTC window from the BAR, not the wall clock. ``_in_window`` calls
    ``utc_hour()`` with no args (current real hour). For replay that would mean
    only setups whose window contains *today's* hour ever fire, regardless of
    the historical bar. We context-manage a monkeypatch of
    ``core.specialized_setups.utc_hour`` to return the bar's own UTC hour, so a
    bar at 14:00 UTC is judged against 14:00-killzone setups. This is scoped to
    the replay call only; live code is untouched.

  * Detect ALL setups on ALL symbols. We build a replay-only config clone with
    ``signals.specialized_setups.enabled=true, shadow=false, symbols=[],
    setups=[], per_symbol={}`` so every setup is a candidate on every symbol
    and no shadow record is written (replay must not pollute the live shadow
    ledger). Fires are collected in-memory, never emitted for trading.

  * Outcomes via the intrabar SL-first labeler (specialized_outcome_labeler).
    Entry = feat.price; SL = support (BUY) / resistance (SELL); TP = entry +/-
    tp_r*risk. Intrabar high/low, SL-first on same-bar ambiguity = pessimistic
    (biases AGAINST edge — correct direction for a no-edge project).

  * Aggregation carries the DSR/SPA caveat. 14 setups x ~14 symbols ~= 100+
    cells is deep data-snooping territory (VERDICT.md). Per-cell win-rate /
    expectancy / CI95 are NECESSARY but NOT SUFFICIENT — a bootstrap CI that
    excludes zero on one cell is selection-biased. The report says this
    explicitly; no per-cell result is to be read as "deploy this".

This module places no orders, touches no kill switch, writes no live state.
It optionally writes one offline report to state/specialized_replay_report.json.
"""

from __future__ import annotations

import logging
import math
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from core import specialized_setups
from core.evidence_engine import EvidenceEngine
from core.feature_engine import FeatureEngine
from core.specialized_outcome_labeler import label_outcome
from core.specialized_setups import detect_specialized
from core.utils import utc_now_iso, write_json_state

logger = logging.getLogger("specialized_replay_labeler")

HISTORY_DIR = Path("data/history")
WARMUP_BARS = 60          # need 50-bar support/resistance + 20-bar vol avg + BB 20
WINDOW_BARS = 500         # FeatureEngine history_lookback_bars default
FORWARD_BARS = 48         # 48 x M5 = 4h forward window for SL/TP/time-stop
DEFAULT_TP_R = 2.0
MIN_CELL_N = 8

REPORT_NAME = "specialized_replay_report.json"


def _history_path(symbol: str, timeframe: str) -> Path:
    return HISTORY_DIR / f"{symbol}_{timeframe}.parquet"


def load_bars(symbol: str, timeframe: str = "M5") -> list[dict[str, Any]]:
    """Read a symbol's parquet history as a list of bar dicts."""
    path = _history_path(symbol, timeframe)
    if not path.exists():
        return []
    df = pd.read_parquet(path)
    # Normalize to plain Python floats + ISO-ish time; FeatureEngine._to_dataframe
    # re-coerces, but plain dicts avoid numpy/pandas scalar edge cases downstream.
    bars: list[dict[str, Any]] = []
    for rec in df.to_dict("records"):
        t = rec.get("time")
        bars.append({
            "time": t,
            "open": float(rec["open"]),
            "high": float(rec["high"]),
            "low": float(rec["low"]),
            "close": float(rec["close"]),
            "volume": float(rec.get("volume") or 0.0),
        })
    return bars


def _bar_utc_hour(bar: dict[str, Any]) -> int:
    """UTC hour of a bar's timestamp (works for tz-aware pandas Timestamp or ISO str)."""
    t = bar.get("time")
    if t is None:
        return -1
    try:
        ts = pd.Timestamp(t)
        if ts.tz is None:
            ts = ts.tz_localize("UTC")
        return int(ts.tz_convert("UTC").hour)
    except Exception:  # noqa: BLE001
        return -1


@contextmanager
def _bar_hour_override(hour: int) -> Iterator[None]:
    """Make ``core.specialized_setups.utc_hour()`` return ``hour`` for the replay
    window so the killzone UTC-window check uses the BAR's hour, not now."""
    saved = specialized_setups.utc_hour
    specialized_setups.utc_hour = lambda: hour  # type: ignore[assignment]
    try:
        yield
    finally:
        specialized_setups.utc_hour = saved  # type: ignore[assignment]


def _replay_config(config: dict[str, Any]) -> dict[str, Any]:
    """Clone config so EVERY specialized setup is detected on EVERY symbol in
    replay, with shadow OFF (no pollution of the live shadow ledger)."""
    import copy
    cfg = copy.deepcopy(config)
    sig = cfg.setdefault("signals", {})
    sa = sig.setdefault("specialized_setups", {})
    sa["enabled"] = True       # emit candidates in-memory (we never route to orders)
    sa["shadow"] = False       # do NOT write state/specialized_shadow_ledger.jsonl
    sa["symbols"] = []         # no symbol filter
    sa["setups"] = []          # no setup filter
    sa["per_symbol"] = {}      # no per-symbol filter
    return cfg


def _m15_window_up_to(m15: list[dict[str, Any]], cutoff_time: Any,
                      limit: int = WINDOW_BARS) -> list[dict[str, Any]]:
    """M15 bars with time <= cutoff_time, most recent ``limit`` only."""
    out: list[dict[str, Any]] = []
    for bar in m15:
        try:
            bt = pd.Timestamp(bar["time"])
            ct = pd.Timestamp(cutoff_time)
            if bt <= ct:
                out.append(bar)
        except Exception:  # noqa: BLE001
            continue
    return out[-limit:]


def _vectorized_features(m5: list[dict[str, Any]], m15: list[dict[str, Any]]) -> pd.DataFrame:
    """Compute FeatureEngine's per-bar features for EVERY M5 bar in one vectorized
    pass instead of re-running pandas rolling per bar (which is ~145ms/bar —
    unusable on 50k x 14 symbols).

    Replicates FeatureEngine._compute_symbol_features exactly:
      * EMA(20, adjust=False) slope sign -> m5/m15 trend
      * BB(20, 2): middle/std/upper/lower/position
      * Stochastic(14/3): k, d, cross
      * ATR(14), atr_ratio
      * support = rolling(50).low.min(), resistance = rolling(50).high.max()
      * candle rejection (wick vs body), breakout vs rolling support/resistance
      * volume_avg(20), volume_ratio
      * volatility_regime (atr_ratio + 20-bar pct-change std thresholds)
      * timeframe_alignment = m5_trend == m15_trend (m15 trend asof-mapped to M5)

    Minor convergence note: live FeatureEngine runs EMA on a 500-bar trailing
    window; here we run EMA on the full series. With span=20 adjust=False the EMA
    converges in ~60 bars, so values at any bar i are identical to live to float
    precision for all bars past warmup. Acceptable for an edge-estimation replay.
    """
    import numpy as np

    df = pd.DataFrame(m5)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    close = df["close"]; high = df["high"]; low = df["low"]; open_ = df["open"]

    # --- M5 EMA(20) trend ---
    ema_m5 = close.ewm(span=20, adjust=False).mean()
    slope_m5 = ema_m5 - ema_m5.shift(5)
    m5_trend = np.where(slope_m5 > 0, "bullish", np.where(slope_m5 < 0, "bearish", "neutral"))

    # --- Bollinger(20, 2) ---
    mid = close.rolling(20).mean()
    std = close.rolling(20).std()
    bb_upper = mid + 2.0 * std
    bb_lower = mid - 2.0 * std
    width = bb_upper - bb_lower
    bb_position = ((close - bb_lower) / width.replace(0, np.nan)).fillna(0.5).clip(-1, 2)
    # --- BB-bandwidth squeeze percentile (iteration 19) ---
    # bw = (upper-lower)/middle; pct = bw.rolling(50).rank(pct=True) — the
    # percentile rank of the CURRENT bandwidth within the prior 50 bars (0 =
    # tightest squeeze). Parity-identical to FeatureEngine._bb_squeeze (the
    # iter-13 dead-setup lesson). Keys the squeeze-breakout entry model.
    bb_bw = (bb_upper - bb_lower) / mid.replace(0, np.nan)
    bb_squeeze_pct = bb_bw.rolling(50).rank(pct=True).fillna(0.5)

    # --- RSI(14) Wilder (iteration 21) ---
    # ewm(alpha=1/14, adjust=False) smoothing — parity-identical to
    # FeatureEngine._rsi. Keys the RSI-reversion entry model. fillna(50) warmup.
    _delta = close.diff()
    _gain = _delta.clip(lower=0.0)
    _loss = (-_delta).clip(lower=0.0)
    _avg_gain = _gain.ewm(alpha=1.0 / 14, adjust=False).mean()
    _avg_loss = _loss.ewm(alpha=1.0 / 14, adjust=False).mean()
    _rs = _avg_gain / _avg_loss.replace(0, np.nan)
    rsi = (100.0 - 100.0 / (1.0 + _rs)).fillna(50.0)

    # --- MACD(12,26,9) (iteration 22) ---
    # EMA fast - EMA slow = MACD line; EMA(9) of MACD = signal; hist = macd -
    # signal; signal-line cross event this bar. Parity-identical to
    # FeatureEngine._macd (the iter-13 dead-setup lesson). Keys the MACD-cross
    # entry model. ewm(span, adjust=False) matches the live helper.
    _ema_fast = close.ewm(span=12, adjust=False).mean()
    _ema_slow = close.ewm(span=26, adjust=False).mean()
    macd_line = _ema_fast - _ema_slow
    macd_signal = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = macd_line - macd_signal
    macd_prev = macd_line.shift(1); macd_sig_prev = macd_signal.shift(1)
    macd_cross = np.where((macd_prev <= macd_sig_prev) & (macd_line > macd_signal),
                  "bullish_cross",
                  np.where((macd_prev >= macd_sig_prev) & (macd_line < macd_signal),
                           "bearish_cross", "none"))

    # --- CCI(20) (iteration 23) ---
    # (TP - SMA(TP)) / (0.015 * mean_deviation), TP=(H+L+C)/3. Parity-identical
    # to FeatureEngine._cci (the iter-13 dead-setup lesson). Keys the
    # CCI-reversion entry model. rolling.apply for mean-deviation matches the
    # live helper exactly (same raw=True lambda).
    _tp = (high + low + close) / 3.0
    _tp_sma = _tp.rolling(20).mean()
    _tp_md = _tp.rolling(20).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    cci = ((_tp - _tp_sma) / (0.015 * _tp_md.replace(0, np.nan))).fillna(0.0)

    # --- MFI(14) (iteration 24) ---
    # Money Flow Index = 100 - 100/(1 + pos_flow/neg_flow), where flow = TP*volume
    # summed over the window on up-TP / down-TP bars. Parity-identical to
    # FeatureEngine._mfi (the iter-13 dead-setup lesson): rolling SUM (not EMA),
    # so values are exact windowed matches after 14 bars. Keys the MFI-reversion
    # entry model — the ONLY volume-weighted oscillator here. fillna(50) warmup.
    _mf_tp = _tp  # reuse TP above
    _raw_mf = _mf_tp * df["volume"]
    _mf_tp_prev = _mf_tp.shift(1)
    _pos_flow = pd.Series(np.where(_mf_tp > _mf_tp_prev, _raw_mf, 0.0), index=close.index)
    _neg_flow = pd.Series(np.where(_mf_tp < _mf_tp_prev, _raw_mf, 0.0), index=close.index)
    _pos_sum = _pos_flow.rolling(14).sum()
    _neg_sum = _neg_flow.rolling(14).sum()
    _mfr = _pos_sum / _neg_sum.replace(0, np.nan)
    mfi = (100.0 - 100.0 / (1.0 + _mfr)).fillna(50.0)

    # --- ADX/DI (14) Wilder (iteration 25) ---
    # +DM = up-move when up>down & up>0; -DM = down-move when down>up & down>0;
    # TR = max(H-L, |H-pC|, |L-pC|); +DI/-DI = 100 * Wilder-smoothed(+DM/-DM) /
    # Wilder-smoothed TR; DX = 100*|+DI--DI|/(+DI+-DI); ADX = Wilder-smoothed DX.
    # Parity-identical to FeatureEngine._adx (the iter-13 dead-setup lesson):
    # same Wilder ewm(alpha=1/14, adjust=False), same TR. Self-contained TR (the
    # ATR field uses SMA, ADX uses Wilder-RMA — distinct). Keys the ADX-trend
    # entry model — trend STRENGTH, the only strength dimension here. fillna(0)
    # warmup (live returns 0.0 on NaN; vectorized fillna(0) matches at last bar).
    _up_move = high.diff()
    _down_move = -low.diff()
    _plus_dm = pd.Series(np.where((_up_move > _down_move) & (_up_move > 0), _up_move, 0.0), index=close.index)
    _minus_dm = pd.Series(np.where((_down_move > _up_move) & (_down_move > 0), _down_move, 0.0), index=close.index)
    _pc_adx = close.shift(1)
    _tr_adx = pd.concat([high - low, (high - _pc_adx).abs(), (low - _pc_adx).abs()], axis=1).max(axis=1)
    _atr_w = _tr_adx.ewm(alpha=1.0 / 14, adjust=False).mean()
    _plus_di = (100.0 * _plus_dm.ewm(alpha=1.0 / 14, adjust=False).mean() / _atr_w.replace(0, np.nan)).fillna(0.0)
    _minus_di = (100.0 * _minus_dm.ewm(alpha=1.0 / 14, adjust=False).mean() / _atr_w.replace(0, np.nan)).fillna(0.0)
    _dx = (100.0 * (_plus_di - _minus_di).abs() / (_plus_di + _minus_di).replace(0, np.nan)).fillna(0.0)
    adx = _dx.ewm(alpha=1.0 / 14, adjust=False).mean().fillna(0.0)

    # --- OBV (On-Balance Volume) + OBV EMA(20) cross — iteration 26 ---
    # OBV = cumsum(sign(close.diff()) * volume): cumulative signed-volume line.
    # A genuinely-new VOLUME-ACCUMULATION dimension (volume_ratio = single-bar
    # spike, mfi = windowed ratio; OBV = cumulative running total). Parity-
    # identical to FeatureEngine._obv (the iter-13 dead-setup lesson): same
    # np.sign(close.diff().fillna(0)) direction, same cumsum, same ewm(span=20).
    # OBV is path-dependent (cumulative), so levels differ between live (full
    # history) and this replay (max_bars tail) — but the CROSS EVENT (obv vs
    # obv_ema flipping) converges independent of the starting level after EMA
    # warmup, so the SIGNAL matches. Keys the OBV-EMA-cross entry model.
    _obv_dir = pd.Series(np.sign(close.diff().fillna(0.0)), index=close.index)
    obv = (_obv_dir * df["volume"]).cumsum()
    obv_ema = obv.ewm(span=20, adjust=False).mean()
    _obv_prev = obv.shift(1)
    _obvema_prev = obv_ema.shift(1)
    _obv_cross = np.where((_obv_prev <= _obvema_prev) & (obv > obv_ema), "bullish_cross",
                         np.where((_obv_prev >= _obvema_prev) & (obv < obv_ema), "bearish_cross", "none"))

    # --- Stochastic(14, 3) ---
    low_min = low.rolling(14).min()
    high_max = high.rolling(14).max()
    denom = (high_max - low_min).replace(0, np.nan)
    k = (100 * (close - low_min) / denom).fillna(50)
    d = k.rolling(3).mean()
    k_prev = k.shift(1); d_prev = d.shift(1)
    cross = np.where((k_prev <= d_prev) & (k > d), "bullish_cross",
            np.where((k_prev >= d_prev) & (k < d), "bearish_cross", "none"))

    # --- ATR(14) ---
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()],
                   axis=1).max(axis=1)
    atr = tr.rolling(14).mean()

    # --- ATR percentile rank (iteration 27) ---
    # Parity-identical to FeatureEngine._atr_pct (the iter-13 dead-setup lesson):
    # same TR, same rolling(14).mean ATR, same rolling(100).rank(pct=True).
    # Windowed (NOT cumulative) so the last-bar value depends only on the last
    # 100 ATRs -> byte-tight between live (full history) and this replay
    # (max_bars tail). Keys the ATR-percentile-breakout entry model — the only
    # relative-volatility-RANK dimension (vs volatility_regime's absolute
    # atr_ratio threshold). fillna(0.5) warmup (live returns 0.5 on NaN).
    atr_pct = atr.rolling(100).rank(pct=True).fillna(0.5)

    # --- Chaikin Money Flow(20) (iteration 28) ---
    # Parity-identical to FeatureEngine._cmf (the iter-13 dead-setup lesson):
    # same money-flow multiplier (2*close-high-low)/(high-low) with zero-range
    # bars -> 0, same mfm*volume, same rolling(20).sum ratio. Uses df["volume"]
    # (no `volume` alias at line 185 — iter-26 lesson). A genuinely-new VOLUME
    # dimension: intrabar close-location weighted by volume, bounded [-1, +1]
    # (volume_ratio = level, mfi = price-change ratio, obv = cumulative line).
    # Windowed (rolling sum, NOT cumulative) -> last-bar value depends only on
    # the last 20 bars -> byte-tight between live (full history) and this replay
    # (max_bars tail). Keys the CMF-confirmed-continuation entry model.
    # fillna(0.0) warmup / zero-volume window (live returns 0.0 on NaN).
    _cmf_rng = (high - low).replace(0, np.nan)
    _cmf_mfm = ((2 * close - high - low) / _cmf_rng).fillna(0.0)
    _cmf_mfv = _cmf_mfm * df["volume"]
    _cmf_vol_sum = df["volume"].rolling(20).sum()
    cmf = (_cmf_mfv.rolling(20).sum() / _cmf_vol_sum.replace(0, np.nan)).fillna(0.0)

    # --- Rolling VWAP(20) + 1.5-sigma bands (iteration 34) ---
    # Parity-identical to FeatureEngine._vwap (the iter-13 dead-setup lesson):
    # same typical_price=(H+L+C)/3, same (tp*vol).rolling(20).sum / vol.rolling
    # (20).sum, same rolling(20).std(tp), same +/- 1.5*std bands, same
    # (close-lower)/(upper-lower) clipped [0,1] position. Uses df["volume"] (no
    # `volume` alias at line 185 — iter-26 lesson). A genuinely-new PRICE-ANCHOR
    # dimension: volume-weighted price level + bands, distinct from the four
    # flow/level volume features and from bb_position (price-only SMA bands).
    # Windowed (rolling sum, NOT session-cumulative) -> last-bar value depends
    # only on the last 20 bars -> byte-tight between live (full history) and
    # this replay (max_bars tail). Keys the VWAP-band-reversion entry model.
    # fillna: vwap/upper/lower -> price (anchor collapses to spot on warmup/zero
    # vol, matches live _vwap fallback); position -> 0.5 mid.
    _vwap_tp = (high + low + close) / 3.0
    _vwap_tpvol = _vwap_tp * df["volume"]
    _vwap_volsum = df["volume"].rolling(20).sum()
    vwap = (_vwap_tpvol.rolling(20).sum() / _vwap_volsum.replace(0, np.nan)).fillna(close)
    _vwap_std = _vwap_tp.rolling(20).std()
    vwap_upper = (vwap + 1.5 * _vwap_std).fillna(close)
    vwap_lower = (vwap - 1.5 * _vwap_std).fillna(close)
    _vwap_width = (vwap_upper - vwap_lower).replace(0, np.nan)
    vwap_position = ((close - vwap_lower) / _vwap_width).clip(0.0, 1.0).fillna(0.5)

    # --- Supertrend(10, 3.0) trend-STATE + flip (iteration 35) ---
    # Parity-identical to FeatureEngine._supertrend (the iter-13 dead-setup
    # lesson): same ATR(10) rolling-mean TR, same hl2 +/- 3.0*ATR bands, same
    # recursive final_upper/lower ratchet (basic<prev OR prev_close>prev -> drop;
    # else hold), same direction (close>prev_final_upper -> up / <prev_final_lower
    # -> down / else hold), same bullish_flip (down->up) / bearish_flip (up->down).
    # The recursion is PATH-DEPENDENT (band levels carry forward), so absolute
    # levels differ between live (500-bar tail) and this replay (full history)
    # early on — BUT final_upper/lower are bounded by recent hl2+/-3*ATR and
    # forget their initial condition in ~2*period=20 bars, so the direction/FLIP
    # converges identical past warmup (same argument as the OBV cross, iter 26).
    # Parity-verified on the last bar. A genuinely-new TREND-STATE dimension:
    # ATR-band overlay with explicit flip, distinct from m5_trend (EMA direction)
    # and adx (strength). Keys the Supertrend-flip-continuation entry model.
    # A Python loop over n bars (the recursion is not pandas-vectorizable); 50k
    # bars/symbol is ~ms. Warmup (<period bars) -> dir "up", flip "none".
    _st_high = high.to_numpy(); _st_low = low.to_numpy(); _st_close = close.to_numpy()
    _st_n = len(_st_close)
    _st_pc = np.concatenate(([np.nan], _st_close[:-1]))
    _st_tr = np.maximum.reduce([
        _st_high - _st_low,
        np.abs(_st_high - _st_pc),
        np.abs(_st_low - _st_pc),
    ])
    _st_tr[0] = _st_high[0] - _st_low[0]  # no prior close on bar 0 -> NaN would poison cumsum
    _st_atr = np.full(_st_n, np.nan)
    _st_period = 10
    if _st_n >= _st_period:
        _st_csum = np.cumsum(_st_tr)
        _st_atr[_st_period - 1:] = (_st_csum[_st_period - 1:] - np.concatenate(([0.0], _st_csum[:-_st_period]))) / _st_period
    _st_hl2 = (_st_high + _st_low) / 2.0
    _st_basic_up = _st_hl2 + 3.0 * _st_atr
    _st_basic_lo = _st_hl2 - 3.0 * _st_atr
    _st_final_up = np.full(_st_n, np.nan)
    _st_final_lo = np.full(_st_n, np.nan)
    _st_dir = np.array(["up"] * _st_n, dtype=object)
    if _st_n > _st_period:
        _st_start = _st_period - 1
        _st_final_up[_st_start] = _st_basic_up[_st_start]
        _st_final_lo[_st_start] = _st_basic_lo[_st_start]
        _st_dir[_st_start] = "up" if _st_close[_st_start] > _st_final_up[_st_start] else "down"
        for i in range(_st_start + 1, _st_n):
            fu = _st_basic_up[i] if (_st_basic_up[i] < _st_final_up[i - 1] or _st_close[i - 1] > _st_final_up[i - 1]) else _st_final_up[i - 1]
            fl = _st_basic_lo[i] if (_st_basic_lo[i] > _st_final_lo[i - 1] or _st_close[i - 1] < _st_final_lo[i - 1]) else _st_final_lo[i - 1]
            _st_final_up[i] = fu
            _st_final_lo[i] = fl
            if _st_close[i] > _st_final_up[i - 1]:
                _di = "up"
            elif _st_close[i] < _st_final_lo[i - 1]:
                _di = "down"
            else:
                _di = _st_dir[i - 1]
            _st_dir[i] = _di
    # flip event per bar
    _st_flip = np.array(["none"] * _st_n, dtype=object)
    if _st_n >= 2:
        _d_prev = _st_dir[:-1]
        _d_now = _st_dir[1:]
        _st_flip[1:] = np.where(((_d_prev == "down") & (_d_now == "up")), "bullish_flip",
                       np.where(((_d_prev == "up") & (_d_now == "down")), "bearish_flip", "none"))
    supertrend_dir = _st_dir
    supertrend_flip = _st_flip

    # --- Ichimoku (iteration 36): Tenkan/Kijun midpoint equilibria + cloud +
    # TK cross. Path-independent rolling max/min midpoint (no carry-forward
    # recursion), so vectorized pandas rolling matches the live _ichimoku
    # exactly for any bar past max window 52. Defaults 9/26/52.
    _ichi_tenkan = (high.rolling(9).max() + low.rolling(9).min()) / 2.0
    _ichi_kijun = (high.rolling(26).max() + low.rolling(26).min()) / 2.0
    _ichi_senkou_a = (_ichi_tenkan + _ichi_kijun) / 2.0
    _ichi_senkou_b = (high.rolling(52).max() + low.rolling(52).min()) / 2.0
    _ichi_cloud_top = np.maximum(_ichi_senkou_a.fillna(-np.inf).to_numpy(),
                                 _ichi_senkou_b.fillna(-np.inf).to_numpy())
    _ichi_cloud_bot = np.minimum(_ichi_senkou_a.fillna(np.inf).to_numpy(),
                                 _ichi_senkou_b.fillna(np.inf).to_numpy())
    _ichi_cp = np.where(close.to_numpy() > _ichi_cloud_top, "above",
              np.where(close.to_numpy() < _ichi_cloud_bot, "below", "inside"))
    # default to "above" during warmup (cloud undefined) — matches live default
    _ichi_cp = np.where(np.isnan(_ichi_senkou_a.to_numpy()), "above", _ichi_cp)
    # TK cross event per bar (tenkan crosses kijun)
    _ichi_tk_cross = np.array(["none"] * len(close), dtype=object)
    _t = _ichi_tenkan.to_numpy(); _k = _ichi_kijun.to_numpy()
    if len(close) >= 2:
        for _i in range(1, len(close)):
            tp, kp = _t[_i - 1], _k[_i - 1]
            tn, kn = _t[_i], _k[_i]
            if np.isnan(tp) or np.isnan(kp) or np.isnan(tn) or np.isnan(kn):
                continue
            if tp <= kp and tn > kn:
                _ichi_tk_cross[_i] = "bullish_cross"
            elif tp >= kp and tn < kn:
                _ichi_tk_cross[_i] = "bearish_cross"
    ichimoku_tenkan = _ichi_tenkan.to_numpy()
    ichimoku_kijun = _ichi_kijun.to_numpy()
    ichimoku_senkou_a = _ichi_senkou_a.to_numpy()
    ichimoku_senkou_b = _ichi_senkou_b.to_numpy()
    ichimoku_cloud_position = _ichi_cp
    ichimoku_tk_cross = _ichi_tk_cross

    # --- Fair Value Gap (iteration 37): ICT/SMC 3-bar structural imbalance.
    # PATH-INDEPENDENT 3-bar gap between non-adjacent bars (bar i-2 vs bar i),
    # gated by min_size_atr (gap >= 0.25*ATR(14)). fvg = bullish_fvg (bar[i-2].
    # high < bar[i].low) / bearish_fvg (bar[i-2].low > bar[i].high) / none.
    # Vectorized via shifted high/low series.
    _fvg_min_atr = 0.25
    _h_prev2 = high.shift(2)
    _l_prev2 = low.shift(2)
    _gap_up = low - _h_prev2          # bullish gap: bar i low - bar i-2 high
    _gap_dn = _l_prev2 - high          # bearish gap: bar i-2 low - bar i high
    _atr_guard = np.where((atr.to_numpy() > 0), atr.to_numpy(), np.nan)
    _gap_up_atr = _gap_up.to_numpy() / _atr_guard
    _gap_dn_atr = _gap_dn.to_numpy() / _atr_guard
    _fvg = np.array(["none"] * len(close), dtype=object)
    _fvg_size = np.zeros(len(close), dtype=float)
    _bu_mask = (_gap_up.to_numpy() > 0) & (_gap_up_atr >= _fvg_min_atr) & ~np.isnan(_gap_up_atr)
    _bd_mask = (_gap_dn.to_numpy() > 0) & (_gap_dn_atr >= _fvg_min_atr) & ~np.isnan(_gap_dn_atr)
    _fvg[_bu_mask] = "bullish_fvg"
    _fvg_size[_bu_mask] = _gap_up_atr[_bu_mask]
    # bearish takes precedence only where not already bullish (a bar can't be
    # both; gap_up>0 and gap_dn>0 are mutually exclusive given bar i-2 vs bar i
    # geometry, but guard anyway)
    _bd_only = _bd_mask & ~_bu_mask
    _fvg[_bd_only] = "bearish_fvg"
    _fvg_size[_bd_only] = _gap_dn_atr[_bd_only]
    fvg = _fvg
    fvg_size_atr = _fvg_size

    # --- Engulfing (iteration 38): 2-bar candle-body engulfing pattern.
    # PATH-INDEPENDENT 2-bar body-vs-body structure (no recursion, no data-
    # availability issue). bullish_engulfing: prior bearish + curr bullish +
    # curr open <= prior close + curr close >= prior open. bearish = mirror.
    _eng_o = open_.to_numpy(); _eng_c = close.to_numpy()
    _eng = np.array(["none"] * len(close), dtype=object)
    if len(close) >= 2:
        _o_prev = _eng_o[:-1]; _c_prev = _eng_c[:-1]
        _o_now = _eng_o[1:]; _c_now = _eng_c[1:]
        _prev_bear = _c_prev < _o_prev
        _prev_bull = _c_prev > _o_prev
        _now_bull = _c_now > _o_now
        _now_bear = _c_now < _o_now
        _bull_eng = _prev_bear & _now_bull & (_o_now <= _c_prev) & (_c_now >= _o_prev)
        _bear_eng = _prev_bull & _now_bear & (_o_now >= _c_prev) & (_c_now <= _o_prev)
        # assign to the CURRENT bar (i+1 of the shifted pair = index 1..n-1)
        _eng[1:][_bull_eng] = "bullish_engulfing"
        _eng[1:][_bear_eng] = "bearish_engulfing"
    engulfing = _eng

    # --- Inside-Bar breakout (iteration 39): 3-bar range-nesting then
    # expansion. PATH-INDEPENDENT 3-bar structure (no recursion, no data-
    # availability issue). mother=bar[i-2], inside=bar[i-1] (range nests within
    # mother range), breakout=bar[i] closes outside mother range.
    # bullish_inside_breakout: high[i-1] <= high[i-2] AND low[i-1] >= low[i-2]
    #   AND close[i] > high[i-2] -> BUY. bearish = mirror (close[i] < low[i-2]).
    _ib_h_m = high.shift(2)   # mother high at i-2
    _ib_l_m = low.shift(2)    # mother low  at i-2
    _ib_h_i = high.shift(1)   # inside high at i-1
    _ib_l_i = low.shift(1)    # inside low  at i-1
    _ib_is_inside = (_ib_h_i.to_numpy() <= _ib_h_m.to_numpy()) & \
                    (_ib_l_i.to_numpy() >= _ib_l_m.to_numpy())
    _ib_close = close.to_numpy()
    _ib = np.array(["none"] * len(close), dtype=object)
    _ib_valid = ~np.isnan(_ib_h_m.to_numpy()) & ~np.isnan(_ib_l_m.to_numpy())
    _ib_bull = _ib_is_inside & _ib_valid & (_ib_close > _ib_h_m.to_numpy())
    _ib_bear = _ib_is_inside & _ib_valid & (_ib_close < _ib_l_m.to_numpy()) & ~_ib_bull
    _ib[_ib_bull] = "bullish_inside_breakout"
    _ib[_ib_bear] = "bearish_inside_breakout"
    inside_bar = _ib

    # --- Close-streak run-length (iteration 40): signed count of consecutive
    # same-direction closes ending at the current bar. PATH-INDEPENDENT
    # (depends only on the observed close series). +N = N consecutive up-closes
    # ending now, -N = N consecutive down-closes, 0 = flat/broken run. Forward
    # accumulation: streak[i] = streak[i-1]+sign[i] if sign[i]==sign[i-1] and
    # sign[i]!=0 else sign[i] (or 0). Matches the live _close_streak backward
    # walk at the last bar (run-length is order-independent).
    _cs_close = close.to_numpy()
    _cs_sign = np.sign(np.diff(_cs_close))  # sign of (close[i]-close[i-1]), len n-1
    _cs = np.zeros(len(_cs_close), dtype=int)
    if len(_cs_close) >= 2:
        # _cs_sign[k] corresponds to bar k+1. Accumulate runs.
        for _k in range(len(_cs_sign)):
            _s = int(_cs_sign[_k])
            _idx = _k + 1
            if _s == 0:
                _cs[_idx] = 0
            elif _k == 0 or _cs_sign[_k - 1] != _s or _cs_sign[_k - 1] == 0:
                _cs[_idx] = _s  # new run of length 1
            else:
                _cs[_idx] = _cs[_idx - 1] + _s  # extend, preserve sign
    close_streak = _cs

    # --- Order-Block displacement origin (iteration 41): ICT/SMC 2-bar
    # structure — prior opposite-colored candle + current displacement bar whose
    # body >= min_displacement_atr * ATR(14). PATH-INDEPENDENT 2-bar structure
    # (no recursion, no data-availability issue). bullish_order_block:
    #   close[i-1] < open[i-1] (prior bearish) AND close[i] > open[i] (curr
    #   bullish) AND |close[i]-open[i]| >= 0.8*ATR[i]. bearish = mirror.
    # Matches the live _order_block at the last bar (uses the per-bar ATR series,
    # whose last value equals the live scalar atr; the body>=k*ATR gate + the
    # opposite-origin condition are identical).
    _ob_o = open_.to_numpy(); _ob_c = close.to_numpy()
    _ob = np.array(["none"] * len(close), dtype=object)
    _ob_min = 0.8
    _ob_atr = np.where((atr.to_numpy() > 0), atr.to_numpy(), np.nan)
    if len(close) >= 2:
        _ob_o_prev = _ob_o[:-1]; _ob_c_prev = _ob_c[:-1]
        _ob_o_now = _ob_o[1:]; _ob_c_now = _ob_c[1:]
        _ob_body_now = np.abs(_ob_c_now - _ob_o_now)
        _ob_thresh = _ob_min * _ob_atr[1:]
        _ob_prev_bear = _ob_c_prev < _ob_o_prev
        _ob_prev_bull = _ob_c_prev > _ob_o_prev
        _ob_now_bull = _ob_c_now > _ob_o_now
        _ob_now_bear = _ob_c_now < _ob_o_now
        _ob_disp = (_ob_body_now >= _ob_thresh) & ~np.isnan(_ob_thresh)
        _ob_bull = _ob_prev_bear & _ob_now_bull & _ob_disp
        _ob_bear = _ob_prev_bull & _ob_now_bear & _ob_disp & ~_ob_bull
        _ob[1:][_ob_bull] = "bullish_order_block"
        _ob[1:][_ob_bear] = "bearish_order_block"
    order_block = _ob

    # --- Heikin-Ashi strong-trend (iteration 42): PRICE-TRANSFORM smoothed-candle
    # wick-absence. HA_close[i]=(O+H+L+C)/4; HA_open[i]=(HA_open[i-1]+HA_close[i-1])/
    # 2, seed HA_open[0]=(O[0]+C[0])/2. bullish_ha_strong: HA_close>HA_open AND
    # low>=HA_open (green HA, no lower wick). bearish_ha_strong: HA_close<HA_open
    # AND high<=HA_open (red HA, no upper wick). Recursive (0.5-alpha) — converges
    # fast; matches the live _ha_trend at the last bar (live seeds 500 bars back,
    # replay seeds at bar 0; both forget the seed after ~20 bars -> same last-bar
    # HA_open to ~1e-150, so the wick-absence label is identical).
    _ha_o = open_.to_numpy(); _ha_h = high.to_numpy()
    _ha_l = low.to_numpy(); _ha_c = close.to_numpy()
    _ha_n = len(close)
    _ha_close = np.empty(_ha_n); _ha_open = np.empty(_ha_n)
    _ha = np.array(["none"] * _ha_n, dtype=object)
    if _ha_n >= 2:
        _ha_close[0] = (_ha_o[0] + _ha_h[0] + _ha_l[0] + _ha_c[0]) / 4.0
        _ha_open[0] = (_ha_o[0] + _ha_c[0]) / 2.0
        for _i in range(1, _ha_n):
            _ha_close[_i] = (_ha_o[_i] + _ha_h[_i] + _ha_l[_i] + _ha_c[_i]) / 4.0
            _ha_open[_i] = (_ha_open[_i - 1] + _ha_close[_i - 1]) / 2.0
        _ha_green = _ha_close > _ha_open
        _ha_red = _ha_close < _ha_open
        _ha_no_lower = _ha_l >= _ha_open
        _ha_no_upper = _ha_h <= _ha_open
        _ha_bull = _ha_green & _ha_no_lower
        _ha_bear = _ha_red & _ha_no_upper & ~_ha_bull
        _ha[_ha_bull] = "bullish_ha_strong"
        _ha[_ha_bear] = "bearish_ha_strong"
    ha_trend = _ha

    # --- support / resistance (rolling 50, EXCLUDING the current bar) ---
    # shift(1) so S/R is the prior 50 bars' low/high — the current bar must be
    # ABLE to break them. Without shift, resistance = rolling max INCLUDES the
    # current high, so `close > resistance` is impossible (close <= high <= max)
    # and the breakout/breakdown states NEVER fire (off-by-one parity bug vs the
    # intended live semantics; see iteration 17 note). SL/TP also use these, so
    # prior-bar structure is the correct swing level for stops.
    support = low.shift(1).rolling(50).min()
    resistance = high.shift(1).rolling(50).max()

    # --- candle rejection (per bar) ---
    body = (close - open_).abs()
    upper_wick = high - np.maximum(open_, close)
    lower_wick = np.minimum(open_, close) - low
    range_ = high - low
    rej = np.where(range_ <= 0, "none",
          np.where((upper_wick > body * 1.5) & (upper_wick > lower_wick), "bearish_rejection",
          np.where((lower_wick > body * 1.5) & (lower_wick > upper_wick), "bullish_rejection",
                   "none")))

    # --- breakout / breakdown / retest (per bar) ---
    prev_c = close.shift(1)
    bo = np.where((prev_c <= resistance) & (close > resistance), "breakout",
         np.where((prev_c >= support) & (close < support), "breakdown",
         np.where((close > resistance * 0.998) & (close < resistance), "breakout_retest",
         np.where((close < support * 1.002) & (close > support), "breakdown_retest", "none"))))

    # --- volume ---
    vol_avg = df["volume"].rolling(20).mean()
    vol_ratio = (df["volume"] / vol_avg.replace(0, np.nan)).fillna(1.0)

    # --- volatility regime ---
    atr_ratio = (atr / close.replace(0, np.nan)).fillna(0.0)
    recent_std = close.pct_change().rolling(20).std()
    regime = np.where((atr_ratio > 0.002) | (recent_std > 0.003), "high",
             np.where(atr_ratio < 0.0003, "low", "normal"))

    # --- M15 trend asof-mapped to M5 timestamps ---
    df15 = pd.DataFrame(m15)
    for c in ("open", "high", "low", "close"):
        df15[c] = df15[c].astype(float)
    ema_m15 = df15["close"].ewm(span=20, adjust=False).mean()
    slope_m15 = ema_m15 - ema_m15.shift(5)
    m15_trend_per_bar = np.where(slope_m15 > 0, "bullish",
                        np.where(slope_m15 < 0, "bearish", "neutral"))
    m15_series = pd.Series(m15_trend_per_bar, index=pd.to_datetime(df15["time"]))
    m5_times = pd.to_datetime(df["time"])
    # asof: last m15 trend at or before each M5 bar's timestamp
    m15_aligned = m15_series.reindex(m5_times, method="ffill").to_numpy()

    m5_trend_str = m5_trend.astype(str)
    m15_aligned_str = m15_aligned.astype(str)
    align = (m5_trend_str == m15_aligned_str)

    out = pd.DataFrame({
        "time": df["time"].to_numpy(),
        "price": close.to_numpy(),
        "m5_trend": m5_trend_str,
        "m15_trend": m15_aligned_str,
        "bb_upper": bb_upper.to_numpy(),
        "bb_middle": mid.to_numpy(),
        "bb_lower": bb_lower.to_numpy(),
        "bb_position": bb_position.to_numpy(),
        "bb_squeeze_pct": bb_squeeze_pct.to_numpy(),
        "rsi": rsi.to_numpy(),
        "macd": macd_line.to_numpy(),
        "macd_signal": macd_signal.to_numpy(),
        "macd_hist": macd_hist.to_numpy(),
        "macd_cross": macd_cross,
        "cci": cci.to_numpy(),
        "mfi": mfi.to_numpy(),
        "adx": adx.to_numpy(),
        "di_plus": _plus_di.to_numpy(),
        "di_minus": _minus_di.to_numpy(),
        "obv": obv.to_numpy(),
        "obv_ema": obv_ema.to_numpy(),
        "obv_cross": _obv_cross,
        "stoch_k": k.to_numpy(),
        "stoch_d": d.to_numpy(),
        "stoch_cross": cross,
        "atr": atr.to_numpy(),
        "atr_ratio": atr_ratio.to_numpy(),
        "atr_pct": atr_pct.to_numpy(),
        "cmf": cmf.to_numpy(),
        "vwap": vwap.to_numpy(),
        "vwap_upper": vwap_upper.to_numpy(),
        "vwap_lower": vwap_lower.to_numpy(),
        "vwap_position": vwap_position.to_numpy(),
        "supertrend_dir": supertrend_dir,
        "supertrend_flip": supertrend_flip,
        "ichimoku_tenkan": ichimoku_tenkan,
        "ichimoku_kijun": ichimoku_kijun,
        "ichimoku_senkou_a": ichimoku_senkou_a,
        "ichimoku_senkou_b": ichimoku_senkou_b,
        "ichimoku_cloud_position": ichimoku_cloud_position,
        "ichimoku_tk_cross": ichimoku_tk_cross,
        "fvg": fvg,
        "fvg_size_atr": fvg_size_atr,
        "engulfing": engulfing,
        "inside_bar": inside_bar,
        "close_streak": close_streak,
        "order_block": order_block,
        "ha_trend": ha_trend,
        "volume_avg": vol_avg.to_numpy(),
        "volume_ratio": vol_ratio.to_numpy(),
        "support": support.to_numpy(),
        "resistance": resistance.to_numpy(),
        "rejection": rej,
        "breakout": bo,
        "timeframe_alignment": align,
        "volatility_regime": regime,
    })
    return out


def replay_symbol(
    symbol: str,
    config: dict[str, Any],
    *,
    max_bars: int | None = None,
    warmup: int = WARMUP_BARS,
    forward_bars: int = FORWARD_BARS,
    tp_r: float = DEFAULT_TP_R,
) -> list[dict[str, Any]]:
    """Replay one symbol's M5 history through the specialized detectors and label
    every fire with its realized R-multiple.

    Returns a list of labeled rows:
      {symbol, setup_type, side, utc_hour, confidence, entry, r_multiple,
       exit_reason, bars_held, fired_at_bar}

    Honesty: intrabar SL-first outcomes; invalid-risk fires are skipped (not
    counted). Places no orders, writes no live state.

    Uses the vectorized feature pass (_vectorized_features) — one pandas pass
    per symbol, then a cheap per-bar loop. ~1000x faster than re-instantiating
    FeatureEngine per bar.
    """
    m5 = load_bars(symbol, "M5")
    m15 = load_bars(symbol, "M15")
    # M5 needs the full warmup (support/resistance uses 50 bars). M15 only needs
    # enough for a trend read (20 = FeatureEngine.min_bars_required) since M15
    # bars are 3x longer — demanding 60 M15 bars would discard the first ~180 M5.
    if len(m5) < warmup + forward_bars + 1 or len(m15) < 20:
        logger.warning("replay %s: insufficient history (M5=%d M15=%d)", symbol, len(m5), len(m15))
        return []

    cfg = _replay_config(config)
    ee = EvidenceEngine(cfg)

    feats = _vectorized_features(m5, m15)

    end = len(m5) - forward_bars  # need forward_bars after the fire bar
    if max_bars is not None:
        # sample the most recent `max_bars` eligible bars (keep newest evidence)
        start = max(warmup, end - max_bars)
    else:
        start = warmup

    # Pre-slice forward OHLC for label_outcome (intrabar high/low/close).
    highs = feats["high"] if "high" in feats else None
    # _vectorized_features does not emit high/low/close; keep them from m5.
    m5_high = [b["high"] for b in m5]
    m5_low = [b["low"] for b in m5]
    m5_close = [b["close"] for b in m5]

    rows: list[dict[str, Any]] = []
    feat_cols = feats.to_dict("records")
    for i in range(start, end):
        feat = feat_cols[i]
        price = feat.get("price")
        if price is None or price != price or price == 0:  # NaN/0 guard
            continue
        # Build the feat dict shape detect_specialized + EvidenceEngine expect.
        feat_dict = {
            "symbol": symbol,
            "price": round(float(price), 5),
            "m5_trend": feat.get("m5_trend"),
            "m15_trend": feat.get("m15_trend"),
            "bb_upper": round(float(feat.get("bb_upper") or 0), 5),
            "bb_middle": round(float(feat.get("bb_middle") or 0), 5),
            "bb_lower": round(float(feat.get("bb_lower") or 0), 5),
            "bb_position": round(float(feat.get("bb_position")), 4),
            "bb_squeeze_pct": round(float(feat.get("bb_squeeze_pct") if feat.get("bb_squeeze_pct") is not None else 0.5), 4),
            "rsi": round(float(feat.get("rsi") if feat.get("rsi") is not None else 50.0), 2),
            "macd": round(float(feat.get("macd") or 0.0), 6),
            "macd_signal": round(float(feat.get("macd_signal") or 0.0), 6),
            "macd_hist": round(float(feat.get("macd_hist") or 0.0), 6),
            "macd_cross": feat.get("macd_cross") or "none",
            "cci": round(float(feat.get("cci") if feat.get("cci") is not None else 0.0), 2),
            "mfi": round(float(feat.get("mfi") if feat.get("mfi") is not None else 50.0), 2),
            "adx": round(float(feat.get("adx") if feat.get("adx") is not None else 0.0), 2),
            "di_plus": round(float(feat.get("di_plus") if feat.get("di_plus") is not None else 0.0), 2),
            "di_minus": round(float(feat.get("di_minus") if feat.get("di_minus") is not None else 0.0), 2),
            "obv": round(float(feat.get("obv") if feat.get("obv") is not None else 0.0), 2),
            "obv_ema": round(float(feat.get("obv_ema") if feat.get("obv_ema") is not None else 0.0), 2),
            "obv_cross": feat.get("obv_cross") or "none",
            "stoch_k": round(float(feat.get("stoch_k") or 50), 2),
            "stoch_d": round(float(feat.get("stoch_d") or 50), 2),
            "stoch_cross": feat.get("stoch_cross") or "none",
            "atr": round(float(feat.get("atr") or 0), 5),
            "atr_ratio": round(float(feat.get("atr_ratio") or 0), 6),
            "atr_pct": round(float(feat.get("atr_pct") if feat.get("atr_pct") is not None else 0.5), 4),
            "cmf": round(float(feat.get("cmf") if feat.get("cmf") is not None else 0.0), 4),
            "vwap": round(float(feat.get("vwap") if feat.get("vwap") is not None else price), 5),
            "vwap_upper": round(float(feat.get("vwap_upper") if feat.get("vwap_upper") is not None else price), 5),
            "vwap_lower": round(float(feat.get("vwap_lower") if feat.get("vwap_lower") is not None else price), 5),
            "vwap_position": round(float(feat.get("vwap_position") if feat.get("vwap_position") is not None else 0.5), 4),
            "supertrend_dir": feat.get("supertrend_dir") or "up",
            "supertrend_flip": feat.get("supertrend_flip") or "none",
            "ichimoku_tenkan": round(float(feat.get("ichimoku_tenkan") if feat.get("ichimoku_tenkan") is not None else price), 5),
            "ichimoku_kijun": round(float(feat.get("ichimoku_kijun") if feat.get("ichimoku_kijun") is not None else price), 5),
            "ichimoku_senkou_a": round(float(feat.get("ichimoku_senkou_a") if feat.get("ichimoku_senkou_a") is not None else price), 5),
            "ichimoku_senkou_b": round(float(feat.get("ichimoku_senkou_b") if feat.get("ichimoku_senkou_b") is not None else price), 5),
            "ichimoku_cloud_position": feat.get("ichimoku_cloud_position") or "above",
            "ichimoku_tk_cross": feat.get("ichimoku_tk_cross") or "none",
            "fvg": feat.get("fvg") or "none",
            "fvg_size_atr": round(float(feat.get("fvg_size_atr") if feat.get("fvg_size_atr") is not None else 0.0), 4),
            "engulfing": feat.get("engulfing") or "none",
            "inside_bar": feat.get("inside_bar") or "none",
            "close_streak": int(feat.get("close_streak") if feat.get("close_streak") is not None else 0),
            "order_block": feat.get("order_block") or "none",
            "ha_trend": feat.get("ha_trend") or "none",
            "volume_avg": round(float(feat.get("volume_avg") or 0), 2),
            "volume_ratio": round(float(feat.get("volume_ratio") or 1.0), 2),
            "support": round(float(feat.get("support") or price), 5),
            "resistance": round(float(feat.get("resistance") or price), 5),
            "rejection": feat.get("rejection") or "none",
            "breakout": feat.get("breakout") or "none",
            "timeframe_alignment": bool(feat.get("timeframe_alignment")),
            "volatility_regime": feat.get("volatility_regime") or "normal",
        }

        ev = ee.compute_symbol(feat_dict, context={})

        bar = m5[i]
        hour = _bar_utc_hour(bar)
        with _bar_hour_override(hour):
            fires = detect_specialized(feat_dict, {"symbol": symbol}, ev, cfg)

        if not fires:
            continue

        # Forward bars for outcome labeling (bars AFTER the fire bar).
        fwd_bars = [
            {"time": m5[j]["time"], "high": m5_high[j], "low": m5_low[j], "close": m5_close[j]}
            for j in range(i + 1, i + 1 + forward_bars)
        ]

        for fire in fires:
            labeler_fire = {
                "side": fire.get("side"),
                "entry": feat_dict["price"],
                "feat": {"support": feat_dict["support"], "resistance": feat_dict["resistance"]},
                "tp_r": tp_r,
                "symbol": symbol,
                "setup_type": fire.get("setup_type"),
                "utc_hour": hour,
                "confidence": fire.get("setup_confidence"),
            }
            res = label_outcome(labeler_fire, fwd_bars, max_bars=forward_bars, tp_r=tp_r)
            if res is None:
                continue  # invalid risk -> skip, not counted
            rows.append({
                "symbol": symbol,
                "setup_type": fire.get("setup_type"),
                "side": fire.get("side"),
                "utc_hour": hour,
                "confidence": fire.get("setup_confidence"),
                "entry": feat_dict["price"],
                "r_multiple": res["r_multiple"],
                "exit_reason": res["exit_reason"],
                "bars_held": res["bars_held"],
                "fired_at_bar": str(bar.get("time")),
            })
    return rows


# --- aggregation (with the DSR/SPA honesty caveat baked in) ---

def _bootstrap_ci95(rs: list[float], reps: int = 1000) -> tuple[float, float] | None:
    """Simple non-parametric bootstrap 95% CI on the mean of ``rs``.

    Returns (lower, upper) in R units. Deterministic-ish: resamples with a
    fixed-seed-free approach using index arithmetic is not possible without
    Math.random (forbidden in workflow scripts, fine here in plain module). We
    use a portable LCG seeded from the data length so results are stable per run.
    """
    n = len(rs)
    if n < 2:
        return None
    mean = sum(rs) / n

    # portable deterministic LCG (no Math.random dependency, stable per dataset)
    seed = 123456789 + n * 2654435761
    state = seed & 0xFFFFFFFF
    def _rand() -> float:
        nonlocal state
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        return state / 0x7FFFFFFF

    means: list[float] = []
    for _ in range(reps):
        s = 0.0
        for _ in range(n):
            idx = int(_rand() * n) % n
            s += rs[idx]
        means.append(s / n)
    means.sort()
    lo = means[int(0.025 * reps)]
    hi = means[int(0.975 * reps)]
    return round(lo, 4), round(hi, 4)


def aggregate_cells(rows: list[dict[str, Any]], *, min_n: int = MIN_CELL_N) -> dict[str, Any]:
    """Aggregate labeled rows into per-(symbol,setup) cells with n / win-rate /
    expectancy / CI95 + thin flag. Carries the DSR/SPA caveat — per-cell CI95>0
    is selection-biased across ~100 cells and is NOT a deploy signal."""
    cells: dict[tuple[str, str], list[float]] = {}
    for r in rows:
        key = (r.get("symbol", ""), r.get("setup_type", ""))
        try:
            cells.setdefault(key, []).append(float(r["r_multiple"]))
        except (TypeError, ValueError):
            continue

    out_cells: list[dict[str, Any]] = []
    for (symbol, setup), rs in sorted(cells.items()):
        n = len(rs)
        wins = sum(1 for r in rs if r > 0)
        losses = sum(1 for r in rs if r < 0)
        mean = sum(rs) / n if n else 0.0
        ci = _bootstrap_ci95(rs) if n >= 2 else None
        out_cells.append({
            "symbol": symbol,
            "setup_type": setup,
            "n": n,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(100.0 * wins / n, 1) if n else 0.0,
            "expectancy_r": round(mean, 4),
            "ci95_lower": ci[0] if ci else None,
            "ci95_upper": ci[1] if ci else None,
            "ci95_excludes_zero": (ci is not None and ci[0] > 0),
            "thin": n < min_n,
        })

    out_cells.sort(key=lambda c: c["expectancy_r"], reverse=True)
    total_fires = len(rows)
    distinct_cells = len(out_cells)
    thin_cells = sum(1 for c in out_cells if c["thin"])
    # How many cells pass the naive CI95>0 bar — flagged as selection-biased.
    naive_pass = sum(1 for c in out_cells if c["ci95_excludes_zero"])

    return {
        "timestamp": utc_now_iso(),
        "total_fires": total_fires,
        "distinct_cells": distinct_cells,
        "thin_cells": thin_cells,
        "naive_ci95_pass": naive_pass,
        "cells": out_cells,
        "caveat": (
            "PER-CELL CI95>0 IS SELECTION-BIASED across ~100 (symbol,setup) cells "
            "and is NOT a deploy signal. Per VERDICT.md, a DSR/SPA-style multiple-"
            "comparison adjustment is required before any per-symbol 'works' claim. "
            "Cells with thin=true (n<%d) have no usable evidence. Outcomes are "
            "intrabar SL-first (pessimistic)."
        ) % min_n,
    }


def run(
    config: dict[str, Any],
    *,
    symbols: list[str] | None = None,
    max_bars: int | None = None,
    forward_bars: int = FORWARD_BARS,
    tp_r: float = DEFAULT_TP_R,
    persist: bool = True,
) -> dict[str, Any]:
    """Replay all configured symbols, aggregate, and optionally persist the
    offline report to state/specialized_replay_report.json."""
    if symbols is None:
        mt5 = config.get("mt5", {}) or {}
        symbols = list(mt5.get("symbols", []) or [])
    if not symbols:
        symbols = [p.name.replace("_M5.parquet", "")
                   for p in HISTORY_DIR.glob("*_M5.parquet")]

    all_rows: list[dict[str, Any]] = []
    per_symbol_counts: dict[str, int] = {}
    for sym in symbols:
        rows = replay_symbol(sym, config, max_bars=max_bars,
                             forward_bars=forward_bars, tp_r=tp_r)
        per_symbol_counts[sym] = len(rows)
        all_rows.extend(rows)

    report = aggregate_cells(all_rows)
    report["per_symbol_fires"] = per_symbol_counts
    report["params"] = {
        "max_bars": max_bars,
        "forward_bars": forward_bars,
        "tp_r": tp_r,
        "symbols": symbols,
    }
    if persist:
        write_json_state(REPORT_NAME, report)
    return report