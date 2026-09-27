import csv
import importlib.util
import json
from pathlib import Path

import pytest
from transport_contracts import PredictionResult

MODULE = Path(__file__).resolve().parents[2] / "tools/ml_offline/offline.py"
spec = importlib.util.spec_from_file_location("ml_offline", MODULE)
offline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(offline)


def write(path, header, rows, delimiter=","):
    with path.open("w", newline="") as f:
        writer = csv.writer(f, delimiter=delimiter)
        writer.writerow(header)
        writer.writerows(rows)
    return path


@pytest.fixture
def source(tmp_path):
    points = write(
        tmp_path / "points.csv",
        ["sample_id", "tr_id", "T", "target_stop_id", "target_time_begin", "cur_dev_s"],
        [
            ["p2", "v", "2026-01-06 12:00:00", "s", "2026-01-06 12:15:00", ""],
            ["p1", "v", "2026-01-06 12:05:00", "s2", "2026-01-06 12:20:00", "-4"],
        ],
    )
    traffic = write(
        tmp_path / "traffic.csv",
        [
            "packet_id",
            "tr_id",
            "unit_id",
            "event_time",
            "receive_time",
            "location_valid",
            "lat",
            "lon",
            "speed",
            "heading",
        ],
        [
            [
                "old",
                "v",
                "u",
                "2026-01-06 11:00:00",
                "2026-01-06 11:00:00",
                "False",
                "",
                "",
                "",
                "",
            ],
            [
                "valid",
                "v",
                "u",
                "2026-01-06 11:59:00",
                "2026-01-06 12:00:00",
                "True",
                "55",
                "37",
                "0",
                "0",
            ],
            [
                "late",
                "v",
                "u",
                "2026-01-06 11:59:30",
                "2026-01-06 12:01:00",
                "False",
                "",
                "",
                "",
                "",
            ],
            [
                "future",
                "v",
                "u",
                "2026-01-06 12:01:00",
                "2026-01-06 12:00:00",
                "False",
                "",
                "",
                "",
                "",
            ],
        ],
    )
    schedule = write(
        tmp_path / "schedule.csv",
        ["tt_action_item_id", "tr_id", "time_begin", "geom"],
        [
            ["s", "v", "2026-01-06 12:15:00", "POINT (37 55)"],
            ["s2", "v", "2026-01-06 12:20:00", "POINT (37 55)"],
        ],
    )
    return points, traffic, schedule


class Client:
    def __init__(self):
        self.batches = []

    async def predict(self, batch):
        self.batches.append(batch)
        return [
            PredictionResult(
                prediction_id=batch.targets[0].prediction_id,
                status="ok",
                delay_s=-3.0 if len(self.batches) == 1 else 0.0,
            )
        ]


async def test_historical_resume(source, tmp_path):
    client = Client()
    args = dict(
        points=source[0],
        traffic=source[1],
        schedule=source[2],
        output=tmp_path / "out",
        timezone="UTC",
        history_minutes=10,
        client=client,
    )
    assert await offline.replay(**args, max_steps=1) == 1
    first = client.batches[0]
    assert first.targets[0].prediction_id == "p2"
    assert first.targets[0].cur_dev_s is None
    assert first.targets[0].cur_dev_source == "dataset_point"
    assert first.targets[0].cur_dev_time == first.as_of
    assert [e.event_id for e in first.telemetry] == ["valid"]
    snapshot = (tmp_path / "out/snapshots/000000000000.json").read_bytes()
    assert b"time_fact" not in snapshot
    assert await offline.replay(**args) == 1
    assert len(client.batches) == 2
    assert snapshot == (tmp_path / "out/snapshots/000000000000.json").read_bytes()
    assert await offline.replay(**args) == 0
    template = write(
        tmp_path / "sample.csv",
        ["sample_id", "prediction"],
        [["p1", 9], ["p2", 9]],
        ";",
    )
    offline.submission(template, tmp_path / "out", tmp_path / "submission.csv")
    assert (tmp_path / "submission.csv").read_text().splitlines() == [
        "sample_id;prediction",
        "p1;0.0",
        "p2;-3.0",
    ]
    labels = write(
        tmp_path / "labels.csv", ["sample_id", "target"], [["p1", 2], ["p2", -1]]
    )
    assert (
        offline.evaluate(labels, tmp_path / "out", label_column="target")["mae_s"]
        == 2.0
    )


async def test_requires_timezone_duplicate_and_changed_inputs(source, tmp_path):
    args = dict(
        points=source[0],
        traffic=source[1],
        schedule=source[2],
        output=tmp_path / "out",
        client=Client(),
    )
    with pytest.raises(ValueError, match="timezone"):
        await offline.replay(**args)
    with source[0].open("a") as f:
        f.write("p2,v,2026-01-06 12:00:00,s,2026-01-06 12:15:00,0\n")
    with pytest.raises(ValueError, match="duplicate"):
        await offline.replay(**args, timezone="UTC")


async def test_submission_refuses_missing_duplicate_error(source, tmp_path):
    out = tmp_path / "out"
    await offline.replay(
        points=source[0],
        traffic=source[1],
        schedule=source[2],
        output=out,
        timezone="UTC",
        client=Client(),
        max_steps=1,
    )
    template = write(
        tmp_path / "sample.csv",
        ["sample_id", "prediction"],
        [["p1", 9], ["p2", 9]],
        ";",
    )
    with pytest.raises(ValueError, match="coverage"):
        offline.submission(template, out, tmp_path / "submission.csv")
    assert not (tmp_path / "submission.csv").exists()
    write(template, ["sample_id", "prediction"], [["p2", 9], ["p2", 9]], ";")
    with pytest.raises(ValueError, match="duplicate"):
        offline.submission(template, out, tmp_path / "submission.csv")
    result_path = out / "results/000000000000.json"
    data = json.loads(result_path.read_text())
    data["result"] = {
        "prediction_id": "p2",
        "status": "error",
        "delay_s": None,
        "error_code": "bad",
    }
    result_path.write_text(json.dumps(data))
    write(template, ["sample_id", "prediction"], [["p2", 9]], ";")
    with pytest.raises(ValueError, match="non-ok"):
        offline.submission(template, out, tmp_path / "submission.csv")
