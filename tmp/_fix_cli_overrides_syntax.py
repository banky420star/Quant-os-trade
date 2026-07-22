"""
Iteratively fix concatenated-statement and IndentationError syntax issues
in mt5_quant_agent/dashboard/server.py, then run the smoke test.

Strategy per iteration:
1. ast.parse(content) — if OK, stop.
2. If SyntaxError: try to find a 2-statement concat on the failing line and split.
3. If IndentationError: the failing line is one indent level too shallow because
   the previous line was glued onto it. Split the GLUED earlier line.
4. Save and re-iterate. Hard cap at 100 iterations.
"""
import ast
import re
import sys
import os

sys.stdout.reconfigure(encoding="utf-8")

p = "dashboard/server.py"
abs_p = os.path.abspath(p)
content = open(abs_p, "r", encoding="utf-8").read()
log = []


def save():
    open(abs_p, "w", encoding="utf-8").write(content)


def try_parse():
    try:
        ast.parse(content)
        return "OK", None
    except (SyntaxError, IndentationError) as exc:
        return "ERR", exc


def fix_syntax_error(text):
    try:
        ast.parse(text)
        return text, None
    except (SyntaxError, IndentationError) as exc:
        lines = text.splitlines()
        ln = exc.lineno - 1
        bad_line = lines[ln]

        # Pattern 1: two statements concatenated (e.g. `now = time.time()    cache_key = "..."`)
        m = re.search(
            r"(\b\w+\s*=[^=]+?|\)|\b\]\)|\])\s+([a-zA-Z_]\w*\s*[=:(]|\w+\s*\.|\bif |\bfor |\bwhile |\bdef |\bclass |\btry|\bwith |\breturn|\braise|\byield|\bprint\b)",
            bad_line,
        )
        if m:
            cut = m.start(2)
            new_line = bad_line[:cut] + "\n" + bad_line[cut:]
            lines[ln] = new_line
            return "\n".join(lines) + "\n", f"split {bad_line[:cut]!r} | {bad_line[cut:]!r} at ln {ln+1}"

        # Pattern 2: IndentationError — the line above was glued.
        if ln >= 1:
            prev = lines[ln - 1]
            m2 = re.search(r"(?:\)|\b\]\)|\]|=)\s+([a-zA-Z_]\w*\s*[=:(]|if |for |while |def |class |try|with |return|raise|yield)", prev)
            if m2:
                cut = m2.start(1)
                lines[ln - 1] = prev[:cut] + "\n" + prev[cut:]
                return "\n".join(lines) + "\n", f"split prev {prev[:cut]!r} | {prev[cut:]!r} at ln {ln}"

        return text, f"NO FIX FOUND at ln {ln+1}: {bad_line!r}"


# Iterate up to 100 times
for i in range(100):
    status, exc = try_parse()
    if status == "OK":
        log.append(f"[{i}] parse OK")
        break
    log.append(f"[{i}] {type(exc).__name__} at ln {exc.lineno}: {exc.msg[:80]} | text={exc.text[:80]!r}")
    content, msg = fix_syntax_error(content)
    log.append(f"    FIX: {msg}")
    if "NO FIX FOUND" in msg:
        break
    save()

print("\n".join(log))
print("---")
status, exc = try_parse()
print(f"FINAL: {status}")
if exc is not None and status == "ERR":
    print(f"  remaining: ln {exc.lineno} {exc.msg[:200]}")
    print(f"  text: {exc.text!r}")
    # Show 6 lines of context
    lines = content.splitlines()
    ln = exc.lineno - 1
    for j in range(max(0, ln - 3), min(len(lines), ln + 5)):
        marker = " <<<" if j == ln else ""
        print(f"    {j+1:>5}: {lines[j]}{marker}")
save()
