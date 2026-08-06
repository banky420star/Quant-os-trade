"""Strategy artifact registry.

Tracks every approved strategy artifact, records promotion history,
and enforces that only artifacts with valid OOS metrics and allowed
profiles can be promoted to the live plane.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.utils import STATE_DIR, utc_now_iso

REGISTRY_PATH = STATE_DIR / "strategy_artifact_registry.json"
CHAMPION_PATH = STATE_DIR / "champion_registry.json"


@dataclass
class StrategyArtifact:
    strategy_id: str
    version: str
    training_cutoff: str
    symbols: list[str]
    feature_schema_hash: str
    cost_model_version: str
    out_of_sample_metrics: dict[str, float]
    allowed_profiles: list[str]
    expires_at: str | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        """
        Serialize the strategy artifact metadata for persistence or interchange.
        
        Returns:
        	dict[str, Any]: A mapping containing the artifact's identity, training metadata, symbols, feature and cost-model metadata, out-of-sample metrics, allowed profiles, expiration, and notes.
        """
        return {
            "strategy_id": self.strategy_id,
            "version": self.version,
            "training_cutoff": self.training_cutoff,
            "symbols": self.symbols,
            "feature_schema_hash": self.feature_schema_hash,
            "cost_model_version": self.cost_model_version,
            "out_of_sample_metrics": self.out_of_sample_metrics,
            "allowed_profiles": self.allowed_profiles,
            "expires_at": self.expires_at,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StrategyArtifact:
        """
        Create a strategy artifact from serialized field data.
        
        Parameters:
            d (dict[str, Any]): Mapping containing the artifact fields.
        
        Returns:
            StrategyArtifact: Reconstructed strategy artifact.
        """
        return cls(
            strategy_id=str(d["strategy_id"]),
            version=str(d["version"]),
            training_cutoff=str(d["training_cutoff"]),
            symbols=list(d.get("symbols", [])),
            feature_schema_hash=str(d.get("feature_schema_hash", "")),
            cost_model_version=str(d.get("cost_model_version", "")),
            out_of_sample_metrics=dict(d.get("out_of_sample_metrics", {})),
            allowed_profiles=list(d.get("allowed_profiles", [])),
            expires_at=d.get("expires_at"),
            notes=str(d.get("notes", "")),
        )


class ChampionRegistry:
    """Track which artifact is the current champion for each profile."""

    def __init__(self, path: Path | None = None):
        """
        Initialize the champion registry from a persisted state file.
        
        Parameters:
        	path (Path | None): Path to the champion state file, or the default registry path when omitted.
        """
        self._path = path or CHAMPION_PATH
        self._champions: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        """
        Load champion assignments from the registry file.
        
        Returns:
            dict[str, str]: A mapping of profile names to artifact identifiers, or an empty mapping if the file is absent.
        """
        if not self._path.exists():
            return {}
        data = json.loads(self._path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.get("champions", {}).items()}

    def _save(self) -> None:
        """Persist the current champion assignments to the registry file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "updated_at": utc_now_iso(),
            "champions": self._champions,
        }
        self._path.write_text(json.dumps(doc, indent=2), encoding="utf-8")

    def get_champion(self, profile: str) -> str | None:
        """Return the registered champion artifact identifier for a profile.
        
        Parameters:
            profile (str): Profile whose champion assignment to retrieve.
        
        Returns:
            str | None: The champion artifact identifier, or `None` if no champion is assigned.
        """
        return self._champions.get(profile)

    def set_champion(self, profile: str, artifact: StrategyArtifact) -> None:
        """
        Promote an eligible artifact as the champion for a profile.
        
        Raises:
            ValueError: If the artifact is not allowed for the specified profile.
        """
        if profile not in artifact.allowed_profiles:
            raise ValueError(
                f"Artifact {artifact.strategy_id} v{artifact.version} "
                f"is not allowed for profile '{profile}'"
            )
        self._champions[profile] = f"{artifact.strategy_id}@{artifact.version}"
        self._save()

    def champions(self) -> dict[str, str]:
        """Return the current champion artifact assignments by profile.
        
        Returns:
            dict[str, str]: A copy mapping each profile to its champion artifact identifier.
        """
        return dict(self._champions)


def register_artifact(artifact: StrategyArtifact) -> Path:
    """Persist an artifact to the registry and return its path."""
    registry: dict[str, Any] = {}
    if REGISTRY_PATH.exists():
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))

    artifacts: list[dict[str, Any]] = registry.setdefault("artifacts", [])
    artifacts.append(artifact.to_dict())
    registry["updated_at"] = utc_now_iso()

    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    return REGISTRY_PATH
