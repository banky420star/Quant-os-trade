"""State-write atomicity proofs (Agent 6, 2026-08-08).

write_json_state() must NEVER expose a half-written / truncated / mixed JSON
file to a reader. Contract:

    serialize -> write .tmp -> flush -> os.replace (atomic), per-file lock,
    Windows-safe retries, and NO non-atomic fallback write to the target.

If every replace attempt fails, the function fails CLOSED: the last valid
state stays on disk, the stale .tmp is removed, and None is returned. This is
a deliberate regression guard against the pre-2026-08-08 fallback that wrote
the target directly after 10 failed replaces — at exactly the failure moment
where a reader could observe partially written JSON.
"""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import utils


@pytest.fixture(autouse=True)
def _hermetic_state(tmp_path, monkeypatch):
    """Redirect STATE_DIR to tmp_path, kill the retry backoff so the
    failure-path test is fast, and mute the expected failure log."""
    monkeypatch.setattr(utils, "STATE_DIR", tmp_path)
    monkeypatch.setattr(utils.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(logging.getLogger("utils"), "level", logging.CRITICAL)
    yield


def test_round_trip_and_no_tmp_debris(tmp_path):
    path = utils.write_json_state("abc.json", {"a": 1})
    assert path is not None and path == tmp_path / "abc.json"
    assert utils.read_json_state("abc.json") == {"a": 1}
    assert not list(tmp_path.glob("*.tmp")), "no temp files may remain after a write"


def test_nested_path_creates_parents(tmp_path):
    path = utils.write_json_state("nested/deep/state.json", {"x": [1, 2]})
    assert path is not None
    assert path.exists()
    assert utils.read_json_state("nested/deep/state.json") == {"x": [1, 2]}
    assert not list(tmp_path.glob("*.tmp"))


def test_overwrite_replaces_content_atomically(tmp_path):
    utils.write_json_state("over.json", {"v": "old"})
    path = utils.write_json_state("over.json", {"v": "new"})
    assert path is not None
    assert utils.read_json_state("over.json") == {"v": "new"}
    assert not list(tmp_path.glob("*.tmp"))


def test_concurrent_writers_leave_valid_json():
    """The per-file lock serializes writers: N threads hammering the same file
    must leave valid JSON (last writer wins), never interleaved or corrupt."""
    utils.write_json_state("conc.json", {"seed": 0})
    errors: list[BaseException] = []

    def _writer(i: int) -> None:
        try:
            for round_no in range(25):
                utils.write_json_state("conc.json", {"seed": i, "round": round_no})
        except BaseException as exc:  # pragma: no cover - failure capture
            errors.append(exc)

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    doc = utils.read_json_state("conc.json")
    assert isinstance(doc, dict) and "seed" in doc and "round" in doc
    assert not list(utils.STATE_DIR.glob("*.tmp"))


def test_replace_failure_never_writes_partial_target(monkeypatch):
    """Bug 3 regression: when os.replace keeps failing, write_json_state must
    NOT fall back to a direct (non-atomic) write of the target. The previous
    valid state is preserved, no .tmp debris remains, and None is returned."""
    utils.write_json_state("guarded.json", {"v": "previous-valid"})

    def _boom(*args, **kwargs):
        raise OSError(13, "Permission denied", str(args[-1]) if args else "")

    monkeypatch.setattr(utils.os, "replace", _boom)
    result = utils.write_json_state("guarded.json", {"v": "new"})
    assert result is None, "a persistent replace failure must fail closed (None)"
    assert utils.read_json_state("guarded.json") == {"v": "previous-valid"}, (
        "target must keep the last valid state — never a partial overwrite"
    )
    assert not list(utils.STATE_DIR.glob("*.tmp")), "stale temp must be cleaned up"


def test_target_never_replaced_when_write_fails_then_recovers(monkeypatch):
    """A transient replace failure window must leave the OLD valid content
    readable; once the window passes, the next write succeeds atomically."""
    utils.write_json_state("flap.json", {"v": "old"})
    original_replace = utils.os.replace
    # Longer than the 10-attempt retry window so the FIRST write exhausts its
    # retries and fails closed; the SECOND write then recovers mid-window.
    state = {"failures_left": 12}

    def _flaky_replace(src, dst):
        if state["failures_left"] > 0:
            state["failures_left"] -= 1
            raise OSError(32, "Sharing violation", str(dst))
        return original_replace(src, dst)

    monkeypatch.setattr(utils.os, "replace", _flaky_replace)
    first = utils.write_json_state("flap.json", {"v": "mid"})
    assert first is None
    # The reader must never have seen partial content.
    assert utils.read_json_state("flap.json") == {"v": "old"}
    second = utils.write_json_state("flap.json", {"v": "final"})
    assert second is not None
    assert utils.read_json_state("flap.json") == {"v": "final"}
    assert not list(utils.STATE_DIR.glob("*.tmp"))
