from __future__ import annotations

from types import SimpleNamespace

from loops import mt5_audit_loop as audit


class _Connection:
    def __init__(self, config, logger):
        self.connected = False

    def connect(self):
        self.connected = True
        return True

    def disconnect(self):
        self.connected = False


def _config(tmp_path):
    return {
        "mt5_audit_loop": {
            "enabled": True,
            "history_days": 30,
            "max_recent_rows": 10,
            "journal_files": 2,
            "journal_tail_lines": 100,
        },
        "filters": {"avoid_news": False},
        "news": {"macro_awareness": False},
        "_journal_root": str(tmp_path),
    }


def test_deal_and_order_rows_normalize_timestamps():
    deal = SimpleNamespace(
        ticket=11, order=12, position_id=13, symbol="XAUUSDm", type=0,
        entry=1, reason=2, volume=0.01, price=2400.5, profit=1.25,
        commission=-0.1, swap=0.0, time=0,
    )
    order = SimpleNamespace(
        ticket=21, order=22, position_id=23, symbol="USOILm", type=1,
        state=2, reason=3, volume_initial=0.02, volume_current=0.0,
        price_open=80.2, time_setup=0, time_done=1,
    )
    assert audit._deal_row(deal)["symbol"] == "XAUUSDm"
    assert audit._deal_row(deal)["time"].endswith("+00:00")
    assert audit._order_row(order)["state"] == 2
    assert audit._order_row(order)["time_done"].endswith("+00:00")


def test_summarize_journal_is_bounded_and_redacts_sensitive_values(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "today.log").write_text(
        "order rejected: login=436469641 password=secret\n"
        "normal terminal connected\n",
        encoding="utf-8",
    )
    report = audit.summarize_journal(tmp_path, max_files=1, tail_lines=10)
    assert report["counts"]["error_or_rejection"] == 1
    assert report["counts"]["trade_related"] == 1
    assert "secret" not in report["samples"][0]
    assert "436469641" not in report["samples"][0]


def test_run_is_read_only_and_writes_audit_state(monkeypatch, tmp_path):
    writes = {}
    journal_dir = tmp_path / "logs"
    journal_dir.mkdir()
    (journal_dir / "today.log").write_text("order rejected: invalid stops\n", encoding="utf-8")

    class FakeMT5:
        def __init__(self):
            self.order_send_called = False
            self.order_check_called = False
            self.terminal = SimpleNamespace(
                connected=True, trade_allowed=True, build=6061,
                data_path=str(tmp_path),
            )
            self.account = SimpleNamespace(
                balance=101.6, equity=101.6, profit=0.0,
                currency="USD", trade_allowed=True, trade_expert=True,
                server="demo", trade_mode=0,
            )

        def account_info(self):
            return self.account

        def terminal_info(self):
            return self.terminal

        def positions_get(self):
            return []

        def orders_get(self):
            return []

        def history_deals_get(self, *_args):
            return [SimpleNamespace(
                ticket=1, order=2, position_id=3, symbol="XAUUSDm", type=0,
                entry=0, reason=0, volume=0.01, price=2400.0, profit=2.0,
                commission=0.0, swap=0.0, time=0,
            )]

        def history_orders_get(self, *_args):
            return []

        def order_send(self, *_args, **_kwargs):
            self.order_send_called = True
            raise AssertionError("read-only audit must not call order_send")

        def order_check(self, *_args, **_kwargs):
            self.order_check_called = True
            raise AssertionError("read-only audit must not call order_check")

    fake = FakeMT5()
    monkeypatch.setattr(audit, "mt5", fake)
    monkeypatch.setattr(audit, "MT5ConnectionManager", _Connection)
    monkeypatch.setattr(audit, "write_json_state", lambda name, data: writes.setdefault(name, data))
    monkeypatch.setattr(audit, "ensure_dirs", lambda: None)
    monkeypatch.setattr(audit, "setup_logger", lambda *args, **kwargs: SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
    ))

    result = audit.run(_config(tmp_path))
    assert result["mt5_audit_loop"] == "OK"
    assert writes["mt5_audit.json"]["read_only"] is True
    assert writes["mt5_audit.json"]["counts"]["deals"] == 1
    assert writes["mt5_calendar.json"]["source"] == "constructed_local_calendar"
    assert fake.order_send_called is False
    assert fake.order_check_called is False


