"""Manually deploy adaptive weight candidates after reviewing research output."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.adaptive_weights import AdaptiveWeightOptimizer
from core.utils import read_json_state


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy weight candidate to adaptive_weights.json")
    parser.add_argument(
        "--file",
        default="weight_candidates.json",
        help="State file with candidate weights (default: weight_candidates.json)",
    )
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    candidate = read_json_state(args.file, default={})
    if candidate.get("status") != "candidate" and not candidate.get("weights"):
        print(f"No deployable candidate in {args.file}")
        return 1

    weights = candidate.get("weights", {})
    print("Candidate weights:")
    for k, v in sorted(weights.items()):
        print(f"  {k}: {v:.4f}")

    proposal = candidate.get("proposal", "unknown")
    improvement = candidate.get("replay_improvement_pct")
    print(f"\nProposal: {proposal}")
    if improvement is not None:
        print(f"Replay improvement: {improvement}%")

    if not args.yes:
        answer = input("\nDeploy these weights? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 0

    deployed = AdaptiveWeightOptimizer.deploy_weights(candidate)
    print(f"Deployed to adaptive_weights.json at {deployed['timestamp']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())