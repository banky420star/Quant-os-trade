# qc_crosscheck/main.py
# Independent cross-check strategy ported to QuantConnect's Lean engine.
# region imports
from AlgorithmImports import *
# endregion
#
# GOAL
#   A SECOND backtest engine (QC/Lean, zero shared code with core/paper_broker.py
#   or scripts/independent_backtest_check.py) for the exit-mechanism hypothesis:
#       "Do break-even + trailing exits lift per-trade R off the floor, or is the
#        bot's reported +0.49R for `medium` a close-to-close backtest artifact?"
#   See memory: exit-model-bias-found-2026-06-27. The from-scratch pandas engine
#   (independent_backtest_check.py) already answered this INTRABAR on the same
#   window: medium = -0.028R vs off = +0.175R -> the exit edge is an artifact.
#   This QC algo is the THIRD engine, kept here for whenever QC infra is available.
#
# CORRECTNESS vs the previous version
#   The old main.py checked `price <= self.sl` against the bar CLOSE in on_data ->
#   it cloned the bot's close-to-close exit bias, so it could NOT cross-check the
#   intrabar finding. This version uses QC NATIVE stop / take-profit orders, which
#   Lean fills INTRABAR when the touch price is reached (the whole point of an
#   independent engine). BE + trailing are implemented by updating the live stop
#   order ticket each bar, exactly as a real broker would.
#
# DATA CAVEAT
#   Uses OANDA XAUUSD minute bars (QC's own data), NOT the bot's local M5/M15
#   parquet. So this is a DIFFERENT-SAMPLE corroboration, not a same-data cross.
#   For a same-data cross, feed the parquet via add_data() (TODO if Docker+login
#   land). Different data is still useful: if the exit artifact disappears on a
#   different sample too, the finding is robust.
#
# RUNNING (BLOCKED on user credentials -- Docker is NOT required for the cloud path)
#   Cloud path (preferred -- no Docker):
#     1. Get your QuantConnect user id + API token from quantconnect.com -> Account.
#     2. `lean login --user-id <UID> --api-token <TOK>`  (non-interactive; agent can
#        run this if you paste the credentials, or you run it yourself via `! `).
#     3. Run BOTH exit variants (the whole point of this cross-check is medium vs off):
#          lean cloud backtest qc_crosscheck --push --name qc_medium --parameter exits medium
#          lean cloud backtest qc_crosscheck --push --name qc_off    --parameter exits off
#        Compare per-trade R / win rate. If medium <= off (or both negative) on this
#        THIRD independent sample, the exit artifact (see
#        exit-model-bias-found-2026-06-27) is robust across engines + data.
#   Local path (optional, needs Docker):
#     1. Install Docker Desktop.
#     2. `lean login` (interactive) or the non-interactive form above.
#     3. `lean backtest qc_crosscheck --parameter exits medium` (and `... off`).
#   NO LIVE TRADING. The algo only backtests on QC's historical XAUUSD data.


