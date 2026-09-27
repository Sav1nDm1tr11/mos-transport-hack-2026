from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


class CatBoostPredictor:
    def __init__(
        self,
        path: Path,
        *,
        metadata_path: Path | None = None,
        model_version: str = "catboost-optuna-v1",
        feature_version: str = "full-online-v1",
    ) -> None:
        from catboost import CatBoostRegressor

        self.model = CatBoostRegressor()
        self.model.load_model(path)
        self.features = list(self.model.feature_names_)
        self.cat_feature_indices = list(self.model.get_cat_feature_indices())
        self.cat_features = [self.features[index] for index in self.cat_feature_indices]
        self.model_version = model_version
        self.feature_version = feature_version
        self.target_mode = "direct"
        self.clip_low = None
        self.clip_high = None

        if metadata_path is not None and metadata_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.model_version = metadata.get("model_version", self.model_version)
            self.feature_version = metadata.get("feature_version", self.feature_version)
            self.target_mode = metadata.get("target_mode", metadata.get("mode", "direct"))
            self.clip_low = metadata.get("clip_low")
            self.clip_high = metadata.get("clip_high")

    def predict(self, frame: pd.DataFrame, cur_dev_s: float | None) -> float:
        matrix = frame[self.features].copy()
        for feature in self.cat_features:
            matrix[feature] = (
                matrix[feature]
                .astype("string")
                .fillna("__missing__")
                .astype(str)
            )

        prediction = float(self.model.predict(matrix)[0])
        if self.target_mode == "residual":
            if cur_dev_s is None:
                raise ValueError("residual CatBoost requires cur_dev_s")
            prediction += float(cur_dev_s)

        if self.clip_low is not None:
            prediction = max(prediction, float(self.clip_low))
        if self.clip_high is not None:
            prediction = min(prediction, float(self.clip_high))
        if not np.isfinite(prediction):
            raise ValueError("CatBoost returned a non-finite prediction")
        return prediction
