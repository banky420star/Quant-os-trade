"""Strategy artifact JSON schema and validation.

Per the roadmap, every strategy that reaches the live plane must carry
a versioned artifact describing its training cutoff, symbols, feature
hash, cost model, and out-of-sample metrics.
"""

from __future__ import annotations

import json
from typing import Any

ARTIFACT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "StrategyArtifact",
    "type": "object",
    "required": [
        "strategy_id",
        "version",
        "training_cutoff",
        "symbols",
        "feature_schema_hash",
        "cost_model_version",
        "out_of_sample_metrics",
        "allowed_profiles",
    ],
    "properties": {
        "strategy_id": {
            "type": "string",
            "description": "Unique identifier, e.g. 'tsmom_blend_v1'",
            "pattern": "^[a-z][a-z0-9_]+$",
        },
        "version": {
            "type": "string",
            "description": "Semantic version, e.g. '1.0.0'",
            "pattern": r"^\d+\.\d+\.\d+$",
        },
        "training_cutoff": {
            "type": "string",
            "format": "date",
            "description": "ISO date of last training observation",
        },
        "symbols": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
        },
        "feature_schema_hash": {
            "type": "string",
            "description": "SHA-256 of the feature definition",
        },
        "cost_model_version": {
            "type": "string",
            "description": "Version tag of the cost model used",
        },
        "out_of_sample_metrics": {
            "type": "object",
            "required": ["net_expectancy", "sharpe", "max_drawdown", "profit_factor", "n_folds"],
            "properties": {
                "net_expectancy": {"type": "number"},
                "sharpe": {"type": "number"},
                "max_drawdown": {"type": "number"},
                "profit_factor": {"type": "number"},
                "n_folds": {"type": "integer", "minimum": 1},
                "median_fold_return": {"type": "number"},
                "positive_fold_fraction": {"type": "number"},
                "dsr": {"type": "number"},
            },
        },
        "allowed_profiles": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Profiles that may run this artifact (e.g. ['shadow', 'demo_conservative'])",
        },
        "expires_at": {
            "type": "string",
            "format": "date",
            "description": "Optional expiry after which the artifact is invalid",
        },
        "notes": {"type": "string"},
    },
}


def validate_artifact(artifact: dict[str, Any]) -> list[str]:
    """
    Validate an artifact dictionary against the required schema fields and key constraints.
    
    Parameters:
    	artifact (dict[str, Any]): The artifact data to validate.
    
    Returns:
    	list[str]: Validation error messages; an empty list indicates no detected errors.
    """
    errors: list[str] = []

    for field in ARTIFACT_SCHEMA.get("required", []):
        if field not in artifact:
            errors.append(f"missing required field: {field}")

    if "strategy_id" in artifact:
        import re
        if not re.match(r"^[a-z][a-z0-9_]+$", str(artifact["strategy_id"])):
            errors.append("strategy_id must match [a-z][a-z0-9_]+")

    if "symbols" in artifact:
        syms = artifact["symbols"]
        if not isinstance(syms, list) or len(syms) == 0:
            errors.append("symbols must be a non-empty list")

    if "allowed_profiles" in artifact:
        profs = artifact.get("allowed_profiles", [])
        if not isinstance(profs, list) or len(profs) == 0:
            errors.append("allowed_profiles must be a non-empty list")

    return errors
