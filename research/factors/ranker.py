"""Cross-sectional research ranker models."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

_LABEL = "__label__"


def _training_frame(X: pd.DataFrame, y: pd.Series) -> pd.DataFrame:
    """Return complete, aligned training data without depending on ``y.name``."""
    return X.dropna().join(y.dropna().rename(_LABEL), how="inner")


class LinearRanker:
    """Regularised linear regression ranker."""

    def __init__(self, alpha: float = 1.0, l1_ratio: float = 0.5):
        self.alpha = alpha
        self.l1_ratio = l1_ratio
        self._coef_: dict[str, float] = {}
        self._intercept_: float = 0.0
        self.is_fitted: bool = False

    def fit(self, X: pd.DataFrame, y: pd.Series) -> LinearRanker:
        from sklearn.linear_model import ElasticNet

        clean = _training_frame(X, y)
        self.is_fitted = False
        if len(clean) < 20:
            return self
        model = ElasticNet(alpha=self.alpha, l1_ratio=self.l1_ratio, max_iter=2000)
        model.fit(clean[X.columns], clean[_LABEL])
        self._coef_ = dict(zip(X.columns, model.coef_, strict=False))
        self._intercept_ = float(model.intercept_)
        self.is_fitted = True
        return self

    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        if not self.is_fitted:
            return pd.DataFrame(
                {"rank_score": 0.0, "expected_net_r": 0.0}, index=X.index
            )
        predictions = pd.Series(self._intercept_, index=X.index, dtype=float)
        for column, coefficient in self._coef_.items():
            if column in X.columns:
                predictions += X[column].fillna(0.0) * coefficient
        return pd.DataFrame(
            {
                "rank_score": predictions.rank(pct=True),
                "expected_net_r": predictions,
            },
            index=X.index,
        )


class LightGBMRanker:
    """LightGBM ranker with feature-importance reporting."""

    def __init__(
        self,
        n_estimators: int = 200,
        max_depth: int = 5,
        learning_rate: float = 0.05,
        seed: int = 42,
    ):
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.learning_rate = learning_rate
        self.seed = seed
        self._model: Any = None
        self._feature_importance_: dict[str, float] = {}
        self.is_fitted: bool = False

    def fit(self, X: pd.DataFrame, y: pd.Series) -> LightGBMRanker:
        self.is_fitted = False
        try:
            import lightgbm as lgb
        except ImportError:
            return self
        clean = _training_frame(X, y)
        if len(clean) < 30:
            return self
        model = lgb.LGBMRegressor(
            n_estimators=self.n_estimators,
            max_depth=self.max_depth,
            learning_rate=self.learning_rate,
            random_state=self.seed,
            verbose=-1,
        )
        model.fit(clean[X.columns], clean[_LABEL])
        self._model = model
        self._feature_importance_ = dict(
            zip(X.columns, model.feature_importances_, strict=False)
        )
        self.is_fitted = True
        return self

    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        if not self.is_fitted or self._model is None:
            return pd.DataFrame(
                {
                    "rank_score": 0.0,
                    "expected_net_r": 0.0,
                    "probability_positive": 0.5,
                },
                index=X.index,
            )
        predictions = pd.Series(
            self._model.predict(X.fillna(0.0)), index=X.index, dtype=float
        )
        return pd.DataFrame(
            {
                "rank_score": predictions.rank(pct=True),
                "expected_net_r": predictions,
                "probability_positive": 1.0 / (1.0 + np.exp(-predictions)),
            },
            index=X.index,
        )


def random_forest_ranker(
    X: pd.DataFrame,
    y: pd.Series,
    *,
    n_estimators: int = 100,
    max_depth: int = 7,
    seed: int = 42,
) -> dict[str, Any]:
    """Fit a random-forest ranker and return index-aligned predictions."""
    from sklearn.ensemble import RandomForestRegressor

    clean = _training_frame(X, y)
    if len(clean) < 30:
        return {
            "predictions": pd.DataFrame(index=X.index),
            "importances": {},
            "oob_score": None,
            "is_fitted": False,
        }
    model = RandomForestRegressor(
        n_estimators=n_estimators,
        max_depth=max_depth,
        random_state=seed,
        oob_score=True,
        n_jobs=-1,
    )
    model.fit(clean[X.columns], clean[_LABEL])
    predictions = pd.Series(model.predict(X.fillna(0.0)), index=X.index, dtype=float)
    return {
        "predictions": pd.DataFrame(
            {
                "rank_score": predictions.rank(pct=True),
                "expected_net_r": predictions,
            },
            index=X.index,
        ),
        "importances": dict(
            zip(X.columns, model.feature_importances_, strict=False)
        ),
        "oob_score": float(model.oob_score_),
        "is_fitted": True,
    }
