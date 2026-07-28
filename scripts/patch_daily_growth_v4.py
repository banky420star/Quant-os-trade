# Patch core/daily_growth.py: add source-level hard-disable for daily-loss kill switch.
import os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "mt5_quant_agent", "core", "daily_growth.py")
src = open(TARGET, encoding="utf-8").read()

# Strip prior broken insertions
prior1 = re.compile(r"\n    # 2026-07-28 hotfix: source-level hard-disable.*?_safe_no_pause_state\(equity, config\)\n", re.DOTALL)
prior2 = re.compile(r"\n\ndef _safe_no_pause_state\(.*?\n    return state\n", re.DOTALL)
for rx in (prior1, prior2):
    new = rx.sub("", src)
    if new != src:
        print("stripped prior bad insertion")
        src = new

# Locate evaluate_daily_growth
m = re.search(r"def evaluate_daily_growth\(", src)
if not m:
    print("def evaluate_daily_growth not found - abort")
    sys.exit(2)
start = m.start()

# Find docstring close: skip past the first two triple-quote pairs (we're already past 'def', so
# the first triple-quote is the docstring opener; the second is the docstring closer).
q1 = src.find('"""', start)
q2 = src.find('"""', q1 + 3) if q1 != -1 else -1
if q1 == -1 or q2 == -1:
    print("docstring not found - abort")
    sys.exit(2)
insert_pos = q2 + 3
print("function starts at byte", start, "docstring closes at byte", insert_pos)

guard_lines = [
    "",
    "",
    "    # 2026-07-28 hotfix: source-level HARD-DISABLE for the daily-loss kill",
    "    # switch. When practice.force_disable_max_loss_pause: true is set in any",
    "    # config layer, evaluate_daily_growth() never sets trading_paused=True",
    "    # or writes a pause_reason. This patch is at the source so it survives",
    "    # config.yaml/profile merge semantics and any hot-reload gap.",
    "    if bool(",
    "        (config or {}).get('practice', {}).get(",
    "            'force_disable_max_loss_pause', False",
    "        )",
    "    ):",
    "        return _safe_no_pause_state(equity, config, existing=existing)",
]
guard = "\n".join(guard_lines)
new_src = src[:insert_pos] + guard + src[insert_pos:]

# Append _safe_no_pause_state helper if not present
if "_safe_no_pause_state" not in new_src:
    helper = []
    helper.append("")
    helper.append("")
    helper.append("def _safe_no_pause_state(")
    helper.append("    equity: float,")
    helper.append("    config: dict[str, Any],")
    helper.append("    *,")
    helper.append("    existing: dict[str, Any] | None = None,")
    helper.append(") -> dict[str, Any]:")
    helper.append("    # 2026-07-28 source-level hard-disable return shape.")
    helper.append("    if existing:")
    helper.append("        state = dict(existing)")
    helper.append("    else:")
    helper.append("        state = dict(sync_daily_session(equity, config))")
    helper.append("    state['trading_paused'] = False")
    helper.append("    state['pause_reason'] = None")
    helper.append("    state['updated_at'] = utc_now_iso()")
    helper.append("    return state")
    helper.append("")
    new_src = new_src.rstrip() + "\n" + "\n".join(helper)
    print("appended helper")

open(TARGET, "w", encoding="utf-8").write(new_src)
print("wrote", TARGET)

# Compile check
import py_compile
try:
    py_compile.compile(TARGET, doraise=True)
    print("PY_COMPILE: OK")
except py_compile.PyCompileError as e:
    print("PY_COMPILE FAILED:", e)
    lines = open(TARGET, encoding="utf-8").read().split("\n")
    for i in range(119, min(len(lines), 220)):
        print(f"  {i + 1}: {lines[i]}")
    sys.exit(2)
