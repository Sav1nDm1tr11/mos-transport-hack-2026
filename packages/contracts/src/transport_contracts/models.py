"""Versioned contracts shared by the gateway, worker, backend, and ML service."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=200)]


class ContractModel(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        allow_inf_nan=False,
        validate_assignment=True,
    )


def _parse_datetime(value: datetime | str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("timestamp must be an ISO 8601 datetime") from exc
    if not isinstance(value, datetime):
        raise ValueError("timestamp must be a datetime or ISO 8601 string")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def _finite_optional(value: float | None) -> float | None:
    if value is not None and not math.isfinite(value):
        raise ValueError("value must be finite")
    return value


class TelemetryEvent(ContractModel):
    schema_version: Literal["1"] = "1"
    run_id: Identifier
    event_id: Identifier
    source: Literal["ndtp", "replay", "scenario"]
    tr_id: Identifier
    unit_id: Identifier
    event_time: datetime
    receive_time: datetime
    ingested_at: datetime
    lat: float | None = None
    lon: float | None = None
    speed_kmh: float | None = None
    heading_deg: float | None = None
    location_valid: bool
    quality_flags: list[str] = Field(default_factory=list)
    source_identity: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_time", "receive_time", "ingested_at", mode="before")
    @classmethod
    def timestamps_are_aware_utc(cls, value: datetime | str) -> datetime:
        return _parse_datetime(value)

    @field_validator("source_identity")
    @classmethod
    def identity_is_finite_json(cls, value):
        try:
            json.dumps(value, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise ValueError("source_identity must contain finite JSON values") from exc
        return value

    @field_validator("speed_kmh", "heading_deg")
    @classmethod
    def measurements_are_finite(cls, value: float | None) -> float | None:
        return _finite_optional(value)

    @model_validator(mode="before")
    @classmethod
    def retain_event_and_clear_invalid_gps(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if not isinstance(data.get("location_valid"), bool):
            return data
        if "quality_flags" in data and not isinstance(data["quality_flags"], list):
            return data
        lat = data.get("lat")
        lon = data.get("lon")
        if any(
            coordinate is not None
            and (
                not isinstance(coordinate, (int, float)) or isinstance(coordinate, bool)
            )
            for coordinate in (lat, lon)
        ):
            return data
        valid_pair = (
            data.get("location_valid") is True
            and isinstance(lat, (int, float))
            and not isinstance(lat, bool)
            and math.isfinite(lat)
            and -90 <= lat <= 90
            and isinstance(lon, (int, float))
            and not isinstance(lon, bool)
            and math.isfinite(lon)
            and -180 <= lon <= 180
            and not (lat == 0 and lon == 0)
        )
        if not valid_pair:
            data["lat"] = None
            data["lon"] = None
            data["location_valid"] = False
            flags = list(data.get("quality_flags") or [])
            if "invalid_position" not in flags:
                flags.append("invalid_position")
            data["quality_flags"] = flags
        return data

    @field_validator("lat", "lon")
    @classmethod
    def coordinates_are_finite(cls, value: float | None) -> float | None:
        return _finite_optional(value)

    @model_validator(mode="after")
    def coordinates_are_paired(self) -> TelemetryEvent:
        if (self.lat is None) != (self.lon is None):
            raise ValueError("lat and lon must be provided together")
        return self


class ScheduleVisit(ContractModel):
    schedule_version: Identifier
    stop_visit_id: Identifier
    tr_id: Identifier
    time_begin: datetime
    lat: float | None = None
    lon: float | None = None
    building_address: str | None = None

    @field_validator("time_begin", mode="before")
    @classmethod
    def time_is_aware_utc(cls, value: datetime | str) -> datetime:
        return _parse_datetime(value)

    @field_validator("lat", "lon")
    @classmethod
    def coordinates_are_finite(cls, value: float | None) -> float | None:
        return _finite_optional(value)

    @model_validator(mode="after")
    def coordinates_are_paired(self) -> ScheduleVisit:
        if (self.lat is None) != (self.lon is None):
            raise ValueError("lat and lon must be provided together")
        if self.lat is not None and not (
            -90 <= self.lat <= 90 and -180 <= self.lon <= 180
        ):
            raise ValueError("coordinates are outside valid ranges")
        return self


class PredictionTarget(ContractModel):
    prediction_id: Identifier
    tr_id: Identifier
    target_stop_id: Identifier
    target_time_begin: datetime
    cur_dev_s: float | None = None
    cur_dev_source: str | None = None
    cur_dev_time: datetime | None = None
    cur_dev_quality: str | None = None

    @field_validator("target_time_begin", "cur_dev_time", mode="before")
    @classmethod
    def times_are_aware_utc(cls, value: datetime | str | None) -> datetime | None:
        return None if value is None else _parse_datetime(value)

    @field_validator("cur_dev_s")
    @classmethod
    def deviation_is_finite(cls, value: float | None) -> float | None:
        return _finite_optional(value)


class PredictionBatch(ContractModel):
    request_id: Identifier
    run_id: Identifier
    schema_version: Literal["1"] = "1"
    as_of: datetime
    schedule_version: Identifier
    network_version: str | None = None
    config_version: str = "1"
    targets: list[PredictionTarget]
    telemetry: list[TelemetryEvent] = Field(default_factory=list)
    schedule_context: list[ScheduleVisit] = Field(default_factory=list)

    @field_validator("as_of", mode="before")
    @classmethod
    def as_of_is_aware_utc(cls, value: datetime | str) -> datetime:
        return _parse_datetime(value)

    @model_validator(mode="after")
    def temporal_contract(self) -> PredictionBatch:
        prediction_ids = [target.prediction_id for target in self.targets]
        if len(prediction_ids) != len(set(prediction_ids)):
            raise ValueError("prediction_id values must be unique within a batch")
        for target in self.targets:
            lead_seconds = (target.target_time_begin - self.as_of).total_seconds()
            if not 600 < lead_seconds <= 900:
                raise ValueError(
                    "target_time_begin must be in (as_of + 10m, as_of + 15m]"
                )
        for event in self.telemetry:
            if event.event_time > self.as_of or event.receive_time > self.as_of:
                raise ValueError(
                    "telemetry event_time and receive_time must not be after as_of"
                )
        return self


class PredictionResult(ContractModel):
    prediction_id: Identifier
    status: Literal["ok", "insufficient_data", "error"]
    delay_s: float | None = None
    error_code: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    quality_flags: list[str] = Field(default_factory=list)
    model_version: str | None = None
    feature_version: str | None = None
    late_probability: float | None = None
    late_threshold_s: float | None = None
    interval_lower_s: float | None = None
    interval_upper_s: float | None = None
    interval_coverage: float | None = None

    @field_validator(
        "delay_s",
        "late_probability",
        "late_threshold_s",
        "interval_lower_s",
        "interval_upper_s",
        "interval_coverage",
    )
    @classmethod
    def outputs_are_finite(cls, value: float | None) -> float | None:
        return _finite_optional(value)

    @model_validator(mode="after")
    def output_groups_are_consistent(self) -> PredictionResult:
        if self.status == "ok" and self.delay_s is None:
            raise ValueError("ok results require delay_s")
        if self.status != "ok" and self.delay_s is not None:
            raise ValueError("non-ok results require null delay_s")
        if self.status == "error" and self.error_code is None:
            raise ValueError("error results require error_code")
        probability_group = (self.late_probability, self.late_threshold_s)
        if any(value is not None for value in probability_group) and not all(
            value is not None for value in probability_group
        ):
            raise ValueError(
                "late_probability and late_threshold_s must be present together"
            )
        if self.late_probability is not None and not 0 <= self.late_probability <= 1:
            raise ValueError("late_probability must be between 0 and 1")
        interval_group = (
            self.interval_lower_s,
            self.interval_upper_s,
            self.interval_coverage,
        )
        if any(value is not None for value in interval_group) and not all(
            value is not None for value in interval_group
        ):
            raise ValueError("interval bounds and coverage must be present together")
        if self.interval_lower_s is not None:
            if self.interval_lower_s > self.interval_upper_s:
                raise ValueError("interval_lower_s must not exceed interval_upper_s")
            if not 0 <= self.interval_coverage <= 1:
                raise ValueError("interval_coverage must be between 0 and 1")
        return self


class BatchResponse(ContractModel):
    request_id: Identifier
    schema_version: Literal["1"]
    model_version: str
    feature_version: str
    predictions: list[PredictionResult]


class ModelInfo(ContractModel):
    """Capabilities published by GET /v1/model-info."""

    model_version: Identifier
    feature_version: Identifier
    supported_schema_versions: list[str]
    history_minutes: int = Field(ge=0)
    min_observations: int = Field(ge=0)
    max_age_seconds: int = Field(ge=0)
    required_fields: list[str]
    supports_missing_cur_dev_s: bool
    max_batch_size: int = Field(gt=0)
    requires_neighbor_vehicles: bool
    requires_network: bool
    batch_independent: bool
    supports_late_probability: bool
    supports_intervals: bool
    reason_codes: list[str]
    available_modes: list[str] = Field(default_factory=list)
    routing_policy: str | None = None
