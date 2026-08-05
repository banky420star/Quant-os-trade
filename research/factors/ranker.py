"""Cross-sectional ranker models.

Per the roadmap, the ranker's role is to decide which valid trend
signals deserve capital — it never creates stops, overrides minimum
RR, bypasses the queue, or submits orders.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


class LinearRanker:
    """Regularised linear regression ranker.

    Simple, inspectable baseline.  Outputs a ``rank_score`` and
    ``expected_net_r`` for each candidate.
    """

    def __init__(self, alpha: float = 1.0, l1_ratio: float = 0.5):
        self.alpha = alpha
        self.l1_ratio = l1_ratio
        self._coef_: dict[str, float] = {}
        self._intercept_: float = 0.0
        self._fitted_: bool = False

    def fit(self, X: pd.DataFrame, y: pd.Series) -> LinearRanker:
        """Fit an ElasticNet model.  *X* has feature columns, *y* is the label."""
        from sklearn.linear_model import ElasticNet

        clean = X.dropna().join(y.dropna(), how="inner")
        if len(clean) < 20:
            return self

        model = ElasticNet(alpha=self.alpha, l1_ratio=self.l1_ratio, max_iter=2000)
        model.fit(clean[X.columns], clean[y.name])
        self._coef_ = dict(zip(X.columns, model.coef_))
        self._intercept_ = float(model.intercept_)
        self._fitted_ = True
        return self

    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        """Return DataFrame with ``rank_score`` and ``expected_net_r``."""
        if not self._fitted_:
            return pd.DataFrame(
                {"rank_score": 0.0, "expected_net_r": 0.0}, index=X.index
            )

        pred = pd.Series(self._intercept_, index=X.index)
        for col, coef in self._coef_.items():
            if col in X.columns:
                pred += X[col].fillna(0.0) * coef

        return pd.DataFrame(
            {
                "rank_score": pred.rank(pct=True),
                "expected_net_r": pred,
            },
            index=X.index,
        )


class LightGBMRanker:
    """LightGBM ranker with built-in feature importance reporting."""

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

    def fit(self, X: pd.DataFrame, y: pd.Series) -> LightGBMRanker:
        """Fit a LightGBM regressor."""
        try:
            import lightgbm as lgb
        except ImportError:
            return self

        clean = X.dropna().join(y.dropna(), how="inner")
        if len(clean) < 30:
            return self

        model = lgb.LGBMRegressor(
            n_estimators=self.n_estimators,
            max_depth=self.max_depth,
            learning_rate=self.learning_rate,
            random_state=self.seed,
            verbose=-1,
        )
        model.fit(clean[X.columns], clean[y.name])
        self._model = model
        self._feature_importance_ = dict(
            zip(X.columns, model.feature_importances_)
        )
        return self

    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        """Return DataFrame with ``rank_score``, ``expected_net_r``, and
        ``probability_positive``."""
        if self._model is None:
            return pd.DataFrame(
                {"rank_score": 0.0, "expected_net_r": 0.0, "probability_positive": 0.5},
                index=X.index,
            )

        preds = self._model.predict(X.fillna(0.0))
        result = pd.DataFrame(
            {
                "rank_score": pd.Series(preds).rank(pct=True),
                "expected_net_r": preds,
                "probability_positive": 1.0 / (1.0 + np.exp(-preds)),
            },
            index=X.index,
        )
        return result


def random_forest_ranker(
    X: pd.DataFrame,
    y: pd.Series,
    *,
    n_estimators: int = 100,
    max_depth: int = 7,
    seed: int = 42,
) -> dict[str, Any]:
    """Fit a RandomForestRegressor and return predictions + feature importances.

    Returns dict with keys ``predictions`` (DataFrame), ``importances``
    (dict), ``oob_score``.
    """
    from sklearn.ensemble import RandomForestRegressor

    clean = X.dropna().join(y.dropna(), how="inner")
    if len(clean) < 30:
        return {"predictions": pd.DataFrame(), "importances": {}, "oob_score": None}

    model = RandomForestRegressor(
        n_estimators=n_estimators,
        max_depth=max_depth,
        random_state=seed,
        oob_score=True,
        n_jobs=-1,
    )
    model.fit(clean[X.columns], clean[y.name])

    preds = model.predict(X.fillna(0.0))
    return {
        "predictions": pd.DataFrame(
            {
                "rank_score": pd.Series(preds).rank(pct=True),
                "expected_net_r": preds,
            },
            index=X.index,
        ),
        "importances": dict(zip(X.columns, model.feature_importances_)),
        "oob_score": float(model.oob_score_) if model.oob_score_ else None,
    }
