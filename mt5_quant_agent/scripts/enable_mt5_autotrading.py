"""Enable MT5 algorithmic trading for Python API via Options dialog."""

from __future__ import annotations

import sys
import time

try:
    from pywinauto import Application
except ImportError:
    print("pywinauto not installed — run: pip install pywinauto")
    sys.exit(1)


def _set_checkbox(dlg, title: str, desired: bool) -> None:
    cb = dlg.child_window(title=title, class_name="Button")
    state = cb.get_check_state()  # 0=unchecked, 1=checked, 2=indeterminate
    checked = state == 1
    if checked != desired:
        cb.click_input()
        time.sleep(0.2)


def enable_autotrading(title_re: str = r".*Exness-MT5Trial9.*") -> bool:
    app = Application(backend="win32").connect(title_re=title_re, timeout=10)
    main = app.window(title_re=title_re)
    main.set_focus()

    # Options -> Expert Advisors (Ctrl+O)
    main.type_keys("^o")
    time.sleep(1.0)
    dlg = app.window(title="Options")
    dlg.set_focus()
    dlg.child_window(class_name="SysTabControl32").select("Expert Advisors")
    time.sleep(0.3)

    _set_checkbox(dlg, "Allow algorithmic trading", True)
    _set_checkbox(dlg, "Disable algorithmic trading via external Python API", False)
    _set_checkbox(dlg, "Allow DLL imports (potentially dangerous, enable only for trusted applications)", True)
    dlg.child_window(title="OK", class_name="Button").click_input()
    time.sleep(0.5)

    # Toolbar "Algo Trading" button (separate from Options checkboxes)
    main.set_focus()
    tb = main.child_window(title="Standard", class_name="ToolbarWindow32")
    rect = tb.rectangle()
    tb.click_input(coords=(rect.width() - 80, (rect.bottom - rect.top) // 2))
    time.sleep(0.5)
    return True


if __name__ == "__main__":
    enable_autotrading()
    print("MT5 autotrading options applied — verify with terminal_info().trade_allowed")