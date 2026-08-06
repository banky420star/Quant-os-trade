"""Artifact registry — versioned strategy artifacts and JSON schema."""

from .registry import ChampionRegistry, StrategyArtifact
from .schema import ARTIFACT_SCHEMA, validate_artifact

__all__ = [
    "ARTIFACT_SCHEMA",
    "ChampionRegistry",
    "StrategyArtifact",
    "validate_artifact",
]
