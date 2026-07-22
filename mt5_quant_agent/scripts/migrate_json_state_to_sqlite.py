"""One-shot / repeatable import of JSON state mirrors into SQLite."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.state_store import get_state_store
from core.utils import load_config


def run_migration(*, seed: bool = True) -> int:
    config = load_config()
    store = get_state_store(config)
    if store is None:
        print("state_store.enabled is false — nothing to migrate")
        return 0

    ok, msg = store.health_check()
    if not ok:
        print(f"Database health check failed: {msg}")
        return 1
    print(f"Database: {msg}")

    if not seed:
        print("Schema ready (no JSON seed requested)")
        return 0

    counts = store.seed_from_json()
    print("Migrated:")
    for key, n in counts.items():
        print(f"  {key}: {n}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="Initialize schema without importing JSON mirrors",
    )
    args = parser.parse_args()
    return run_migration(seed=not args.schema_only)


if __name__ == "__main__":
    raise SystemExit(main())