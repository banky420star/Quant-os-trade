from __future__ import annotations

import pandas as pd
import pytest

from research.validation.feature_ablation import (
    AblationGroup,
    FeatureAblationLab,
    fingerprint_frame,
)


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trend": [1.0, 2.0, 3.0, 4.0],
            "momentum": [0.2, 0.1, -0.1, -0.2],
            "volume": [10.0, 12.0, 8.0, 9.0],
        },
        index=pd.date_range("2026-01-01", periods=4, freq="min", tz="UTC"),
    )


def test_frame_fingerprint_is_stable_and_column_order_sensitive():
    frame = _frame()
    assert fingerprint_frame(frame) == fingerprint_frame(frame.copy(deep=True))
    assert fingerprint_frame(frame) != fingerprint_frame(frame[["volume", "trend", "momentum"]])


def test_named_ablation_changes_declared_columns_and_preserves_shape():
    frame = _frame()
    lab = FeatureAblationLab(
        frame,
        [AblationGroup.from_columns("NO_TREND", ["trend"])],
    )
    variant = lab.variant("NO_TREND")
    assert variant.shape == frame.shape
    assert list(variant.columns) == list(frame.columns)
    assert (variant["trend"] == 0.0).all()
    assert variant["momentum"].equals(frame["momentum"])
    assert fingerprint_frame(variant) != lab.control_fingerprint


def test_missing_or_duplicate_feature_groups_fail_closed():
    frame = _frame()
    with pytest.raises(KeyError, match="missing columns"):
        FeatureAblationLab(
            frame,
            [AblationGroup.from_columns("BROKEN", ["not_a_feature"])],
        )
    with pytest.raises(ValueError, match="duplicate ablation group"):
        FeatureAblationLab(
            frame,
            [
                AblationGroup.from_columns("NO_TREND", ["trend"]),
                AblationGroup.from_columns("NO_TREND", ["momentum"]),
            ],
        )


def test_noop_ablation_detects_broken_mapping_or_fill_choice():
    frame = _frame()
    frame["already_zero"] = 0.0
    lab = FeatureAblationLab(
        frame,
        [AblationGroup.from_columns("NOOP", ["already_zero"], fill_value=0.0)],
    )
    with pytest.raises(AssertionError, match="did not change the feature matrix"):
        lab.variant("NOOP")


def test_report_uses_same_evaluator_and_records_metric_deltas():
    frame = _frame()
    lab = FeatureAblationLab(
        frame,
        [
            AblationGroup.from_columns("NO_TREND", ["trend"]),
            AblationGroup.from_columns("NO_VOLUME", ["volume"]),
        ],
    )
    calls: list[str] = []

    def evaluator(matrix: pd.DataFrame, name: str) -> dict:
        calls.append(name)
        return {
            "score": float(matrix.sum().sum()),
            "rows": len(matrix),
            "label": name,
        }

    report = lab.run(evaluator)
    assert calls == ["CONTROL", "NO_TREND", "NO_VOLUME"]
    assert report.shadow_only is True
    assert report.execution_authority_granted is False
    assert len(report.evidence_hash) == 64
    assert all(item.changed_from_control for item in report.variants)
    trend = next(item for item in report.variants if item.name == "NO_TREND")
    assert trend.metric_deltas["score"] < 0
    assert trend.metric_deltas["rows"] == 0


def test_evaluator_mutation_cannot_change_stored_control():
    frame = _frame()
    original = fingerprint_frame(frame)
    lab = FeatureAblationLab(
        frame,
        [AblationGroup.from_columns("NO_TREND", ["trend"])],
    )

    def mutating_evaluator(matrix: pd.DataFrame, _name: str) -> dict:
        matrix.iloc[:, :] = 999.0
        return {"score": 1.0}

    lab.run(mutating_evaluator)
    assert lab.control_fingerprint == original
    assert fingerprint_frame(frame) == original
