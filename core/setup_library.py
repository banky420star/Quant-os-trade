"""Setup Library — explicit setup definitions with rules and regime compatibility."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class SetupDefinition:
    name: str
    display_name: str
    allowed_regimes: tuple[str, ...]
    blocked_regimes: tuple[str, ...] = ()
    min_confidence: float = 0.5
    min_rr: float = 1.2
    description: str = ""
    entry_hints: list[str] = field(default_factory=list)
    exit_hints: list[str] = field(default_factory=list)


SETUP_LIBRARY: dict[str, SetupDefinition] = {
    "trend_continuation": SetupDefinition(
        name="trend_continuation",
        display_name="Trend Continuation",
        allowed_regimes=("strong_trend", "weak_trend", "expansion", "compression", "transitional"),
        blocked_regimes=("range", "volatility_spike"),
        min_confidence=0.55,
        description="Trade with aligned trend after momentum confirmation.",
        entry_hints=["M5 and M15 aligned", "move_type continuation", "volume confirming"],
        exit_hints=["SL beyond structure", "TP at 1.5R minimum"],
    ),
    "pullback": SetupDefinition(
        name="pullback",
        display_name="Pullback",
        allowed_regimes=("strong_trend", "weak_trend", "expansion", "compression", "transitional"),
        blocked_regimes=("range", "volatility_spike"),
        min_confidence=0.5,
        description="Retest of broken level or EMA in trending market.",
        entry_hints=["Pullback to structure", "Trend bias intact"],
        exit_hints=["SL beyond pullback swing", "TP at prior high/low"],
    ),
    "breakout": SetupDefinition(
        name="breakout",
        display_name="Breakout",
        allowed_regimes=("expansion", "compression", "strong_trend", "accumulation"),
        blocked_regimes=("range", "distribution"),
        min_confidence=0.55,
        description="Break of key support/resistance with volume.",
        entry_hints=["Breakout candle closed", "Volume above average"],
        exit_hints=["SL inside range", "TP measured move"],
    ),
    "compression_breakout": SetupDefinition(
        name="compression_breakout",
        display_name="Compression Breakout",
        allowed_regimes=("compression", "accumulation"),
        blocked_regimes=("strong_trend", "volatility_spike"),
        min_confidence=0.55,
        description="Volatility squeeze release.",
        entry_hints=["BB squeeze", "ATR compression resolving"],
        exit_hints=["SL inside coil", "TP 2x coil width"],
    ),
    "range_fade": SetupDefinition(
        name="range_fade",
        display_name="Range Fade",
        allowed_regimes=("range", "distribution", "accumulation"),
        blocked_regimes=("strong_trend", "weak_trend", "expansion"),
        min_confidence=0.5,
        description="Fade extremes in defined range.",
        entry_hints=["Price at range boundary", "No trend alignment"],
        exit_hints=["SL beyond range", "TP mid-range"],
    ),
    "liquidity_sweep": SetupDefinition(
        name="liquidity_sweep",
        display_name="Liquidity Sweep",
        allowed_regimes=("range", "weak_trend", "distribution", "accumulation"),
        blocked_regimes=("volatility_spike",),
        min_confidence=0.55,
        description="Stop hunt beyond level followed by rejection.",
        entry_hints=["Wick beyond S/R", "Rejection candle", "Liquidity score high"],
        exit_hints=["SL beyond sweep wick", "TP opposite range bound"],
    ),
    "mean_reversion": SetupDefinition(
        name="mean_reversion",
        display_name="Mean Reversion",
        allowed_regimes=("range", "distribution", "accumulation", "compression"),
        blocked_regimes=("strong_trend", "expansion"),
        min_confidence=0.5,
        description="Fade extension from Bollinger bands.",
        entry_hints=["BB position extreme", "Not in strong trend"],
        exit_hints=["SL beyond band", "TP middle band"],
    ),
    "false_breakout": SetupDefinition(
        name="false_breakout",
        display_name="False Breakout",
        allowed_regimes=("range", "distribution", "accumulation", "weak_trend"),
        blocked_regimes=("strong_trend", "expansion"),
        min_confidence=0.5,
        description="Failed breakout with rejection back into range.",
        entry_hints=["Breakout failure", "Rejection at extreme"],
        exit_hints=["SL beyond false break", "TP range mid"],
    ),
    # 2026-07-31 — specialized ICT/SMC killzone setups sourced from TradingView
    # community indicators (Gold SMC Dashboard, Smart Money Gold Map, XAU/USD
    # Killzones 2026 playbook). Opt-in via signals.specialized_setups.enabled.
    # HONEST: per VERDICT.md there is no deployable edge at retail 30bps; adding
    # these does NOT create edge. The value is GRANULARITY — new setup_types
    # mean new culturing cells so the data-driven veto + bandit + setup-aggregate
    # veto can distinguish per-symbol what works. They flow through every
    # existing verifier gate; if one loses on a symbol, culturing blocks it.
    "silver_bullet": SetupDefinition(
        name="silver_bullet",
        display_name="Silver Bullet (ICT)",
        allowed_regimes=("weak_trend", "range", "distribution", "transitional", "strong_trend"),
        blocked_regimes=("volatility_spike",),
        min_confidence=0.55,
        description="ICT Silver Bullet — 14:00-15:00 UTC session-liquidity sweep + CHoCH (rejection) entry.",
        entry_hints=["14:00-15:00 UTC window", "Sweep of session high/low", "Rejection (CHoCH proxy)"],
        exit_hints=["SL beyond sweep wick", "TP next HTF liquidity pool"],
    ),
    "london_judas": SetupDefinition(
        name="london_judas",
        display_name="London Judas Swing (ICT)",
        allowed_regimes=("range", "weak_trend", "distribution", "accumulation", "transitional"),
        blocked_regimes=("strong_trend", "expansion", "volatility_spike"),
        min_confidence=0.55,
        description="London Open manipulation — fake move sweeps liquidity then reverses (Judas Swing).",
        entry_hints=["07:00-10:00 UTC London killzone", "Sweep + rejection at session extreme"],
        exit_hints=["SL beyond manipulation wick", "TP opposite session extreme"],
    ),
    # 2026-07-31 iteration 2 — symbol-specific breakout setups sourced from
    # TradingView community indicators (Kane's Oil ORB, NexusFi CL playbook,
    # Asian Range Sweep scanners). Breakout-based (feat.breakout), distinct
    # from the iteration-1 rejection-based ICT setups. Per-symbol trial via
    # signals.specialized_setups.symbols allowlist (oil_orb -> USOILm,
    # asia_range_breakout -> FX majors / BTCUSDm).
    "oil_orb": SetupDefinition(
        name="oil_orb",
        display_name="Oil Opening Range Breakout",
        allowed_regimes=("strong_trend", "weak_trend", "expansion", "compression", "transitional"),
        blocked_regimes=("range", "distribution", "volatility_spike"),
        min_confidence=0.55,
        description="Crude oil opening-range breakout — 14:00-19:00 UTC NY execution window (9 AM ET ORB).",
        entry_hints=["14:00-19:00 UTC window", "Breakout of session opening range", "Volume confirms"],
        exit_hints=["SL inside opening range", "TP measured move / 2x OR width"],
    ),
    "asia_range_breakout": SetupDefinition(
        name="asia_range_breakout",
        display_name="Asian Range Breakout (London open)",
        allowed_regimes=("strong_trend", "weak_trend", "expansion", "compression", "transitional"),
        blocked_regimes=("range", "distribution", "volatility_spike"),
        min_confidence=0.55,
        description="Breakout of the Asian session range at the London open (07:00-10:00 UTC). FX/crypto.",
        entry_hints=["Asian range formed 00:00-04:00 UTC", "London open breaks range high/low"],
        exit_hints=["SL inside Asian range", "TP opposite range edge / measured move"],
    ),
    # 2026-07-31 iteration 3 — US equity-index specialized setups. Sourced from
    # TradingView community (9:30 Breakout / kaiserfx_, ORB - Futures and Stocks
    # / tolosatrader, US 30 Daily Breakout Strategy / yavanmahur) + Fazen Capital
    # + Vortex Capital 60-day ORB research. Breakout-based; target US500m /
    # US30m / NAS100m via signals.specialized_setups.symbols allowlist.
    "us_open_orb": SetupDefinition(
        name="us_open_orb",
        display_name="US RTH Open ORB",
        allowed_regimes=("strong_trend", "weak_trend", "expansion", "compression", "transitional"),
        blocked_regimes=("range", "distribution", "volatility_spike"),
        min_confidence=0.55,
        description="US index RTH opening-range breakout — 14:00-16:00 UTC (9:30 AM ET first ~90 min).",
        entry_hints=["9:30 AM ET RTH open", "Opening-range break in first 60-90 min", "Volume >= 1.5x avg"],
        exit_hints=["SL opposite OR boundary", "TP 1.5-2x OR width / IB extension", "Time exit ~10:15-10:30 ET"],
    ),
    "prev_day_breakout": SetupDefinition(
        name="prev_day_breakout",
        display_name="Previous Day High/Low Breakout",
        allowed_regimes=("strong_trend", "weak_trend", "expansion", "transitional"),
        blocked_regimes=("range", "distribution", "compression", "volatility_spike"),
        min_confidence=0.55,
        description="Close beyond previous day high/low during US RTH (14:00-21:00 UTC). One per day, NY reset.",
        entry_hints=["Close above prev day high -> BUY / below prev day low -> SELL", "US RTH session"],
        exit_hints=["SL back inside prev day range", "TP measured move"],
    ),
    # 2026-07-31 iteration 4 — EU + Tokyo open gaps. Sourced from TradingView
    # (Europe Open Levels PRO / markus_schneider7, Xetra Auctions Breakout) +
    # Fazen DAX 08:00-10:00 GMT + FXVPS 14-year DAX ORB study (PF 1.25, Sharpe
    # 1.46, +206R/14y — a REAL but THIN ~0.1R/trade edge, slippage-sensitive)
    # + USDJPY session research (Tokyo first hour highest volatility). NOTE:
    # FR40m is CAC, not DAX — the documented DAX edge may not transfer; the
    # culturing ledger will measure it per symbol. Breakout-based, opt-in.
    "eu_open_orb": SetupDefinition(
        name="eu_open_orb",
        display_name="EU Open ORB (London-Frankfurt)",
        allowed_regimes=("strong_trend", "weak_trend", "expansion", "compression", "transitional"),
        blocked_regimes=("range", "distribution", "volatility_spike"),
        min_confidence=0.55,
        description="EU index opening-range breakout — 08:00-10:00 UTC (London-Frankfurt synchronized open).",
        entry_hints=["08:00-10:00 UTC EU cash open", "Break of first 5-15 min opening range", "Volume confirms"],
        exit_hints=["SL opposite OR boundary (1R)", "TP 2x OR width / trail", "Flat by 13:00 UTC if neither hit"],
    ),
    "tokyo_open_orb": SetupDefinition(
        name="tokyo_open_orb",
        display_name="Tokyo Open ORB",
        allowed_regimes=("strong_trend", "weak_trend", "expansion", "compression", "transitional"),
        blocked_regimes=("range", "distribution", "volatility_spike"),
        min_confidence=0.55,
        description="Tokyo session opening-range breakout — 00:00-02:00 UTC (first hour, highest JPY volatility).",
        entry_hints=["00:00-02:00 UTC Tokyo open first hour", "Break of opening range", "BOJ intervention awareness"],
        exit_hints=["SL opposite OR boundary", "TP 2x OR width", "Reduced size near BOJ intervention levels"],
    ),
    # iteration 5 — London close reversal + Sydney open ORB.
    "london_close_reversal": SetupDefinition(
        name="london_close_reversal",
        display_name="London Close Reversal",
        allowed_regimes=("weak_trend", "transitional", "compression", "range"),
        blocked_regimes=("strong_trend", "expansion", "volatility_spike"),
        min_confidence=0.55,
        description="London Close killzone reversal — 15:00-17:00 UTC rejection at session close (ICT London Close).",
        entry_hints=["15:00-17:00 UTC London close killzone", "Rejection candle at session extreme", "Sweep + CHoCH proxy"],
        exit_hints=["SL beyond sweep extreme", "TP into prior session range", "Time-stop by 17:00 UTC"],
    ),
    "sydney_open_orb": SetupDefinition(
        name="sydney_open_orb",
        display_name="Sydney Open ORB",
        allowed_regimes=("strong_trend", "weak_trend", "expansion", "compression", "transitional"),
        blocked_regimes=("range", "distribution", "volatility_spike"),
        min_confidence=0.55,
        description="Sydney session opening-range breakout — 21:00-23:00 UTC (Asia week-open, AUD leg).",
        entry_hints=["21:00-23:00 UTC Sydney open", "Break of opening range", "Thin pre-Tokyo liquidity — watch spread"],
        exit_hints=["SL opposite OR boundary", "TP 2x OR width", "Reduced size for thin-liquidity spread"],
    ),
    # iteration 6 — London morning breakout + NY lunch reversal.
    "london_morning_breakout": SetupDefinition(
        name="london_morning_breakout",
        display_name="London Morning Breakout",
        allowed_regimes=("strong_trend", "weak_trend", "expansion", "compression", "transitional"),
        blocked_regimes=("range", "distribution", "volatility_spike"),
        min_confidence=0.55,
        description="London morning continuation breakout — 10:00-14:00 UTC (post-open trend into NY).",
        entry_hints=["10:00-14:00 UTC London morning", "Breakout of session range", "Trend into NY open"],
        exit_hints=["SL opposite breakout level", "TP 2R minimum", "Time-stop before 14:00 UTC if stalled"],
    ),
    "ny_lunch_reversal": SetupDefinition(
        name="ny_lunch_reversal",
        display_name="NY Lunch Reversal",
        allowed_regimes=("weak_trend", "transitional", "compression", "range"),
        blocked_regimes=("strong_trend", "expansion", "volatility_spike"),
        min_confidence=0.55,
        description="NY lunch lull reversal — 17:00-19:00 UTC (12:00-14:00 ET, fade of morning extreme).",
        entry_hints=["17:00-19:00 UTC NY lunch lull", "Rejection at morning extreme", "Sweep + revert"],
        exit_hints=["SL beyond sweep extreme", "TP back into morning range", "Time-stop by 19:00 UTC"],
    ),
    # iteration 7 — session-gated BB mean reversion (third entry model).
    "london_bb_reversion": SetupDefinition(
        name="london_bb_reversion",
        display_name="London BB Reversion",
        allowed_regimes=("weak_trend", "transitional", "compression", "range"),
        blocked_regimes=("strong_trend", "expansion", "volatility_spike"),
        min_confidence=0.55,
        description="London session Bollinger-Band mean reversion — 07:00-10:00 UTC (fade overstretched band).",
        entry_hints=["07:00-10:00 UTC London window", "bb_position at band extreme", "Fade back to mean"],
        exit_hints=["SL beyond band", "TP at opposite band / mean", "Time-stop by 10:00 UTC"],
    ),
    "ny_bb_reversion": SetupDefinition(
        name="ny_bb_reversion",
        display_name="NY BB Reversion",
        allowed_regimes=("weak_trend", "transitional", "compression", "range"),
        blocked_regimes=("strong_trend", "expansion", "volatility_spike"),
        min_confidence=0.55,
        description="New York session Bollinger-Band mean reversion — 14:00-17:00 UTC (fade overstretched band).",
        entry_hints=["14:00-17:00 UTC US RTH", "bb_position at band extreme", "Fade back to mean"],
        exit_hints=["SL beyond band", "TP at opposite band / mean", "Time-stop by 17:00 UTC"],
    ),
    # iteration 12 — stochastic-cross momentum continuation (4th entry model).
    "london_stoch_cross": SetupDefinition(
        name="london_stoch_cross",
        display_name="London Stoch Cross",
        allowed_regimes=("trending", "weak_trend", "transitional"),
        blocked_regimes=("range", "volatility_spike"),
        min_confidence=0.55,
        description="London session stochastic %K/%D cross momentum continuation — 07:00-10:00 UTC.",
        entry_hints=["07:00-10:00 UTC London window", "stoch_cross bullish/bearish", "Momentum continuation"],
        exit_hints=["SL beyond recent swing", "TP at 2R or next structure", "Time-stop by 10:00 UTC"],
    ),
    "ny_stoch_cross": SetupDefinition(
        name="ny_stoch_cross",
        display_name="NY Stoch Cross",
        allowed_regimes=("trending", "weak_trend", "transitional"),
        blocked_regimes=("range", "volatility_spike"),
        min_confidence=0.55,
        description="New York session stochastic %K/%D cross momentum continuation — 14:00-17:00 UTC.",
        entry_hints=["14:00-17:00 UTC US RTH", "stoch_cross bullish/bearish", "Momentum continuation"],
        exit_hints=["SL beyond recent swing", "TP at 2R or next structure", "Time-stop by 17:00 UTC"],
    ),
    # iteration 13 — volatility-expansion momentum (5th entry model).
    "london_vol_expansion": SetupDefinition(
        name="london_vol_expansion",
        display_name="London Vol Expansion",
        allowed_regimes=("trending", "expansion", "volatility_spike"),
        blocked_regimes=("range", "compression"),
        min_confidence=0.55,
        description="London session ATR/volatility-expansion momentum continuation — 07:00-10:00 UTC.",
        entry_hints=["07:00-10:00 UTC London window", "volatility_regime == high", "Trade M5-trend direction"],
        exit_hints=["SL beyond expansion base", "TP at 2R or trail ATR", "Time-stop by 10:00 UTC"],
    ),
    "ny_vol_expansion": SetupDefinition(
        name="ny_vol_expansion",
        display_name="NY Vol Expansion",
        allowed_regimes=("trending", "expansion", "volatility_spike"),
        blocked_regimes=("range", "compression"),
        min_confidence=0.55,
        description="New York session ATR/volatility-expansion momentum continuation — 14:00-17:00 UTC.",
        entry_hints=["14:00-17:00 UTC US RTH", "volatility_regime == high", "Trade M5-trend direction"],
        exit_hints=["SL beyond expansion base", "TP at 2R or trail ATR", "Time-stop by 17:00 UTC"],
    ),
    # iteration 14 — volume-spike confirmation (6th entry model).
    "london_volume_spike": SetupDefinition(
        name="london_volume_spike",
        display_name="London Volume Spike",
        allowed_regimes=("trending", "expansion", "volatility_spike"),
        blocked_regimes=("range", "compression"),
        min_confidence=0.55,
        description="London session volume-spike momentum continuation — 07:00-10:00 UTC (volume confirms M5-trend move).",
        entry_hints=["07:00-10:00 UTC London window", "volume_ratio >= 1.5x avg", "Trade M5-trend direction"],
        exit_hints=["SL beyond spike base", "TP at 2R or trail ATR", "Time-stop by 10:00 UTC"],
    ),
    "ny_volume_spike": SetupDefinition(
        name="ny_volume_spike",
        display_name="NY Volume Spike",
        allowed_regimes=("trending", "expansion", "volatility_spike"),
        blocked_regimes=("range", "compression"),
        min_confidence=0.55,
        description="New York session volume-spike momentum continuation — 14:00-17:00 UTC (volume confirms M5-trend move).",
        entry_hints=["14:00-17:00 UTC US RTH", "volume_ratio >= 1.5x avg", "Trade M5-trend direction"],
        exit_hints=["SL beyond spike base", "TP at 2R or trail ATR", "Time-stop by 17:00 UTC"],
    ),
    # iteration 15 — multi-timeframe alignment continuation (7th entry model).
    "london_mtf_align": SetupDefinition(
        name="london_mtf_align",
        display_name="London MTF Align",
        allowed_regimes=("trending", "weak_trend", "expansion", "transitional"),
        blocked_regimes=("range", "volatility_spike"),
        min_confidence=0.55,
        description="London session multi-timeframe-alignment continuation — 07:00-10:00 UTC (M5+M15 EMA20 trends aligned, trade the aligned direction).",
        entry_hints=["07:00-10:00 UTC London window", "timeframe_alignment == True", "Trade aligned M5/M15 direction"],
        exit_hints=["SL beyond recent swing", "TP at 2R or trail ATR", "Time-stop by 10:00 UTC"],
    ),
    "ny_mtf_align": SetupDefinition(
        name="ny_mtf_align",
        display_name="NY MTF Align",
        allowed_regimes=("trending", "weak_trend", "expansion", "transitional"),
        blocked_regimes=("range", "volatility_spike"),
        min_confidence=0.55,
        description="New York session multi-timeframe-alignment continuation — 14:00-17:00 UTC (M5+M15 EMA20 trends aligned, trade the aligned direction).",
        entry_hints=["14:00-17:00 UTC US RTH", "timeframe_alignment == True", "Trade aligned M5/M15 direction"],
        exit_hints=["SL beyond recent swing", "TP at 2R or trail ATR", "Time-stop by 17:00 UTC"],
    ),
    # iteration 16 — stochastic oversold/overbought reversion (8th entry model).
    "london_stoch_reversion": SetupDefinition(
        name="london_stoch_reversion",
        display_name="London Stoch Reversion",
        allowed_regimes=("weak_trend", "transitional", "compression", "range"),
        blocked_regimes=("strong_trend", "expansion", "volatility_spike"),
        min_confidence=0.55,
        description="London session stochastic oversold/overbought mean reversion — 07:00-10:00 UTC (fade stoch %K extreme back to mean).",
        entry_hints=["07:00-10:00 UTC London window", "stoch_k <= 20 oversold / >= 80 overbought", "Fade back to mean"],
        exit_hints=["SL beyond recent swing", "TP at opposite stoch extreme / mean", "Time-stop by 10:00 UTC"],
    ),
    "ny_stoch_reversion": SetupDefinition(
        name="ny_stoch_reversion",
        display_name="NY Stoch Reversion",
        allowed_regimes=("weak_trend", "transitional", "compression", "range"),
        blocked_regimes=("strong_trend", "expansion", "volatility_spike"),
        min_confidence=0.55,
        description="New York session stochastic oversold/overbought mean reversion — 14:00-17:00 UTC (fade stoch %K extreme back to mean).",
        entry_hints=["14:00-17:00 UTC US RTH", "stoch_k <= 20 oversold / >= 80 overbought", "Fade back to mean"],
        exit_hints=["SL beyond recent swing", "TP at opposite stoch extreme / mean", "Time-stop by 17:00 UTC"],
    ),
    # iteration 18 — higher-timeframe-trend-filtered breakout (9th entry model).
    "london_htf_breakout": SetupDefinition(
        name="london_htf_breakout",
        display_name="London HTF Breakout",
        allowed_regimes=("trending", "weak_trend", "expansion", "transitional"),
        blocked_regimes=("range", "volatility_spike"),
        min_confidence=0.55,
        description="London session structural breakout filtered by M15 EMA20-slope trend direction — 07:00-10:00 UTC (breakout up only with M15 bullish, breakdown down only with M15 bearish).",
        entry_hints=["07:00-10:00 UTC London window", "M15 trend direction agrees with breakout", "Close > prior 50-bar resistance / < prior 50-bar support"],
        exit_hints=["SL beyond the broken level", "TP at measured-move / next swing", "Time-stop by 10:00 UTC"],
    ),
    "ny_htf_breakout": SetupDefinition(
        name="ny_htf_breakout",
        display_name="NY HTF Breakout",
        allowed_regimes=("trending", "weak_trend", "expansion", "transitional"),
        blocked_regimes=("range", "volatility_spike"),
        min_confidence=0.55,
        description="New York session structural breakout filtered by M15 EMA20-slope trend direction — 14:00-17:00 UTC (breakout up only with M15 bullish, breakdown down only with M15 bearish).",
        entry_hints=["14:00-17:00 UTC US RTH", "M15 trend direction agrees with breakout", "Close > prior 50-bar resistance / < prior 50-bar support"],
        exit_hints=["SL beyond the broken level", "TP at measured-move / next swing", "Time-stop by 17:00 UTC"],
    ),
    # iteration 19 — BB-squeeze-release breakout (10th entry model).
    "london_squeeze_breakout": SetupDefinition(
        name="london_squeeze_breakout",
        display_name="London BB Squeeze Breakout",
        allowed_regimes=("weak_trend", "transitional", "compression", "range", "trending"),
        blocked_regimes=("volatility_spike",),
        min_confidence=0.55,
        description="London session Bollinger-bandwidth squeeze-release breakout — 07:00-10:00 UTC (breakout that releases a compressed-volatility coil: BB bandwidth in tightest 20% of last 50 bars).",
        entry_hints=["07:00-10:00 UTC London window", "bb_squeeze_pct <= 0.20 (compression coil)", "Breakout up → BUY / breakdown down → SELL (squeeze release)"],
        exit_hints=["SL beyond the broken level / opposite band", "TP at measured-move of the coil", "Time-stop by 10:00 UTC"],
    ),
    "ny_squeeze_breakout": SetupDefinition(
        name="ny_squeeze_breakout",
        display_name="NY BB Squeeze Breakout",
        allowed_regimes=("weak_trend", "transitional", "compression", "range", "trending"),
        blocked_regimes=("volatility_spike",),
        min_confidence=0.55,
        description="New York session Bollinger-bandwidth squeeze-release breakout — 14:00-17:00 UTC (breakout that releases a compressed-volatility coil: BB bandwidth in tightest 20% of last 50 bars).",
        entry_hints=["14:00-17:00 UTC US RTH", "bb_squeeze_pct <= 0.20 (compression coil)", "Breakout up → BUY / breakdown down → SELL (squeeze release)"],
        exit_hints=["SL beyond the broken level / opposite band", "TP at measured-move of the coil", "Time-stop by 17:00 UTC"],
    ),
    # iteration 20 — volume-confirmed breakout (11th entry model).
    "london_volume_breakout": SetupDefinition(
        name="london_volume_breakout",
        display_name="London Volume Breakout",
        allowed_regimes=("trending", "weak_trend", "expansion", "transitional"),
        blocked_regimes=("range", "volatility_spike"),
        min_confidence=0.55,
        description="London session volume-confirmed structural breakout — 07:00-10:00 UTC (close > prior 50-bar resistance with volume_ratio >= 1.5x avg tick-volume).",
        entry_hints=["07:00-10:00 UTC London window", "volume_ratio >= 1.5x avg (volume confirmation)", "Breakout up → BUY / breakdown down → SELL"],
        exit_hints=["SL beyond the broken level", "TP at measured-move / next swing", "Time-stop by 10:00 UTC"],
    ),
    "ny_volume_breakout": SetupDefinition(
        name="ny_volume_breakout",
        display_name="NY Volume Breakout",
        allowed_regimes=("trending", "weak_trend", "expansion", "transitional"),
        blocked_regimes=("range", "volatility_spike"),
        min_confidence=0.55,
        description="New York session volume-confirmed structural breakout — 14:00-17:00 UTC (close > prior 50-bar resistance with volume_ratio >= 1.5x avg tick-volume).",
        entry_hints=["14:00-17:00 UTC US RTH", "volume_ratio >= 1.5x avg (volume confirmation)", "Breakout up → BUY / breakdown down → SELL"],
        exit_hints=["SL beyond the broken level", "TP at measured-move / next swing", "Time-stop by 17:00 UTC"],
    ),
    # iteration 21 — RSI oversold/overbought mean reversion (12th entry model).
    "london_rsi_reversion": SetupDefinition(
        name="london_rsi_reversion",
        display_name="London RSI Reversion",
        allowed_regimes=("weak_trend", "transitional", "compression", "range"),
        blocked_regimes=("strong_trend", "expansion", "volatility_spike"),
        min_confidence=0.55,
        description="London session Wilder's RSI(14) oversold/overbought mean reversion — 07:00-10:00 UTC (fade RSI <= 30 oversold up / RSI >= 70 overbought down back to mean).",
        entry_hints=["07:00-10:00 UTC London window", "rsi <= 30 oversold → BUY / rsi >= 70 overbought → SELL", "Fade back to mean"],
        exit_hints=["SL beyond recent swing", "TP at opposite RSI extreme / mean", "Time-stop by 10:00 UTC"],
    ),
    "ny_rsi_reversion": SetupDefinition(
        name="ny_rsi_reversion",
        display_name="NY RSI Reversion",
        allowed_regimes=("weak_trend", "transitional", "compression", "range"),
        blocked_regimes=("strong_trend", "expansion", "volatility_spike"),
        min_confidence=0.55,
        description="New York session Wilder's RSI(14) oversold/overbought mean reversion — 14:00-17:00 UTC (fade RSI <= 30 oversold up / RSI >= 70 overbought down back to mean).",
        entry_hints=["14:00-17:00 UTC US RTH", "rsi <= 30 oversold → BUY / rsi >= 70 overbought → SELL", "Fade back to mean"],
        exit_hints=["SL beyond recent swing", "TP at opposite RSI extreme / mean", "Time-stop by 17:00 UTC"],
    ),
    # iteration 22 — MACD signal-line-cross momentum continuation (13th entry model).
    "london_macd_cross": SetupDefinition(
        name="london_macd_cross",
        display_name="London MACD Cross",
        allowed_regimes=("trending", "weak_trend", "expansion", "transitional"),
        blocked_regimes=("range", "volatility_spike"),
        min_confidence=0.55,
        description="London session MACD(12,26,9) signal-line-cross momentum continuation — 07:00-10:00 UTC (MACD line crosses above signal → BUY / below signal → SELL).",
        entry_hints=["07:00-10:00 UTC London window", "macd_cross == bullish_cross → BUY / bearish_cross → SELL", "Smoothed-momentum continuation"],
        exit_hints=["SL beyond recent swing", "TP at measured-move / next swing", "Time-stop by 10:00 UTC"],
    ),
    "ny_macd_cross": SetupDefinition(
        name="ny_macd_cross",
        display_name="NY MACD Cross",
        allowed_regimes=("trending", "weak_trend", "expansion", "transitional"),
        blocked_regimes=("range", "volatility_spike"),
        min_confidence=0.55,
        description="New York session MACD(12,26,9) signal-line-cross momentum continuation — 14:00-17:00 UTC (MACD line crosses above signal → BUY / below signal → SELL).",
        entry_hints=["14:00-17:00 UTC US RTH", "macd_cross == bullish_cross → BUY / bearish_cross → SELL", "Smoothed-momentum continuation"],
        exit_hints=["SL beyond recent swing", "TP at measured-move / next swing", "Time-stop by 17:00 UTC"],
    ),
    # iteration 23 — CCI oversold/overbought mean reversion (14th entry model).
    "london_cci_reversion": SetupDefinition(
        name="london_cci_reversion",
        display_name="London CCI Reversion",
        allowed_regimes=("weak_trend", "transitional", "compression", "range"),
        blocked_regimes=("strong_trend", "expansion", "volatility_spike"),
        min_confidence=0.55,
        description="London session CCI(20) oversold/overbought mean reversion — 07:00-10:00 UTC (fade CCI <= -100 oversold up / CCI >= +100 overbought down back to mean).",
        entry_hints=["07:00-10:00 UTC London window", "cci <= -100 oversold → BUY / cci >= +100 overbought → SELL", "Fade back to mean"],
        exit_hints=["SL beyond recent swing", "TP at opposite CCI extreme / mean", "Time-stop by 10:00 UTC"],
    ),
    "ny_cci_reversion": SetupDefinition(
        name="ny_cci_reversion",
        display_name="NY CCI Reversion",
        allowed_regimes=("weak_trend", "transitional", "compression", "range"),
        blocked_regimes=("strong_trend", "expansion", "volatility_spike"),
        min_confidence=0.55,
        description="New York session CCI(20) oversold/overbought mean reversion — 14:00-17:00 UTC (fade CCI <= -100 oversold up / CCI >= +100 overbought down back to mean).",
        entry_hints=["14:00-17:00 UTC US RTH", "cci <= -100 oversold → BUY / cci >= +100 overbought → SELL", "Fade back to mean"],
        exit_hints=["SL beyond recent swing", "TP at opposite CCI extreme / mean", "Time-stop by 17:00 UTC"],
    ),
    # iteration 24 — MFI oversold/overbought mean reversion (15th entry model,
    # volume-weighted oscillator — the only one here that folds in volume).
    "london_mfi_reversion": SetupDefinition(
        name="london_mfi_reversion",
        display_name="London MFI Reversion",
        allowed_regimes=("weak_trend", "transitional", "compression", "range"),
        blocked_regimes=("strong_trend", "expansion", "volatility_spike"),
        min_confidence=0.55,
        description="London session MFI(14) volume-weighted oversold/overbought mean reversion — 07:00-10:00 UTC (fade MFI <= 20 oversold up / MFI >= 80 overbought down back to mean).",
        entry_hints=["07:00-10:00 UTC London window", "mfi <= 20 oversold → BUY / mfi >= 80 overbought → SELL", "Volume-weighted fade back to mean"],
        exit_hints=["SL beyond recent swing", "TP at opposite MFI extreme / mean", "Time-stop by 10:00 UTC"],
    ),
    "ny_mfi_reversion": SetupDefinition(
        name="ny_mfi_reversion",
        display_name="NY MFI Reversion",
        allowed_regimes=("weak_trend", "transitional", "compression", "range"),
        blocked_regimes=("strong_trend", "expansion", "volatility_spike"),
        min_confidence=0.55,
        description="New York session MFI(14) volume-weighted oversold/overbought mean reversion — 14:00-17:00 UTC (fade MFI <= 20 oversold up / MFI >= 80 overbought down back to mean).",
        entry_hints=["14:00-17:00 UTC US RTH", "mfi <= 20 oversold → BUY / mfi >= 80 overbought → SELL", "Volume-weighted fade back to mean"],
        exit_hints=["SL beyond recent swing", "TP at opposite MFI extreme / mean", "Time-stop by 17:00 UTC"],
    ),
    # iteration 25 — ADX trend-strength continuation (16th entry model, the only
    # trend-STRENGTH dimension — directional models measure direction, this
    # measures how strong the trend is via Wilder's DMI).
    "london_adx_trend": SetupDefinition(
        name="london_adx_trend",
        display_name="London ADX Trend",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="London session ADX(14) trend-strength continuation — 07:00-10:00 UTC (ADX >= 25 strong trend + +DI > -DI bullish continuation / -DI > +DI bearish continuation).",
        entry_hints=["07:00-10:00 UTC London window", "adx >= 25 strong trend", "+DI > -DI → BUY / -DI > +DI → SELL (strength-filtered continuation)"],
        exit_hints=["SL beyond recent swing / DI flip", "TP at next swing / trailing", "Time-stop by 10:00 UTC"],
    ),
    "ny_adx_trend": SetupDefinition(
        name="ny_adx_trend",
        display_name="NY ADX Trend",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="New York session ADX(14) trend-strength continuation — 14:00-17:00 UTC (ADX >= 25 strong trend + +DI > -DI bullish continuation / -DI > +DI bearish continuation).",
        entry_hints=["14:00-17:00 UTC US RTH", "adx >= 25 strong trend", "+DI > -DI → BUY / -DI > +DI → SELL (strength-filtered continuation)"],
        exit_hints=["SL beyond recent swing / DI flip", "TP at next swing / trailing", "Time-stop by 17:00 UTC"],
    ),
    "london_obv_cross": SetupDefinition(
        name="london_obv_cross",
        display_name="London OBV Cross",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="London session OBV-EMA(20) cross volume-accumulation continuation — 07:00-10:00 UTC (OBV crosses above its EMA → net accumulation BUY / below → distribution SELL).",
        entry_hints=["07:00-10:00 UTC London window", "OBV crosses its EMA(20)", "bullish cross → BUY (accumulation) / bearish cross → SELL (distribution)"],
        exit_hints=["SL beyond recent swing / OBV re-cross", "TP at next swing / trailing", "Time-stop by 10:00 UTC"],
    ),
    "ny_obv_cross": SetupDefinition(
        name="ny_obv_cross",
        display_name="NY OBV Cross",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="New York session OBV-EMA(20) cross volume-accumulation continuation — 14:00-17:00 UTC (OBV crosses above its EMA → net accumulation BUY / below → distribution SELL).",
        entry_hints=["14:00-17:00 UTC US RTH", "OBV crosses its EMA(20)", "bullish cross → BUY (accumulation) / bearish cross → SELL (distribution)"],
        exit_hints=["SL beyond recent swing / OBV re-cross", "TP at next swing / trailing", "Time-stop by 17:00 UTC"],
    ),
    "london_atr_pct_breakout": SetupDefinition(
        name="london_atr_pct_breakout",
        display_name="London ATR-Pct Breakout",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="London session ATR-percentile-breakout relative-vol expansion — 07:00-10:00 UTC (ATR(14) percentile rank within its 100-bar history >= 0.70 → relative-vol expanding → trade M5 trend direction).",
        entry_hints=["07:00-10:00 UTC London window", "atr_pct >= 0.70 (relative-vol expanding vs own history)", "M5 trend bullish → BUY / bearish → SELL (relative-vol expansion continuation)"],
        exit_hints=["SL beyond recent swing / atr_pct fall below 0.50", "TP at next swing / trailing", "Time-stop by 10:00 UTC"],
    ),
    "ny_atr_pct_breakout": SetupDefinition(
        name="ny_atr_pct_breakout",
        display_name="NY ATR-Pct Breakout",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="New York session ATR-percentile-breakout relative-vol expansion — 14:00-17:00 UTC (ATR(14) percentile rank within its 100-bar history >= 0.70 → relative-vol expanding → trade M5 trend direction).",
        entry_hints=["14:00-17:00 UTC US RTH", "atr_pct >= 0.70 (relative-vol expanding vs own history)", "M5 trend bullish → BUY / bearish → SELL (relative-vol expansion continuation)"],
        exit_hints=["SL beyond recent swing / atr_pct fall below 0.50", "TP at next swing / trailing", "Time-stop by 17:00 UTC"],
    ),
    "london_cmf_continuation": SetupDefinition(
        name="london_cmf_continuation",
        display_name="London CMF Continuation",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="London session Chaikin-Money-Flow-confirmed continuation — 07:00-10:00 UTC (CMF(20) >= +0.10 accumulation + bullish M5 trend → BUY; CMF <= -0.10 distribution + bearish → SELL; intrabar close-location * volume pressure).",
        entry_hints=["07:00-10:00 UTC London window", "CMF(20) >= +0.10 (accumulation, closes near bar highs on rising vol) / <= -0.10 (distribution)", "M5 trend bullish → BUY / bearish → SELL (volume-pressure continuation)"],
        exit_hints=["SL beyond recent swing / CMF flip past zero", "TP at next swing / trailing", "Time-stop by 10:00 UTC"],
    ),
    "ny_cmf_continuation": SetupDefinition(
        name="ny_cmf_continuation",
        display_name="NY CMF Continuation",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="New York session Chaikin-Money-Flow-confirmed continuation — 14:00-17:00 UTC (CMF(20) >= +0.10 accumulation + bullish M5 trend → BUY; CMF <= -0.10 distribution + bearish → SELL; intrabar close-location * volume pressure).",
        entry_hints=["14:00-17:00 UTC US RTH", "CMF(20) >= +0.10 (accumulation, closes near bar highs on rising vol) / <= -0.10 (distribution)", "M5 trend bullish → BUY / bearish → SELL (volume-pressure continuation)"],
        exit_hints=["SL beyond recent swing / CMF flip past zero", "TP at next swing / trailing", "Time-stop by 17:00 UTC"],
    ),
    "london_triple_confirm": SetupDefinition(
        name="london_triple_confirm",
        display_name="London Triple-Confirm",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="London session triple-confirmation momentum continuation — 07:00-10:00 UTC (ADX>=25 strong trend AND ATR-pct>=0.70 relative-vol expanding AND CMF agrees with trend AND M5 directional → trade M5 trend; 3-way confluence of strength + rel-vol-rank + intrabar money-flow pressure).",
        entry_hints=["07:00-10:00 UTC London window", "ADX(14) >= 25 (strong trend) AND ATR-pct >= 0.70 (relative-vol expanding) AND CMF(20) agrees with trend (accumulation+bullish / distribution+bearish)", "M5 trend bullish → BUY / bearish → SELL (triple-confirmation continuation)"],
        exit_hints=["SL beyond recent swing / any of the 3 dims fails", "TP at next swing / trailing", "Time-stop by 10:00 UTC"],
    ),
    "ny_triple_confirm": SetupDefinition(
        name="ny_triple_confirm",
        display_name="NY Triple-Confirm",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="New York session triple-confirmation momentum continuation — 14:00-17:00 UTC (ADX>=25 strong trend AND ATR-pct>=0.70 relative-vol expanding AND CMF agrees with trend AND M5 directional → trade M5 trend; 3-way confluence of strength + rel-vol-rank + intrabar money-flow pressure).",
        entry_hints=["14:00-17:00 UTC US RTH", "ADX(14) >= 25 (strong trend) AND ATR-pct >= 0.70 (relative-vol expanding) AND CMF(20) agrees with trend (accumulation+bullish / distribution+bearish)", "M5 trend bullish → BUY / bearish → SELL (triple-confirmation continuation)"],
        exit_hints=["SL beyond recent swing / any of the 3 dims fails", "TP at next swing / trailing", "Time-stop by 17:00 UTC"],
    ),
    "london_adx_cmf": SetupDefinition(
        name="london_adx_cmf",
        display_name="London ADX+CMF",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="London session ADX+CMF 2-way-compound continuation — 07:00-10:00 UTC (ADX>=25 strong trend AND CMF agrees with trend AND M5 directional → trade M5 trend; drops the ATR-pct gate from the triple to isolate whether strength+money-flow pressure alone carry the EUR/GBP edge).",
        entry_hints=["07:00-10:00 UTC London window", "ADX(14) >= 25 (strong trend) AND CMF(20) agrees with trend (accumulation+bullish / distribution+bearish)", "M5 trend bullish → BUY / bearish → SELL (2-way strength+pressure confluence)"],
        exit_hints=["SL beyond recent swing / ADX falls below 20 or CMF flips past zero", "TP at next swing / trailing", "Time-stop by 10:00 UTC"],
    ),
    "ny_adx_cmf": SetupDefinition(
        name="ny_adx_cmf",
        display_name="NY ADX+CMF",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="New York session ADX+CMF 2-way-compound continuation — 14:00-17:00 UTC (ADX>=25 strong trend AND CMF agrees with trend AND M5 directional → trade M5 trend; drops the ATR-pct gate from the triple to isolate whether strength+money-flow pressure alone carry the EUR/GBP edge).",
        entry_hints=["14:00-17:00 UTC US RTH", "ADX(14) >= 25 (strong trend) AND CMF(20) agrees with trend (accumulation+bullish / distribution+bearish)", "M5 trend bullish → BUY / bearish → SELL (2-way strength+pressure confluence)"],
        exit_hints=["SL beyond recent swing / ADX falls below 20 or CMF flips past zero", "TP at next swing / trailing", "Time-stop by 17:00 UTC"],
    ),
    "london_adx_atr_pct": SetupDefinition(
        name="london_adx_atr_pct",
        display_name="London ADX+ATR-pct",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="London session ADX+ATR-pct 2-way-compound continuation — 07:00-10:00 UTC (ADX>=25 strong trend AND ATR-pct>=0.70 relative-vol expanding AND M5 directional → trade M5 trend; drops the CMF gate from the triple — attribution complement to iter30's adx×cmf, isolates whether strength+vol-rank WITHOUT money-flow pressure carry the EUR/GBP edge).",
        entry_hints=["07:00-10:00 UTC London window", "ADX(14) >= 25 (strong trend) AND ATR(14) percentile rank >= 0.70 (relative-vol expanding)", "M5 trend bullish → BUY / bearish → SELL (2-way strength+vol-rank confluence, no money-flow gate)"],
        exit_hints=["SL beyond recent swing / ADX falls below 20 or ATR-pct rolls back below 0.50", "TP at next swing / trailing", "Time-stop by 10:00 UTC"],
    ),
    "ny_adx_atr_pct": SetupDefinition(
        name="ny_adx_atr_pct",
        display_name="NY ADX+ATR-pct",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="New York session ADX+ATR-pct 2-way-compound continuation — 14:00-17:00 UTC (ADX>=25 strong trend AND ATR-pct>=0.70 relative-vol expanding AND M5 directional → trade M5 trend; drops the CMF gate from the triple — attribution complement to iter30's adx×cmf, isolates whether strength+vol-rank WITHOUT money-flow pressure carry the EUR/GBP NY edge).",
        entry_hints=["14:00-17:00 UTC US RTH", "ADX(14) >= 25 (strong trend) AND ATR(14) percentile rank >= 0.70 (relative-vol expanding)", "M5 trend bullish → BUY / bearish → SELL (2-way strength+vol-rank confluence, no money-flow gate)"],
        exit_hints=["SL beyond recent swing / ADX falls below 20 or ATR-pct rolls back below 0.50", "TP at next swing / trailing", "Time-stop by 17:00 UTC"],
    ),
    "london_atr_pct_cmf": SetupDefinition(
        name="london_atr_pct_cmf",
        display_name="London ATR-pct+CMF",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="London session ATR-pct+CMF 2-way-compound continuation — 07:00-10:00 UTC (ATR-pct>=0.70 relative-vol expanding AND CMF agrees with trend AND M5 directional → trade M5 trend; drops the ADX strength gate from the triple — final compound pair, completes the full 2-way attribution matrix; tests whether vol-rank+money-flow WITHOUT trend-strength carry the EUR/GBP edge).",
        entry_hints=["07:00-10:00 UTC London window", "ATR(14) percentile rank >= 0.70 (relative-vol expanding) AND CMF(20) agrees with trend (accumulation+bullish / distribution+bearish)", "M5 trend bullish → BUY / bearish → SELL (2-way vol-rank+pressure confluence, no strength gate)"],
        exit_hints=["SL beyond recent swing / ATR-pct rolls back below 0.50 or CMF flips past zero", "TP at next swing / trailing", "Time-stop by 10:00 UTC"],
    ),
    "ny_atr_pct_cmf": SetupDefinition(
        name="ny_atr_pct_cmf",
        display_name="NY ATR-pct+CMF",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="New York session ATR-pct+CMF 2-way-compound continuation — 14:00-17:00 UTC (ATR-pct>=0.70 relative-vol expanding AND CMF agrees with trend AND M5 directional → trade M5 trend; drops the ADX strength gate from the triple — final compound pair, completes the full 2-way attribution matrix; tests whether vol-rank+money-flow WITHOUT trend-strength carry the EUR/GBP NY edge).",
        entry_hints=["14:00-17:00 UTC US RTH", "ATR(14) percentile rank >= 0.70 (relative-vol expanding) AND CMF(20) agrees with trend (accumulation+bullish / distribution+bearish)", "M5 trend bullish → BUY / bearish → SELL (2-way vol-rank+pressure confluence, no strength gate)"],
        exit_hints=["SL beyond recent swing / ATR-pct rolls back below 0.50 or CMF flips past zero", "TP at next swing / trailing", "Time-stop by 17:00 UTC"],
    ),
    # iteration 34 — session-gated VWAP-band mean reversion (24th entry model, 5th
    # volume dimension: volume-weighted PRICE anchor, distinct from BB's price-only SMA band).
    "london_vwap_reversion": SetupDefinition(
        name="london_vwap_reversion",
        display_name="London VWAP Reversion",
        allowed_regimes=("weak_trend", "transitional", "compression", "range"),
        blocked_regimes=("strong_trend", "expansion", "volatility_spike"),
        min_confidence=0.55,
        description="London session VWAP-band mean reversion — 07:00-10:00 UTC (fade extension beyond the rolling VWAP+/-1.5sigma band back to the volume-weighted anchor; 24th entry model, 5th volume dim — volume-weighted price anchor vs BB's price-only SMA).",
        entry_hints=["07:00-10:00 UTC London window", "vwap_position >= 0.92 (above upper band) → SELL fade / <= 0.08 (below lower band) → BUY fade", "Fade back to VWAP"],
        exit_hints=["SL beyond VWAP band", "TP at VWAP / opposite band", "Time-stop by 10:00 UTC"],
    ),
    "ny_vwap_reversion": SetupDefinition(
        name="ny_vwap_reversion",
        display_name="NY VWAP Reversion",
        allowed_regimes=("weak_trend", "transitional", "compression", "range"),
        blocked_regimes=("strong_trend", "expansion", "volatility_spike"),
        min_confidence=0.55,
        description="New York session VWAP-band mean reversion — 14:00-17:00 UTC (fade extension beyond the rolling VWAP+/-1.5sigma band back to the volume-weighted anchor; 24th entry model, 5th volume dim — volume-weighted price anchor vs BB's price-only SMA).",
        entry_hints=["14:00-17:00 UTC US RTH", "vwap_position >= 0.92 (above upper band) → SELL fade / <= 0.08 (below lower band) → BUY fade", "Fade back to VWAP"],
        exit_hints=["SL beyond VWAP band", "TP at VWAP / opposite band", "Time-stop by 17:00 UTC"],
    ),
    # iteration 35 — session-gated Supertrend-flip trend continuation (25th entry
    # model; new trend-STATE dimension = ATR-band flip, distinct from EMA direction + ADX strength).
    "london_supertrend_flip": SetupDefinition(
        name="london_supertrend_flip",
        display_name="London Supertrend Flip",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="London session Supertrend-flip trend continuation — 07:00-10:00 UTC (Supertrend(10,3.0) ATR-band direction flip DOWN→UP → BUY / UP→DOWN → SELL; 25th entry model, new trend-STATE dimension — ATR-band flip vs EMA-slope direction + ADX strength).",
        entry_hints=["07:00-10:00 UTC London window", "supertrend_flip bullish_flip → BUY / bearish_flip → SELL (ATR-band trailing-stop trend-state change)", "Continuation in flip direction"],
        exit_hints=["SL beyond recent swing / opposite Supertrend flip", "TP at next swing / trailing", "Time-stop by 10:00 UTC"],
    ),
    "ny_supertrend_flip": SetupDefinition(
        name="ny_supertrend_flip",
        display_name="NY Supertrend Flip",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="New York session Supertrend-flip trend continuation — 14:00-17:00 UTC (Supertrend(10,3.0) ATR-band direction flip DOWN→UP → BUY / UP→DOWN → SELL; 25th entry model, new trend-STATE dimension — ATR-band flip vs EMA-slope direction + ADX strength).",
        entry_hints=["14:00-17:00 UTC US RTH", "supertrend_flip bullish_flip → BUY / bearish_flip → SELL (ATR-band trailing-stop trend-state change)", "Continuation in flip direction"],
        exit_hints=["SL beyond recent swing / opposite Supertrend flip", "TP at next swing / trailing", "Time-stop by 17:00 UTC"],
    ),
    # iteration 36 — session-gated Ichimoku Tenkan/Kijun-cross continuation
    # (26th entry model; new EQUILIBRIUM dimension = rolling high-low midpoint
    # cross confirmed by cloud position, distinct from EMA direction + ADX
    # strength + ATR-band state + VWAP volume-weighted anchor + close-vs-range
    # oscillators).
    "london_ichimoku_tk_cross": SetupDefinition(
        name="london_ichimoku_tk_cross",
        display_name="London Ichimoku TK Cross",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="London session Ichimoku Tenkan/Kijun-cross continuation — 07:00-10:00 UTC (Ichimoku 9/26/52: Tenkan-sen crosses above/below Kijun-sen midpoint equilibrium, confirmed by price above/below the Senkou cloud; 26th entry model, new EQUILIBRIUM dimension — rolling high-low midpoint vs EMA direction + ADX strength + ATR-band state + VWAP anchor).",
        entry_hints=["07:00-10:00 UTC London window", "bullish TK cross (Tenkan over Kijun) + above cloud → BUY / bearish cross + below cloud → SELL", "Cross + inside cloud = skip (no confirmation)"],
        exit_hints=["SL beyond Kijun-sen / opposite side of cloud", "TP at next swing / trailing", "Time-stop by 10:00 UTC"],
    ),
    "ny_ichimoku_tk_cross": SetupDefinition(
        name="ny_ichimoku_tk_cross",
        display_name="NY Ichimoku TK Cross",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="New York session Ichimoku Tenkan/Kijun-cross continuation — 14:00-17:00 UTC (Ichimoku 9/26/52: Tenkan-sen crosses above/below Kijun-sen midpoint equilibrium, confirmed by price above/below the Senkou cloud; 26th entry model, new EQUILIBRIUM dimension — rolling high-low midpoint vs EMA direction + ADX strength + ATR-band state + VWAP anchor).",
        entry_hints=["14:00-17:00 UTC US RTH", "bullish TK cross (Tenkan over Kijun) + above cloud → BUY / bearish cross + below cloud → SELL", "Cross + inside cloud = skip (no confirmation)"],
        exit_hints=["SL beyond Kijun-sen / opposite side of cloud", "TP at next swing / trailing", "Time-stop by 17:00 UTC"],
    ),
    # iteration 37 — session-gated Fair Value Gap imbalance continuation (27th
    # entry model; new PRICE-IMBALANCE dimension = non-adjacent 3-bar ICT/SMC
    # gap gated by ATR-relative size, distinct from all 1-bar-S/R breakouts +
    # continuous-structure setups).
    "london_fvg": SetupDefinition(
        name="london_fvg",
        display_name="London Fair Value Gap",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="London session Fair Value Gap imbalance continuation — 07:00-10:00 UTC (ICT/SMC 3-bar structural gap: bar[i-2].high < bar[i].low bullish / bar[i-2].low > bar[i].high bearish, gap >= 0.25*ATR; 27th entry model, new PRICE-IMBALANCE dimension — non-adjacent 3-bar gap vs all 1-bar-S/R + continuous-structure setups).",
        entry_hints=["07:00-10:00 UTC London window", "bullish_fvg (3-bar upside imbalance) → BUY / bearish_fvg → SELL (continuation in imbalance direction)", "Gap size >= 0.25*ATR required (sub-noise gaps skip)"],
        exit_hints=["SL beyond the FVG zone / opposite FVG", "TP at next swing / trailing", "Time-stop by 10:00 UTC"],
    ),
    "ny_fvg": SetupDefinition(
        name="ny_fvg",
        display_name="NY Fair Value Gap",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="New York session Fair Value Gap imbalance continuation — 14:00-17:00 UTC (ICT/SMC 3-bar structural gap: bar[i-2].high < bar[i].low bullish / bar[i-2].low > bar[i].high bearish, gap >= 0.25*ATR; 27th entry model, new PRICE-IMBALANCE dimension — non-adjacent 3-bar gap vs all 1-bar-S/R + continuous-structure setups).",
        entry_hints=["14:00-17:00 UTC US RTH", "bullish_fvg (3-bar upside imbalance) → BUY / bearish_fvg → SELL (continuation in imbalance direction)", "Gap size >= 0.25*ATR required (sub-noise gaps skip)"],
        exit_hints=["SL beyond the FVG zone / opposite FVG", "TP at next swing / trailing", "Time-stop by 17:00 UTC"],
    ),
    # iteration 38 — session-gated 2-bar Engulfing candlestick reversal (28th
    # entry model; new CANDLE-STRUCTURE dimension = 2-bar body-vs-body engulf,
    # distinct from 1-bar wick rejection + 3-bar FVG gap).
    "london_engulfing": SetupDefinition(
        name="london_engulfing",
        display_name="London Engulfing",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend", "range"),
        blocked_regimes=("volatility_spike", "compression"),
        min_confidence=0.55,
        description="London session 2-bar Engulfing candlestick reversal — 07:00-10:00 UTC (bullish bar engulfs prior bearish body → BUY / bearish bar engulfs prior bullish body → SELL; 28th entry model, new CANDLE-STRUCTURE dimension — 2-bar body-vs-body engulf vs 1-bar wick rejection + 3-bar FVG gap).",
        entry_hints=["07:00-10:00 UTC London window", "bullish_engulfing (bullish bar wraps prior bearish body) → BUY / bearish_engulfing → SELL", "2-bar candle-body reversal"],
        exit_hints=["SL beyond the engulfing bar's extreme", "TP at next swing / trailing", "Time-stop by 10:00 UTC"],
    ),
    "ny_engulfing": SetupDefinition(
        name="ny_engulfing",
        display_name="NY Engulfing",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend", "range"),
        blocked_regimes=("volatility_spike", "compression"),
        min_confidence=0.55,
        description="New York session 2-bar Engulfing candlestick reversal — 14:00-17:00 UTC (bullish bar engulfs prior bearish body → BUY / bearish bar engulfs prior bullish body → SELL; 28th entry model, new CANDLE-STRUCTURE dimension — 2-bar body-vs-body engulf vs 1-bar wick rejection + 3-bar FVG gap).",
        entry_hints=["14:00-17:00 UTC US RTH", "bullish_engulfing (bullish bar wraps prior bearish body) → BUY / bearish_engulfing → SELL", "2-bar candle-body reversal"],
        exit_hints=["SL beyond the engulfing bar's extreme", "TP at next swing / trailing", "Time-stop by 17:00 UTC"],
    ),
    # iteration 39 — session-gated 3-bar Inside-Bar breakout (29th entry
    # model; new CANDLE-STRUCTURE dimension = 2-3-bar range-nesting then
    # expansion, distinct from 1-bar wick rejection + 2-bar body engulf +
    # 3-bar FVG gap).
    "london_inside_bar": SetupDefinition(
        name="london_inside_bar",
        display_name="London Inside-Bar",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend", "range"),
        blocked_regimes=("volatility_spike", "compression"),
        min_confidence=0.55,
        description="London session 3-bar Inside-Bar breakout — 07:00-10:00 UTC (inside bar's range nests within the mother bar, then breakout bar closes above mother high → BUY / below mother low → SELL; 29th entry model, new CANDLE-STRUCTURE dimension — 2-3-bar range-nesting vs 1-bar wick rejection + 2-bar body engulf + 3-bar FVG gap).",
        entry_hints=["07:00-10:00 UTC London window", "bullish_inside_breakout (breakout bar closes above mother high) → BUY / bearish_inside_breakout → SELL", "3-bar volatility-contraction-then-expansion"],
        exit_hints=["SL beyond the mother bar's opposite extreme", "TP at next swing / trailing", "Time-stop by 10:00 UTC"],
    ),
    "ny_inside_bar": SetupDefinition(
        name="ny_inside_bar",
        display_name="NY Inside-Bar",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend", "range"),
        blocked_regimes=("volatility_spike", "compression"),
        min_confidence=0.55,
        description="New York session 3-bar Inside-Bar breakout — 14:00-17:00 UTC (inside bar's range nests within the mother bar, then breakout bar closes above mother high → BUY / below mother low → SELL; 29th entry model, new CANDLE-STRUCTURE dimension — 2-3-bar range-nesting vs 1-bar wick rejection + 2-bar body engulf + 3-bar FVG gap).",
        entry_hints=["14:00-17:00 UTC US RTH", "bullish_inside_breakout (breakout bar closes above mother high) → BUY / bearish_inside_breakout → SELL", "3-bar volatility-contraction-then-expansion"],
        exit_hints=["SL beyond the mother bar's opposite extreme", "TP at next swing / trailing", "Time-stop by 17:00 UTC"],
    ),
    # iteration 40 — session-gated consecutive-close-streak continuation (30th
    # entry model; new STATISTICAL dimension = run-length of consecutive
    # same-direction closes, distinct from EMA-state/ADX-strength/candle-
    # structure/oscillator dimensions).
    "london_close_streak": SetupDefinition(
        name="london_close_streak",
        display_name="London Close-Streak",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="London session consecutive-close-streak continuation — 07:00-10:00 UTC (streak of N>=3 consecutive up-closes → BUY / N>=3 consecutive down-closes → SELL; 30th entry model, new STATISTICAL dimension — run-length of consecutive same-direction closes, distinct from EMA-trend state / ADX strength / candle-structure patterns).",
        entry_hints=["07:00-10:00 UTC London window", "streak >= +3 consecutive up-closes → BUY / streak <= -3 consecutive down-closes → SELL", "run-length momentum continuation"],
        exit_hints=["SL beyond the streak-start swing", "TP at next swing / trailing", "Time-stop by 10:00 UTC"],
    ),
    "ny_close_streak": SetupDefinition(
        name="ny_close_streak",
        display_name="NY Close-Streak",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="New York session consecutive-close-streak continuation — 14:00-17:00 UTC (streak of N>=3 consecutive up-closes → BUY / N>=3 consecutive down-closes → SELL; 30th entry model, new STATISTICAL dimension — run-length of consecutive same-direction closes, distinct from EMA-trend state / ADX strength / candle-structure patterns).",
        entry_hints=["14:00-17:00 UTC US RTH", "streak >= +3 consecutive up-closes → BUY / streak <= -3 consecutive down-closes → SELL", "run-length momentum continuation"],
        exit_hints=["SL beyond the streak-start swing", "TP at next swing / trailing", "Time-stop by 17:00 UTC"],
    ),
    # iteration 41 — session-gated ICT/SMC Order-Block displacement continuation
    # (31st entry model; new CANDLE-STRUCTURE dimension = opposite-origin + impulse-
    # magnitude body >= 0.8*ATR, distinct from body-wrap engulf / range-nesting
    # inside-bar / 3-bar FVG gap / run-length close-streak).
    "london_order_block": SetupDefinition(
        name="london_order_block",
        display_name="London Order Block",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="London session ICT/SMC Order-Block displacement continuation — 07:00-10:00 UTC (prior opposite-colored candle + strong displacement bar body >= 0.8*ATR → trade displacement direction; 31st entry model, new CANDLE-STRUCTURE dimension — opposite-origin + impulse-magnitude, distinct from body-wrap engulf / range-nesting inside-bar / 3-bar FVG gap / run-length close-streak).",
        entry_hints=["07:00-10:00 UTC London window", "bullish_order_block (prior bearish + bullish displacement) → BUY / bearish_order_block → SELL", "ICT/SMC displacement-origin continuation"],
        exit_hints=["SL beyond the order-block origin candle", "TP at next swing / trailing", "Time-stop by 10:00 UTC"],
    ),
    "ny_order_block": SetupDefinition(
        name="ny_order_block",
        display_name="NY Order Block",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="New York session ICT/SMC Order-Block displacement continuation — 14:00-17:00 UTC (prior opposite-colored candle + strong displacement bar body >= 0.8*ATR → trade displacement direction; 31st entry model, new CANDLE-STRUCTURE dimension — opposite-origin + impulse-magnitude, distinct from body-wrap engulf / range-nesting inside-bar / 3-bar FVG gap / run-length close-streak).",
        entry_hints=["14:00-17:00 UTC US RTH", "bullish_order_block (prior bearish + bullish displacement) → BUY / bearish_order_block → SELL", "ICT/SMC displacement-origin continuation"],
        exit_hints=["SL beyond the order-block origin candle", "TP at next swing / trailing", "Time-stop by 17:00 UTC"],
    ),
    # iteration 42 — session-gated Heikin-Ashi smoothed-candle strong-trend
    # continuation (32nd entry model; new PRICE-TRANSFORM family = smoothed OHLC
    # candle wick-absence, distinct from all raw-OHLC entries).
    "london_ha": SetupDefinition(
        name="london_ha",
        display_name="London Heikin-Ashi",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="London session Heikin-Ashi smoothed-candle strong-trend continuation — 07:00-10:00 UTC (green HA candle with no lower wick → BUY / red HA with no upper wick → SELL; 32nd entry model, new PRICE-TRANSFORM family — smoothed OHLC candle wick-absence, distinct from all raw-OHLC candle-structure / oscillator / state / run-length entries).",
        entry_hints=["07:00-10:00 UTC London window", "bullish_ha_strong (green smoothed candle, no lower wick) → BUY / bearish_ha_strong → SELL", "Heikin-Ashi smoothed-trend continuation"],
        exit_hints=["SL beyond the HA candle", "TP at next swing / trailing", "Time-stop by 10:00 UTC"],
    ),
    "ny_ha": SetupDefinition(
        name="ny_ha",
        display_name="NY Heikin-Ashi",
        allowed_regimes=("trending", "strong_trend", "expansion", "transitional", "weak_trend"),
        blocked_regimes=("range", "volatility_spike", "compression"),
        min_confidence=0.55,
        description="New York session Heikin-Ashi smoothed-candle strong-trend continuation — 14:00-17:00 UTC (green HA candle with no lower wick → BUY / red HA with no upper wick → SELL; 32nd entry model, new PRICE-TRANSFORM family — smoothed OHLC candle wick-absence, distinct from all raw-OHLC candle-structure / oscillator / state / run-length entries).",
        entry_hints=["14:00-17:00 UTC US RTH", "bullish_ha_strong (green smoothed candle, no lower wick) → BUY / bearish_ha_strong → SELL", "Heikin-Ashi smoothed-trend continuation"],
        exit_hints=["SL beyond the HA candle", "TP at next swing / trailing", "Time-stop by 17:00 UTC"],
    ),
}


def get_setup(name: str) -> SetupDefinition | None:
    return SETUP_LIBRARY.get(name)


def is_regime_compatible(setup_type: str, primary_regime: str) -> bool:
    defn = get_setup(setup_type)
    if not defn:
        return True
    if primary_regime in defn.blocked_regimes:
        return False
    if defn.allowed_regimes and primary_regime not in defn.allowed_regimes:
        return False
    return True


def enrich_setup_stats(
    setup_type: str,
    edge_scores: dict[str, Any],
    symbol: str | None = None,
) -> dict[str, Any]:
    """Attach memory stats to a setup definition."""
    defn = get_setup(setup_type)
    stats_src = edge_scores.get("setup_stats", {})
    if symbol and symbol in stats_src.get("by_symbol", {}):
        s = stats_src["by_symbol"][symbol].get(setup_type, {})
    else:
        s = stats_src.get("global", {}).get(setup_type, {})
    return {
        "name": setup_type,
        "display_name": defn.display_name if defn else setup_type,
        "description": defn.description if defn else "",
        "win_rate_pct": s.get("win_rate_pct", 0),
        "total": s.get("total", 0),
        "wins": s.get("wins", 0),
        "losses": s.get("losses", 0),
        "entry_hints": defn.entry_hints if defn else [],
        "exit_hints": defn.exit_hints if defn else [],
    }


def list_setups() -> list[dict[str, Any]]:
    return [
        {
            "name": d.name,
            "display_name": d.display_name,
            "allowed_regimes": list(d.allowed_regimes),
            "blocked_regimes": list(d.blocked_regimes),
            "min_confidence": d.min_confidence,
        }
        for d in SETUP_LIBRARY.values()
    ]