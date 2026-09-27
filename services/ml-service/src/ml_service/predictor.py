from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
import pandas as pd
from transport_contracts import PredictionTarget


class ColdPredictor:
    def __init__(self, path: Path) -> None:
        with path.open("rb") as file:
            checkpoint = pickle.load(file)

        self.model = checkpoint["model"]
        self.preprocessor = checkpoint["preprocessor"]
        self.features = list(checkpoint["features"])
        self.mode = checkpoint.get("mode", path.stem)
        self.target_mode = checkpoint.get("target_mode", "direct")
        self.clip_low = checkpoint.get("clip_low")
        self.clip_high = checkpoint.get("clip_high")

    def predict(self, frame: pd.DataFrame, target: PredictionTarget) -> float:
        matrix = self.preprocessor.transform(frame[self.features])
        prediction = float(self.model.predict(matrix)[0])

        if self.target_mode == "residual":
            if target.cur_dev_s is None:
                raise ValueError("residual cold model requires cur_dev_s")
            prediction += float(target.cur_dev_s)

        if self.clip_low is not None:
            prediction = max(prediction, float(self.clip_low))
        if self.clip_high is not None:
            prediction = min(prediction, float(self.clip_high))

        if not np.isfinite(prediction):
            raise ValueError("model returned a non-finite prediction")

        return prediction
