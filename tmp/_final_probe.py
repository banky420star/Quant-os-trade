import json, sys, urllib.request
sys.stdout.reconfigure(encoding="utf-8")
try:
    d = json.loads(urllib.request.urlopen("http://127.0.0.1:8081/api/state", timeout=6).read())
except Exception as e:
    print("probe_err:", type(e).__name__, str(e)[:120])
    sys.exit(0)

rt = d.get("runtime_mode", {})
ts = d.get("trading_status", {})
lp = d.get("live_portfolio", {})
cs = d.get("candidate_signals", {})
appr = d.get("approved_signals", {})
cf = d.get("config", {})

print("runtime.label:", rt.get("label"))
print("runtime.detail[:160]:", str(rt.get("detail", ""))[:160])
print("trading_status:", ts.get("status"), " can_execute=", ts.get("can_execute"),
      " reason=", ts.get("block_reason") or ts.get("reason"))
print("equity=", lp.get("equity"), " balance=", lp.get("balance"),
      " open_positions=", len(lp.get("open_positions") or []))
print("candidates=", cs.get("count"), " approved=", appr.get("count"))

ex = cf.get("execution") or {}
print("exec.mode=", ex.get("mode"), " live=", ex.get("live_trading_enabled"),
      " mt5=", ex.get("mt5_trading_enabled"), " allow_live=", ex.get("allow_live_account"))

syms = cf.get("symbols") or []
print("symbols_n=", len(syms), " btc_in=", "BTCUSDm" in syms,
      " first_5=", syms[:5])

# Print which symbols survived.
pm = cf.get("practice", {}).get("micro", {})
print("practice.micro.symbols_n=", len(pm.get("symbols") or []),
      " btc_in_micro=", "BTCUSDm" in (pm.get("symbols") or []))

risk = cf.get("risk", {})
print("risk.max_drawdown_pct=", risk.get("max_drawdown_pct"),
      " max_daily_loss_pct=", risk.get("max_daily_loss_pct"),
      " max_consecutive_losses=", risk.get("max_consecutive_losses"),
      " max_loss_per_trade_usd=", pm.get("max_loss_per_trade_usd"))
