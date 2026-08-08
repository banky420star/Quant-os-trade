"""Fingerprint-verified feature ablation for Quant OS research.

The lab preserves matrix shape by masking named feature groups.  Every variant
is fingerprinted before evaluation and a declared ablation fails immediately
when it does not actually change the input matrix.  This prevents the class of
silent column-index mistakes found during the legacy super-lamp audit.

This module is research-only.  It does not import broker, execution, profile,
or dashboard-control code.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from core.model_governance import fingerprint_payload


@dataclass(frozen=True)
class AblationGroup:
    name: str
    columns: tuple[str, ...]
    fill_value: float = 0.0
    description: str = ""

    @classmethod
    def from_columns(
        cls,
        name: str,
        columns: Sequence[str],
        *,
        fill_value: float = 0.0,
        description: str = "",
    ) -> "AblationGroup":
        return cls(
            name=str(name),
            columns=tuple(str(column) for column in columns),
            fill_value=float(fill_value),
            description=str(description),
        )


@dataclass(frozen=True)
class AblationVariantResult:
    name: str
    columns: tuple[str, ...]
    fingerprint: str
    changed_from_control: bool
    metrics: Mapping[str, Any]
    metric_deltas: Mapping[str, float]
    fill_value: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AblationReport:
    control_fingerprint: str
    control_metrics: Mapping[str, Any]
    variants: tuple[AblationVariantResult, ...]
    evidence_hash: str
    shadow_only: bool = True
    execution_authority_granted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def fingerprint_frame(frame: pd.DataFrame) -> str:
    """Hash values, index, column order, dtypes and shape deterministically."""
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("fingerprint_frame expects a pandas DataFrame")
    metadata = {
        "shape": list(frame.shape),
        "columns": [str(column) for column in frame.columns],
        "dtypes": [str(dtype) for dtype in frame.dtypes],
        "index_name": str(frame.index.name or ""),
        "index_type": type(frame.index).__name__,
    }
    value_hashes = pd.util.hash_pandas_object(frame, index=True).to_numpy()
    digest = hashlib.sha256()
    digest.update(fingerprint_payload(metadata).encode("ascii"))
    digest.update(value_hashes.tobytes())
    return digest.hexdigest()


class FeatureAblationLab:
    """Create and evaluate shape-preserving, named feature ablations."""

    CONTROL_NAME = "CONTROL"

    def __init__(self, frame: pd.DataFrame, groups: Sequence[AblationGroup]) -> None:
        if not isinstance(frame, pd.DataFrame):
            raise TypeError("frame must be a pandas DataFrame")
        if frame.empty:
            raise ValueError("feature frame must not be empty")
        if not frame.columns.is_unique:
            raise ValueError("feature frame columns must be unique")

        self._control = frame.copy(deep=True)
        self._control_fingerprint = fingerprint_frame(self._control)
        self._groups: dict[str, AblationGroup] = {}
        for group in groups:
            name = str(group.name or "").strip()
            if not name or name == self.CONTROL_NAME:
                raise ValueError(f"invalid ablation group name: {group.name!r}")
            if name in self._groups:
                raise ValueError(f"duplicate ablation group: {name}")
            if not group.columns:
                raise ValueError(f"ablation group has no columns: {name}")
            missing = [column for column in group.columns if column not in self._control.columns]
            if missing:
                raise KeyError(f"ablation group {name} missing columns: {missing}")
            self._groups[name] = group

    @property
    def control_fingerprint(self) -> str:
        return self._control_fingerprint

    def variant(self, name: str) -> pd.DataFrame:
        if name == self.CONTROL_NAME:
            return self._control.copy(deep=True)
        try:
            group = self._groups[name]
        except KeyError as exc:
            raise KeyError(f"unknown ablation group: {name}") from exc
        variant = self._control.copy(deep=True)
        variant.loc[:, list(group.columns)] = group.fill_value
        fingerprint = fingerprint_frame(variant)
        if fingerprint == self._control_fingerprint:
            raise AssertionError(
                f"ablation {name} did not change the feature matrix; "
                "check column mapping and fill values"
            )
        return variant

    @staticmethod
    def _numeric_deltas(
        control: Mapping[str, Any], variant: Mapping[str, Any]
    ) -> dict[str, float]:
        deltas: dict[str, float] = {}
        for key, value in variant.items():
            baseline = control.get(key)
            if isinstance(value, bool) or isinstance(baseline, bool):
                continue
            if isinstance(value, (int, float)) and isinstance(baseline, (int, float)):
                deltas[str(key)] = float(value) - float(baseline)
        return deltas

    def run(
        self,
        evaluator: Callable[[pd.DataFrame, str], Mapping[str, Any]],
    ) -> AblationReport:
        """Evaluate control and every named variant through the same callable."""
        if not callable(evaluator):
            raise TypeError("evaluator must be callable")

        control_before = fingerprint_frame(self._control)
        control_metrics = dict(evaluator(self._control.copy(deep=True), self.CONTROL_NAME))
        results: list[AblationVariantResult] = []

        for name, group in self._groups.items():
            variant = self.variant(name)
            variant_fingerprint = fingerprint_frame(variant)
            metrics = dict(evaluator(variant.copy(deep=True), name))
            results.append(
                AblationVariantResult(
                    name=name,
                    columns=group.columns,
                    fingerprint=variant_fingerprint,
                    changed_from_control=variant_fingerprint != self._control_fingerprint,
                    metrics=metrics,
                    metric_deltas=self._numeric_deltas(control_metrics, metrics),
                    fill_value=group.fill_value,
                )
            )

        # An evaluator must not be able to mutate the stored control matrix.
        if fingerprint_frame(self._control) != control_before:
            raise RuntimeError("ablation evaluator mutated the control feature matrix")

        evidence_payload = {
            "control_fingerprint": self._control_fingerprint,
            "control_metrics": control_metrics,
            "groups": [
                {
                    "group": asdict(self._groups[result.name]),
                    "result": result.to_dict(),
                }
                for result in results
            ],
            "shadow_only": True,
            "execution_authority_granted": False,
        }
        return AblationReport(
            control_fingerprint=self._control_fingerprint,
            control_metrics=control_metrics,
            variants=tuple(results),
            evidence_hash=fingerprint_payload(evidence_payload),
        )


__all__ = [
    "AblationGroup",
    "AblationReport",
    "AblationVariantResult",
    "FeatureAblationLab",
    "fingerprint_frame",
]
