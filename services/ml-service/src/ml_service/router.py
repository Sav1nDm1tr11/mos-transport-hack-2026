from __future__ import annotations

from dataclasses import dataclass

from transport_contracts import PredictionTarget

from .features import ColdFeatureBuilder
from .full_features import FullFeatureBuilder


@dataclass(frozen=True)
class RouteDecision:
    reason_code: str
    predictor: object
    builder: object


class ModelRouter:
    def __init__(
        self,
        *,
        strict_model,
        plus_dev_model=None,
        full_model=None,
        full_history_minutes: int = 30,
        full_min_span_minutes: float = 15.0,
        full_min_observations: int = 6,
    ) -> None:
        self.strict_model = strict_model
        self.plus_dev_model = plus_dev_model
        self.full_model = full_model
        self.full_history_minutes = full_history_minutes
        self.full_min_span_minutes = full_min_span_minutes
        self.full_min_observations = full_min_observations

    @property
    def available_modes(self) -> list[str]:
        modes = ["COLD_STRICT"]
        if self.plus_dev_model is not None:
            modes.append("COLD_PLUS_DEV")
        if self.full_model is not None:
            modes.append("FULL_CATBOOST")
        return modes

    def choose(
        self,
        target: PredictionTarget,
        cold_builder: ColdFeatureBuilder,
        full_builder: FullFeatureBuilder,
    ) -> RouteDecision:
        if self._full_ready(target, full_builder):
            return RouteDecision("FULL_CATBOOST", self.full_model, full_builder)
        if target.cur_dev_s is not None and self.plus_dev_model is not None:
            return RouteDecision("COLD_PLUS_DEV", self.plus_dev_model, cold_builder)
        return RouteDecision("COLD_STRICT", self.strict_model, cold_builder)

    def _full_ready(
        self,
        target: PredictionTarget,
        full_builder: FullFeatureBuilder,
    ) -> bool:
        if self.full_model is None or target.cur_dev_s is None:
            return False
        observations, span_minutes = full_builder.history_stats(
            target,
            self.full_history_minutes,
        )
        return (
            observations >= self.full_min_observations
            and span_minutes >= self.full_min_span_minutes
        )
