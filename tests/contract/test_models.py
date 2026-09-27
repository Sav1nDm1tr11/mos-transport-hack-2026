from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from transport_contracts import (
    BatchResponse,
    PredictionBatch,
    PredictionResult,
    ScheduleVisit,
    TelemetryEvent,
)

NOW = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)


def batch(**updates):
    data = {
        "request_id": "req-1",
        "run_id": "run-1",
        "as_of": NOW,
        "schedule_version": "sched-1",
        "targets": [
            {
                "prediction_id": "pred-1",
                "tr_id": "bus-1",
                "target_stop_id": "visit-1",
                "target_time_begin": NOW + timedelta(minutes=11),
            }
        ],
    }
    data.update(updates)
    return data


def event(**updates):
    data = {
        "schema_version": "1",
        "run_id": "run-1",
        "event_id": "event-1",
        "source": "ndtp",
        "tr_id": "bus-1",
        "unit_id": "unit-1",
        "event_time": NOW - timedelta(seconds=1),
        "receive_time": NOW,
        "ingested_at": NOW,
        "lat": 55.75,
        "lon": 37.61,
        "location_valid": True,
        "quality_flags": [],
        "source_identity": {"session": "s-1"},
    }
    data.update(updates)
    return data


def test_telemetry_requires_aware_times_and_finite_measurements():
    with pytest.raises(ValidationError):
        TelemetryEvent(**event(event_time=datetime(2026, 9, 26, 8, 59)))
    with pytest.raises(ValidationError):
        TelemetryEvent(**event(speed_kmh=float("inf")))


def test_invalid_gps_is_retained_with_position_cleared_and_flagged():
    item = TelemetryEvent(**event(lat=95.0, lon=37.61))

    assert item.event_id == "event-1"
    assert item.lat is item.lon is None
    assert item.location_valid is False
    assert "invalid_position" in item.quality_flags


def test_coordinates_must_be_a_pair_and_schedule_allows_future_plans():
    item = TelemetryEvent(**event(lat=55.75, lon=None))
    assert item.lat is item.lon is None
    assert "invalid_position" in item.quality_flags

    visit = ScheduleVisit(
        schedule_version="sched-1",
        stop_visit_id="visit-1",
        tr_id="bus-1",
        time_begin=NOW + timedelta(days=1),
    )
    assert visit.time_begin > NOW


def test_prediction_target_window_is_open_at_ten_and_closed_at_fifteen_minutes():
    assert len(PredictionBatch(**batch()).targets) == 1
    for delta in (timedelta(minutes=10), timedelta(minutes=15, microseconds=1)):
        with pytest.raises(ValidationError):
            PredictionBatch(
                **batch(
                    targets=[
                        {
                            "prediction_id": "pred-1",
                            "tr_id": "bus-1",
                            "target_stop_id": "visit-1",
                            "target_time_begin": NOW + delta,
                        }
                    ]
                )
            )


def test_batch_rejects_future_event_or_receive_time():
    with pytest.raises(ValidationError):
        PredictionBatch(
            **batch(telemetry=[event(event_time=NOW + timedelta(seconds=1))])
        )
    with pytest.raises(ValidationError):
        PredictionBatch(
            **batch(telemetry=[event(receive_time=NOW + timedelta(seconds=1))])
        )


def test_prediction_result_requires_consistent_optional_outputs():
    PredictionResult(prediction_id="pred-1", status="ok", delay_s=4.5)
    with pytest.raises(ValidationError):
        PredictionResult(prediction_id="pred-1", status="ok", delay_s=None)
    with pytest.raises(ValidationError):
        PredictionResult(
            prediction_id="pred-1",
            status="ok",
            delay_s=4,
            late_probability=0.8,
        )
    with pytest.raises(ValidationError):
        PredictionResult(
            prediction_id="pred-1",
            status="error",
            delay_s=4,
            error_code="bad_input",
        )


def test_batch_response_exposes_model_and_feature_versions():
    response = BatchResponse(
        request_id="req-1",
        schema_version="1",
        model_version="m-1",
        feature_version="f-1",
        predictions=[],
    )
    assert response.model_version == "m-1"
    assert response.feature_version == "f-1"


def test_json_wire_timestamps_parse_from_dict_but_reject_naive_or_coerced_values():
    data = event()
    data["event_time"] = "2026-09-26T08:59:59Z"
    data["receive_time"] = "2026-09-26T09:00:00+00:00"
    data["ingested_at"] = "2026-09-26T09:00:00Z"
    item = TelemetryEvent.model_validate(data)
    assert item.event_time.tzinfo is UTC

    with pytest.raises(ValidationError):
        TelemetryEvent.model_validate({**data, "event_time": "2026-09-26T08:59:59"})
    with pytest.raises(ValidationError):
        TelemetryEvent.model_validate({**data, "event_time": 1_790_403_599})


def test_false_or_synthetic_position_is_cleared_and_empty_diagnostics_default():
    item = TelemetryEvent.model_validate(event(location_valid=False))
    assert item.lat is item.lon is None
    assert item.quality_flags == ["invalid_position"]

    synthetic = TelemetryEvent.model_validate(event(lat=0.0, lon=0.0))
    assert synthetic.lat is synthetic.lon is None
    assert "invalid_position" in synthetic.quality_flags

    defaults = TelemetryEvent.model_validate(
        {
            key: value
            for key, value in event().items()
            if key not in {"quality_flags", "source_identity"}
        }
    )
    assert defaults.quality_flags == []
    assert defaults.source_identity == {}


def test_identifiers_are_non_empty_and_bounded():
    with pytest.raises(ValidationError):
        TelemetryEvent.model_validate(event(run_id=""))
    with pytest.raises(ValidationError):
        TelemetryEvent.model_validate(event(event_id="e" * 201))


def test_gps_and_validity_types_remain_strict():
    with pytest.raises(ValidationError):
        TelemetryEvent.model_validate(event(lat="55.75"))
    with pytest.raises(ValidationError):
        TelemetryEvent.model_validate(event(location_valid="false"))


def test_prediction_batch_accepts_iso_wire_timestamps_from_python_dict():
    payload = batch()
    payload["as_of"] = "2026-09-26T09:00:00Z"
    payload["targets"][0]["target_time_begin"] = "2026-09-26T09:11:00Z"
    parsed = PredictionBatch.model_validate(payload)
    assert parsed.as_of == NOW
    assert parsed.targets[0].target_time_begin == NOW + timedelta(minutes=11)
