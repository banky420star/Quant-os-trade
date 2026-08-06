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
        """
        Initialize a regularized linear ranker.
        
        Parameters:
        	alpha (float): Overall regularization strength.
        	l1_ratio (float): Balance between L1 and L2 regularization, from 0.0 to 1.0.
        """
        self.alpha = alpha
        self.l1_ratio = l1_ratio
        self._coef_: dict[str, float] = {}
        self._intercept_: float = 0.0
        self._fitted_: bool = False

    def fit(self, X: pd.DataFrame, y: pd.Series) -> LinearRanker:
        """
        Fit the ranker on aligned, complete feature and label data.
        
        Parameters:
        	X (pd.DataFrame): Feature values used for training.
        	y (pd.Series): Target values used to train the model.
        
        Returns:
        	LinearRanker: This ranker after fitting, or unchanged when fewer than 20 aligned complete samples are available.
        """
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
        """
        Score candidates using the fitted linear ranking model.
        
        Parameters:
            X (pd.DataFrame): Candidate feature values.
        
        Returns:
            pd.DataFrame: A DataFrame containing percentile `rank_score` and predicted `expected_net_r` for each candidate.
        """
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
        """
        Initialize a LightGBM ranker with the specified model settings.
        
        Parameters:
            n_estimators (int): Number of boosting estimators.
            max_depth (int): Maximum depth of each tree.
            learning_rate (float): Boosting learning rate.
            seed (int): Random seed used for reproducibility.
        """
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.learning_rate = learning_rate
        self.seed = seed
        self._model: Any = None
        self._feature_importance_: dict[str, float] = {}

    def fit(self, X: pd.DataFrame, y: pd.Series) -> LightGBMRanker:
        """
        Fit the LightGBM regression model when sufficient complete training data is available.
        
        Parameters:
        	X (pd.DataFrame): Training feature values.
        	y (pd.Series): Target values aligned with the rows in `X`.
        
        Returns:
        	LightGBMRanker: This ranker instance.
        """
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
        """
        Score candidate signals using the fitted model.
        
        Parameters:
            X (pd.DataFrame): Feature values for the candidates to score.
        
        Returns:
            pd.DataFrame: A DataFrame containing percentile rank scores, predicted net R
            values, and estimated probabilities of positive outcomes. Unfitted models
            return neutral defaults.
        """
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
    """
    Train a random forest ranker and generate scores for the provided feature data.
    
    Parameters:
        X (pd.DataFrame): Feature data used for training and prediction.
        y (pd.Series): Target values aligned with the rows in ``X``.
        n_estimators (int): Number of trees in the forest.
        max_depth (int): Maximum depth of each tree.
        seed (int): Random seed used for model training.
    
    Returns:
        dict[str, Any]: A mapping containing ``predictions`` with percentile rank
        scores and expected net R values, ``importances`` with feature importance
        values, and ``oob_score`` with the out-of-bag score. Returns empty
        predictions and importances with a null OOB score when fewer than 30
        complete training samples are available.
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
