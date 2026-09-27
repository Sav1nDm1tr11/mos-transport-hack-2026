from datetime import UTC, datetime, timedelta

from ml_service.features import ColdFeatureBuilder
from ml_service.full_features import FullFeatureBuilder
from ml_service.router import ModelRouter
from transport_contracts import PredictionBatch, PredictionTarget, ScheduleVisit, TelemetryEvent


class DummyPredictor:
    features = ["cur_dev_s"]


def make_batch(*, cur_dev_s=30.0, observations=7, span_minutes=20):
    as_of = datetime(2026, 1, 6, 9, 0, tzinfo=UTC)
    target = PredictionTarget(
        prediction_id="p1",
        tr_id="bus-1",
        target_stop_id="s2",
        target_time_begin=as_of + timedelta(minutes=12),
        cur_dev_s=cur_dev_s,
    )

    telemetry = []
    for index in range(observations):
        offset = span_minutes * index / max(observations - 1, 1)
        event_time = as_of - timedelta(minutes=span_minutes - offset)
        telemetry.append(
            TelemetryEvent(
                run_id="live",
                event_id=f"e{index}",
                source="replay",
                tr_id="bus-1",
                unit_id="u1",
                event_time=event_time,
                receive_time=event_time,
                ingested_at=event_time,
                lat=55.75 + index * 0.0001,
                lon=37.61 + index * 0.0001,
                speed_kmh=20.0,
                heading_deg=90.0,
                location_valid=True,
            )
        )

    schedule = [
        ScheduleVisit(
            schedule_version="default",
            stop_visit_id="s1",
            tr_id="bus-1",
            time_begin=as_of + timedelta(minutes=5),
            lat=55.755,
            lon=37.615,
        ),
        ScheduleVisit(
            schedule_version="default",
            stop_visit_id="s2",
            tr_id="bus-1",
            time_begin=as_of + timedelta(minutes=12),
            lat=55.760,
            lon=37.620,
        ),
    ]

    return PredictionBatch(
        request_id="batch-1",
        run_id="live",
        as_of=as_of,
        schedule_version="default",
        targets=[target],
        telemetry=telemetry,
        schedule_context=schedule,
    )


def test_router_prefers_full_after_history_accumulates():
    batch = make_batch()
    target = batch.targets[0]
    cold_builder = ColdFeatureBuilder(batch)
    full_builder = FullFeatureBuilder(batch)
    router = ModelRouter(
        strict_model=DummyPredictor(),
        plus_dev_model=DummyPredictor(),
        full_model=DummyPredictor(),
        full_history_minutes=30,
        full_min_span_minutes=15,
        full_min_observations=6,
    )

    decision = router.choose(target, cold_builder, full_builder)

    assert decision.reason_code == "FULL_CATBOOST"


def test_router_uses_plus_dev_before_full_history():
    batch = make_batch(span_minutes=5)
    target = batch.targets[0]
    cold_builder = ColdFeatureBuilder(batch)
    full_builder = FullFeatureBuilder(batch)
    router = ModelRouter(
        strict_model=DummyPredictor(),
        plus_dev_model=DummyPredictor(),
        full_model=DummyPredictor(),
        full_min_span_minutes=15,
        full_min_observations=6,
    )

    decision = router.choose(target, cold_builder, full_builder)

    assert decision.reason_code == "COLD_PLUS_DEV"


def test_router_uses_strict_without_current_deviation():
    batch = make_batch(cur_dev_s=None)
    target = batch.targets[0]
    cold_builder = ColdFeatureBuilder(batch)
    full_builder = FullFeatureBuilder(batch)
    router = ModelRouter(
        strict_model=DummyPredictor(),
        plus_dev_model=DummyPredictor(),
        full_model=DummyPredictor(),
    )

    decision = router.choose(target, cold_builder, full_builder)

    assert decision.reason_code == "COLD_STRICT"


def test_full_builder_uses_only_available_history():
    batch = make_batch()
    builder = FullFeatureBuilder(batch)
    frame = builder.build(
        batch.targets[0],
        [
            "cur_dev_s",
            "w30m_n",
            "w30m_speed_mean",
            "w30m_target_progress_m",
            "target_grid_3",
            "pca_core_01",
        ],
    )

    assert frame.loc[0, "cur_dev_s"] == 30.0
    assert frame.loc[0, "w30m_n"] == 7
    assert frame.loc[0, "w30m_speed_mean"] == 20.0
    assert frame.loc[0, "target_grid_3"] is not None
    assert frame.loc[0, "pca_core_01"] != frame.loc[0, "pca_core_01"]