def test_history_api_none_is_reported_as_unavailable(monkeypatch):
    writes = {}

    class FakeMT5:
        def account_info(self):
            return SimpleNamespace(balance=100.0, equity=100.0, trade_mode=0)

        def terminal_info(self):
            return SimpleNamespace(connected=True, trade_allowed=True, build=1, data_path=None)

        def positions_get(self):
            return []

        def orders_get(self):
            return []

        def history_deals_get(self, *_args):
            return None

        def history_orders_get(self, *_args):
            return []

        def last_error(self):
            return (-10005, "IPC timeout")

    monkeypatch.setattr(audit, "mt5", FakeMT5())
    monkeypatch.setattr(audit, "write_json_state", lambda name, data: writes.setdefault(name, data))
    monkeypatch.setattr(audit, "ensure_dirs", lambda: None)
    monkeypatch.setattr(audit, "setup_logger", lambda *args, **kwargs: SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
    ))
    monkeypatch.setattr(audit, "MT5ConnectionManager", _Connection)

    result = audit.run({"mt5_audit_loop": {"enabled": True}})
    assert result == {"mt5_audit_loop": "OK"}
    assert writes["mt5_audit.json"]["status"] == "unavailable"
    assert "history_deals_get failed" in writes["mt5_audit.json"]["error"]


def test_already_connected_session_is_not_disconnected(monkeypatch):
    writes = {}

    class FakeMT5:
        def account_info(self):
            return SimpleNamespace(balance=100.0, equity=100.0, trade_mode=0)

        def terminal_info(self):
            return SimpleNamespace(connected=True, trade_allowed=True, build=1, data_path=None)

        def positions_get(self):
            return []

        def orders_get(self):
            return []

        def history_deals_get(self, *_args):
            return []

        def history_orders_get(self, *_args):
            return []

    class MustNotConnect(_Connection):
        def connect(self):
            raise AssertionError("audit must reuse the existing MT5 session")

        def disconnect(self):
            raise AssertionError("audit must not disconnect the existing MT5 session")

    monkeypatch.setattr(audit, "mt5", FakeMT5())
    monkeypatch.setattr(audit, "MT5ConnectionManager", MustNotConnect)
    monkeypatch.setattr(audit, "write_json_state", lambda name, data: writes.setdefault(name, data))
    monkeypatch.setattr(audit, "ensure_dirs", lambda: None)
    monkeypatch.setattr(audit, "setup_logger", lambda *args, **kwargs: SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
    ))

    result = audit.run({"mt5_audit_loop": {"enabled": True}})
    assert result == {"mt5_audit_loop": "OK"}
    assert writes["mt5_audit.json"]["status"] == "ok"


def test_run_failure_writes_unavailable_report(monkeypatch):
    writes = {}

    class BrokenConnection(_Connection):
        def connect(self):
            raise ConnectionError("terminal unavailable")

    monkeypatch.setattr(audit, "MT5ConnectionManager", BrokenConnection)
    monkeypatch.setattr(audit, "mt5", SimpleNamespace())
    monkeypatch.setattr(audit, "write_json_state", lambda name, data: writes.setdefault(name, data))
    monkeypatch.setattr(audit, "ensure_dirs", lambda: None)
    monkeypatch.setattr(audit, "setup_logger", lambda *args, **kwargs: SimpleNamespace(
        info=lambda *a, **k: None, warning=lambda *a, **k: None,
    ))

    result = audit.run({"mt5_audit_loop": {"enabled": True}})
    assert result["mt5_audit_loop"] == "OK"
    assert writes["mt5_audit.json"]["status"] == "unavailable"
    assert writes["mt5_audit.json"]["orders_submitted"] == 0
