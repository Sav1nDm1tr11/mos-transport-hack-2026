import csv
import hashlib
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/data-import"))
sys.path.insert(0, str(ROOT / "tools/csv-replay"))
import_schedule = importlib.import_module("import_schedule")
replay = importlib.import_module("replay")


def test_schedule_parser_uses_plan_only_and_converts_geometry_and_local_time():
    row = {
        "tt_action_item_id": "stop-1",
        "tr_id": "bus-1",
        "time_begin": "2026-01-06 06:36:00.000000000",
        "time_fact_begin": "2099-01-01 00:00:00",
        "geom": "POINT (37.43070705 55.8040083)",
        "building_address": "Stop address",
    }

    visit = import_schedule.parse_visit(row, "default", "Europe/Moscow")

    assert visit == {
        "schedule_version": "default",
        "stop_visit_id": "stop-1",
        "tr_id": "bus-1",
        "time_begin": "2026-01-06T03:36:00+00:00",
        "lat": 55.8040083,
        "lon": 37.43070705,
        "building_address": "Stop address",
    }
    assert "time_fact_begin" not in visit


def test_schedule_requires_timezone_only_for_naive_timestamp():
    row = {
        "tt_action_item_id": "1",
        "tr_id": "bus",
        "time_begin": "2026-01-06T00:00:00Z",
    }
    assert (
        import_schedule.parse_visit(row, "v", None)["time_begin"]
        == "2026-01-06T00:00:00+00:00"
    )
    row["time_begin"] = "2026-01-06 00:00:00"
    with pytest.raises(ValueError, match="timezone"):
        import_schedule.parse_visit(row, "v", None)


def test_schedule_batches_are_bounded(tmp_path):
    path = tmp_path / "schedule.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=["tt_action_item_id", "tr_id", "time_begin"]
        )
        writer.writeheader()
        for i in range(2005):
            writer.writerow(
                {
                    "tt_action_item_id": i,
                    "tr_id": "bus",
                    "time_begin": "2026-01-06T00:00:00Z",
                }
            )

    batches = list(
        import_schedule.iter_schedule_batches(path, "default", None, batch_size=1000)
    )

    assert [len(batch) for batch in batches] == [1000, 1000, 5]


def test_traffic_parser_produces_stable_event_id_and_nulls_invalid_gps(tmp_path):
    path = tmp_path / "traffic.csv"
    path.write_text(
        "tr_id,unit_id,event_time,location_valid,lon,lat,speed,heading\n"
        "bus-1,42,2026-01-06 06:36:00,True,bad,55.8,12,90\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    event = next(replay.iter_traffic_events(path, "run-a", "Europe/Moscow"))
    same = next(replay.iter_traffic_events(path, "run-a", "Europe/Moscow"))

    assert {k: v for k, v in event.items() if k != "ingested_at"} == {
        k: v for k, v in same.items() if k != "ingested_at"
    }
    assert event["event_id"] == hashlib.sha256(f"{digest}:1".encode()).hexdigest()
    assert event["event_time"] == "2026-01-06T03:36:00+00:00"
    assert event["receive_time"] == event["event_time"]
    assert event["lat"] is None and event["lon"] is None
    assert event["location_valid"] is False
    assert "gps_unparseable" in event["quality_flags"]
    assert event["source"] == "replay" and event["run_id"] == "run-a"
    assert event["ingested_at"] != event["event_time"]


def test_traffic_parser_rejects_naive_time_without_timezone(tmp_path):
    path = tmp_path / "traffic.csv"
    path.write_text(
        "tr_id,unit_id,event_time,location_valid,lon,lat\nb,1,2026-01-06 00:00:00,False,,,\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="timezone"):
        next(replay.iter_traffic_events(path, "r", None))


def test_invalid_coordinate_pair_is_null_with_quality_flag():
    row = {
        "tr_id": "bus",
        "unit_id": "42",
        "event_time": "2026-01-06T00:00:00Z",
        "location_valid": "True",
        "lon": "37.6",
        "lat": "95",
    }

    event = replay.parse_traffic_row(
        row, "run", "2026-01-06T00:00:00Z", 1, "Europe/Moscow"
    )

    assert event["lat"] is None and event["lon"] is None
    assert event["location_valid"] is False
    assert "invalid_position" in event["quality_flags"]