class EMACrossATRExitCrossCheck(QCAlgorithm):
    def initialize(self):
        self.set_start_date(2025, 12, 9)
        self.set_end_date(2026, 1, 4)
        self.set_cash(100000)
        # Exit mode is a backtest PARAMETER so a single codebase can run both the
        # `medium` (BE+trail) and `off` (raw 2R TP / 1.5*ATR SL, no management) cells
        # for a like-for-like cross-check. Set via `--parameter exits medium|off`.
        self.exit_mode = (self.get_parameter("exits") or "medium").lower()
        if self.exit_mode not in ("medium", "off"):
            raise ValueError(f"exits parameter must be 'medium' or 'off', got {self.exit_mode!r}")
        self.symbol = self.add_forex("XAUUSD", Resolution.MINUTE, Market.OANDA).symbol
        self.set_benchmark(self.symbol)
        self.ema = self.ema(self.symbol, 20, Resolution.MINUTE)
        self.atr = self.atr(self.symbol, 14, MovingAverageType.SIMPLE, Resolution.MINUTE)
        self.ema_prev = None
        self.qty = 0
        self.dir = 0
        self.entry = None
        self.sl_price = None
        self.tp_price = None
        self.peak = None
        self.be_done = False
        self.trail_done = False
        self.sl_ticket = None     # live StopMarketOrder ticket
        self.tp_ticket = None     # live TakeProfit limit ticket
        self.cost_r = 0.15        # informational only; QC fills are gross
        self.debug(f"exit_mode={self.exit_mode}")

    # ---- helpers -----------------------------------------------------------
    def _atr_val(self):
        return float(self.atr.current.value)

    def _reset(self):
        # cancel any live exit orders before clearing state
        if self.sl_ticket is not None:
            self.sl_ticket.cancel()
        if self.tp_ticket is not None:
            self.tp_ticket.cancel()
        self.sl_ticket = self.tp_ticket = None
        self.dir = 0
        self.entry = self.sl_price = self.tp_price = self.peak = None
        self.be_done = self.trail_done = False

    def _update_stop(self, price: float) -> None:
        # QC's pythonnet binding exposes OrderTicket stop-update methods as either
        # `update_stop_price` (snake_case) or `UpdateStopPrice` (PascalCase)
        # depending on the installed QuantConnect version. Try both so the algo
        # doesn't hard-crash on an AttributeError the first time BE/trail fires.
        t = self.sl_ticket
        for attr in ("update_stop_price", "UpdateStopPrice"):
            fn = getattr(t, attr, None)
            if callable(fn):
                fn(price)
                return
        self.debug(f"could not update stop ticket (no update_stop_price/UpdateStopPrice on {type(t).__name__})")

    # ---- main loop ---------------------------------------------------------
    def on_data(self, data):
        if not self.atr.is_ready or not self.ema.is_ready:
            return
        if self.symbol not in data or data[self.symbol].close is None:
            return
        price = float(data[self.symbol].close)
        atr_val = self._atr_val()
        ema_val = float(self.ema.current.value)
        slope = (ema_val - self.ema_prev) if self.ema_prev is not None else 0.0
        self.ema_prev = ema_val

        # If the stop or TP already filled, QC auto-liquidates and invested flips
        # to False. Clean up stale tickets + state BEFORE the management block (the
        # old code nested this check inside the `invested` guard, making it
        # unreachable and leaving tickets/state stale until the next entry).
        if self.dir != 0 and not self.portfolio[self.symbol].invested:
            self._reset()
            return

        # in position -> manage exits via live stop/ticket updates (intrabar fills)
        if self.dir != 0 and self.portfolio[self.symbol].invested:
            # `off` mode: no management -- let the static 2R TP / 1.5*ATR SL ride to
            # fill (the baseline `off` cell). Only `medium` does BE + trailing.
            if self.exit_mode != "medium":
                return
            # update favourable peak
            if self.dir == 1:
                self.peak = max(self.peak, price)
            else:
                self.peak = min(self.peak, price)
            fav = (price - self.entry) * self.dir

            # break-even: once +0.50*ATR favourable, lock 0.10*ATR
            if not self.be_done and fav >= 0.50 * atr_val:
                self.sl_price = self.entry + self.dir * 0.10 * atr_val
                self.be_done = True
                if self.sl_ticket is not None:
                    self._update_stop(self.sl_price)

            # trailing arming
            if not self.trail_done and fav >= 0.75 * atr_val:
                self.trail_done = True

            # trailing: peak - 0.35*ATR, only ratchet favourable
            if self.trail_done:
                new_sl = self.peak - self.dir * 0.35 * atr_val
                if (self.dir == 1 and new_sl > self.sl_price) or \
                   (self.dir == -1 and new_sl < self.sl_price):
                    self.sl_price = new_sl
                    if self.sl_ticket is not None:
                        self._update_stop(self.sl_price)

            # once trailing is live, cancel the static TP (matches bot logic:
            # TP only valid pre-BE/pre-trail; after that, let the trail manage).
            if self.trail_done and self.tp_ticket is not None:
                self.tp_ticket.cancel()
                self.tp_ticket = None
            return

        # no position -> look for entry on slope sign
        if atr_val <= 0:
            return
        if slope > 0:
            d = 1
        elif slope < 0:
            d = -1
        else:
            return

        # market entry (small fixed size, ~10% notional)
        self.set_holdings(self.symbol, 0.1 * d)
        fill = float(self.securities[self.symbol].price)
        self.dir = d
        self.entry = fill
        sl_dist = 1.5 * atr_val
        self.sl_price = fill - d * sl_dist
        self.tp_price = fill + d * 2.0 * sl_dist   # 2R
        self.peak = fill
        self.be_done = False
        self.trail_done = False
        # qty signed so exit orders are negative of position
        self.qty = float(self.portfolio[self.symbol].quantity)
        exit_qty = -self.qty
        # INTRABAR stop + take-profit: Lean fills these when the touch price is
        # reached inside a bar, not only on closes -- the core correctness fix.
        self.sl_ticket = self.stop_market_order(self.symbol, exit_qty, self.sl_price)
        # take-profit as a limit order in the exit direction
        self.tp_ticket = self.limit_order(self.symbol, exit_qty, self.tp_price)