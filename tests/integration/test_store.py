from datetime import UTC, datetime, timedelta

import pytest
from transport_storage.store import Backpressure, Conflict, Store

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)


def event(event_id="e1", **kwargs):
    return dict(
        schema_version="1",
        run_id="live",
        event_id=event_id,
        source="scenario",
        tr_id="bus",
        unit_id="42",
        event_time=NOW.isoformat(),
        receive_time=NOW.isoformat(),
        ingested_at=NOW.isoformat(),
        lat=55.7,
        lon=37.6,
        speed_kmh=20,
        heading_deg=90,
        location_valid=True,
        quality_flags=[],
        source_identity={},
        **kwargs,
    )


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "state.sqlite"))
    s.bind_device("42", "bus")
    yield s
    s.close()


def test_durable_inbox_recovers_and_projects_after_restart(tmp_path):
    path = str(tmp_path / "s.db")
    first = Store(path)
    assert first.enqueue(event()) is True
    first.close()
    second = Store(path)
    assert second.process_pending() == 1
    assert second.vehicle("live", "bus")["lat"] == 55.7
    assert second.enqueue(event()) is False
    assert second.process_pending() == 0
    second.close()


def test_conflicting_event_id_is_not_silently_deduplicated(store):
    store.enqueue(event())
    altered = event()
    altered["lat"] = 55.8
    with pytest.raises(Conflict):
        store.enqueue(altered)


def test_late_event_and_invalid_position_preserve_good_position(store):
    store.enqueue(event())
    store.process_pending()
    old = event("old")
    old["event_time"] = (NOW - timedelta(seconds=10)).isoformat()
    old["lat"] = 50
    store.enqueue(old)
    store.process_pending()
    invalid = event("bad")
    invalid.update(
        event_time=(NOW + timedelta(seconds=10)).isoformat(),
        lat=None,
        lon=None,
        location_valid=False,
    )
    store.enqueue(invalid)
    store.process_pending()
    state = store.vehicle("live", "bus")
    assert state["lat"] == 55.7 and state["position_time"] == NOW.isoformat()
    assert state["event_time"] == invalid["event_time"]
    assert len(store.telemetry("live", "bus")) == 3


def test_run_isolation_and_bounded_pending_queue(store):
    store.enqueue(event(), max_pending=1)
    with pytest.raises(Backpressure):
        store.enqueue(event("e2"), max_pending=1)
    store.process_pending()
    e = event()
    e["run_id"] = "replay"
    assert store.enqueue(e) is True
    store.process_pending()
    assert len(store.vehicles("replay")) == 1


def test_schedule_selection_window_and_ambiguity(store):
    for i, seconds in enumerate([600, 660, 900, 901]):
        store.put_schedule(
            [
                dict(
                    schedule_version="v1",
                    stop_visit_id=str(i),
                    tr_id="bus",
                    time_begin=(NOW + timedelta(seconds=seconds)).isoformat(),
                    lat=None,
                    lon=None,
                    building_address=None,
                )
            ]
        )
    selected = store.targets("v1", NOW)
    assert selected[0]["stop_visit_id"] == "1"
    store.put_schedule(
        [
            dict(
                schedule_version="v1",
                stop_visit_id="other",
                tr_id="bus",
                time_begin=(NOW + timedelta(seconds=660)).isoformat(),
                lat=None,
                lon=None,
                building_address=None,
            )
        ]
    )
    assert store.targets("v1", NOW) == []


def test_prediction_input_is_immutable_and_output_idempotent(store):
    data = {
        "request_id": "b",
        "run_id": "live",
        "as_of": NOW.isoformat(),
        "targets": [],
    }
    assert store.save_batch(data)
    assert not store.save_batch(data)
    with pytest.raises(Conflict):
        store.save_batch({**data, "targets": [{}]})


def test_device_binding_is_explicit_and_conflicts_rejected(store):
    assert store.resolve_device("42") == "bus"
    assert store.resolve_device("unknown") is None
    with pytest.raises(Conflict):
        store.bind_device("42", "other")


def test_delayed_good_fix_can_advance_position_without_rolling_back_latest_packet(
    store,
):
    store.enqueue(event())
    store.process_pending()
    bad = event("bad")
    bad.update(
        event_time=(NOW + timedelta(seconds=10)).isoformat(),
        lat=None,
        lon=None,
        location_valid=False,
    )
    store.enqueue(bad)
    store.process_pending()
    delayed = event("delayed")
    delayed.update(event_time=(NOW + timedelta(seconds=5)).isoformat(), lat=55.8)
    store.enqueue(delayed)
    store.process_pending()
    result = store.vehicle("live", "bus")
    assert result["event_time"] == bad["event_time"]
    assert result["position_time"] == delayed["event_time"]
    assert result["lat"] == 55.8


def test_replay_clock_and_causal_snapshot_do_not_leak_future_telemetry(store):
    first = event("r1")
    first["run_id"] = "replay"
    first["event_time"] = (NOW - timedelta(minutes=5)).isoformat()
    first["receive_time"] = first["event_time"]
    first["speed_kmh"] = 10
    second = event("r2")
    second["run_id"] = "replay"
    second["event_time"] = (NOW + timedelta(minutes=5)).isoformat()
    second["receive_time"] = second["event_time"]
    second["speed_kmh"] = 40
    second["lat"] = 55.8

    store.enqueue(first)
    store.enqueue(second)
    store.process_pending()
    store.set_run_clock("replay", NOW)

    assert store.run_clock("replay") == NOW.isoformat()
    snap = store.snapshot_at("replay", NOW, 50)
    assert snap["counts"] == {"vehicles": 1, "events": 1, "pending": 0}
    assert snap["vehicles"][0]["speed_kmh"] == 10
    assert snap["vehicles"][0]["lat"] == 55.7

    later = store.snapshot_at("replay", NOW + timedelta(minutes=6), 50)
    assert later["counts"]["events"] == 2
    assert later["vehicles"][0]["speed_kmh"] == 40
    assert later["vehicles"][0]["lat"] == 55.8


def test_replay_duplicate_can_advance_clock_after_rewind(store):
    replay = event("duplicate-clock")
    replay["run_id"] = "replay"
    replay["source"] = "replay"
    replay["event_time"] = NOW.isoformat()
    replay["receive_time"] = NOW.isoformat()
    replay["source_identity"] = {"advance_clock": True}

    assert store.enqueue(replay) is True
    store.set_run_clock("replay", NOW - timedelta(minutes=1))
    assert store.enqueue(replay) is False
    assert store.run_clock("replay") == NOW.isoformat()


def test_replay_warmup_does_not_advance_clock(store):
    replay = event("warmup")
    replay["run_id"] = "replay"
    replay["source"] = "replay"
    replay["source_identity"] = {"advance_clock": False}

    assert store.enqueue(replay) is True
    assert store.run_clock("replay") is None
