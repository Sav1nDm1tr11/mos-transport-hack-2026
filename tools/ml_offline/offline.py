"""Historical inference with immutable inputs, resumable results and strict exports."""

import argparse
import asyncio
import csv
import hashlib
import json
import math
import re
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from processing_worker.ml_client import MLClient
from transport_contracts import (
    PredictionBatch,
    PredictionResult,
    ScheduleVisit,
    TelemetryEvent,
)


def rows(path, delimiter=","):
    with Path(path).open(newline="", encoding="utf-8-sig") as stream:
        yield from csv.DictReader(stream, delimiter=delimiter)


def stamp(value, timezone):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(timezone))
    return dt.astimezone(UTC)


def number(value):
    if value in (None, ""):
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite input")
    return result


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def save(path, value):
    body = json.dumps(value, sort_keys=True, allow_nan=False, ensure_ascii=False)
    if path.exists():
        if path.read_text() != body:
            raise ValueError("immutable artifact differs")
        return
    temp = path.with_suffix(".tmp")
    temp.write_text(body)
    temp.replace(path)


async def replay(
    *,
    points,
    traffic,
    schedule,
    output,
    timezone=None,
    history_minutes=30,
    client,
    max_steps=None,
    speed=0,
):
    if not timezone:
        raise ValueError("explicit timezone required")
    ZoneInfo(timezone)
    if history_minutes < 0 or speed < 0 or (max_steps is not None and max_steps < 0):
        raise ValueError("negative replay parameter")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    manifest = dict(
        version=1,
        points=digest(points),
        traffic=digest(traffic),
        schedule=digest(schedule),
        timezone=timezone,
        history_minutes=history_minutes,
    )
    save(output / "manifest.json", manifest)
    for name in ("snapshots", "results"):
        (output / name).mkdir(exist_ok=True)
    # Bound memory while ordering input and slicing vehicle history.
    with tempfile.TemporaryDirectory() as scratch:
        db = sqlite3.connect(Path(scratch) / "index.sqlite")
        try:
            db.executescript("""
                CREATE TABLE points(id TEXT PRIMARY KEY, time TEXT, body TEXT);
                CREATE TABLE traffic(tr TEXT, time TEXT, received TEXT, body TEXT);
                CREATE INDEX traffic_time ON traffic(tr,time,received);
                CREATE TABLE schedule(id TEXT PRIMARY KEY, body TEXT);
            """)
            for p in rows(points):
                try:
                    db.execute(
                        "INSERT INTO points VALUES (?,?,?)",
                        (
                            p["sample_id"],
                            stamp(p["T"], timezone).isoformat(),
                            json.dumps(p),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError("duplicate sample_id") from exc
            for t in rows(traffic):
                event = stamp(t["event_time"], timezone)
                received = stamp(t["receive_time"], timezone)
                e = TelemetryEvent(
                    run_id="offline",
                    event_id=t["packet_id"],
                    source="replay",
                    tr_id=t["tr_id"],
                    unit_id=t["unit_id"],
                    event_time=event,
                    receive_time=received,
                    ingested_at=received,
                    location_valid=t["location_valid"].lower() == "true",
                    lat=number(t.get("lat")),
                    lon=number(t.get("lon")),
                    speed_kmh=number(t.get("speed")),
                    heading_deg=number(t.get("heading")),
                )
                db.execute(
                    "INSERT INTO traffic VALUES (?,?,?,?)",
                    (
                        e.tr_id,
                        event.isoformat(),
                        received.isoformat(),
                        e.model_dump_json(),
                    ),
                )
            for s in rows(schedule):
                coords = re.fullmatch(
                    r"POINT\s*\(([-\d.eE+]+)\s+([-\d.eE+]+)\)", s.get("geom", "")
                )
                visit = ScheduleVisit(
                    schedule_version=manifest["schedule"],
                    stop_visit_id=s["tt_action_item_id"],
                    tr_id=s["tr_id"],
                    time_begin=stamp(s["time_begin"], timezone),
                    lon=float(coords[1]) if coords else None,
                    lat=float(coords[2]) if coords else None,
                )
                try:
                    db.execute(
                        "INSERT INTO schedule VALUES (?,?)",
                        (visit.stop_visit_id, visit.model_dump_json()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError("duplicate schedule visit") from exc
            db.commit()
            completed = 0
            previous = None
            for idx, (sample_id, time, body) in enumerate(
                db.execute("SELECT id,time,body FROM points ORDER BY time,id")
            ):
                name = f"{idx:012d}.json"
                result_path = output / "results" / name
                if result_path.exists():
                    stored = json.loads(result_path.read_text())
                    if stored["sample_id"] != sample_id:
                        raise ValueError("checkpoint mismatch")
                    PredictionResult.model_validate(stored["result"])
                    continue
                if max_steps is not None and completed >= max_steps:
                    break
                p = json.loads(body)
                as_of = datetime.fromisoformat(time)
                if previous is not None and speed:
                    await asyncio.sleep(
                        max(0, (as_of - previous).total_seconds()) / speed
                    )
                context = db.execute(
                    "SELECT body FROM schedule WHERE id=?", (p["target_stop_id"],)
                ).fetchone()
                if context is None:
                    raise ValueError("target absent from schedule")
                visit = ScheduleVisit.model_validate_json(context[0])
                target_time = stamp(p["target_time_begin"], timezone)
                if visit.tr_id != p["tr_id"] or visit.time_begin != target_time:
                    raise ValueError("point and schedule target mismatch")
                events = [
                    TelemetryEvent.model_validate_json(r[0])
                    for r in db.execute(
                        "SELECT body FROM traffic WHERE tr=? AND time>=? AND time<=? AND received<=? ORDER BY time,body LIMIT 100001",
                        (
                            p["tr_id"],
                            (as_of - timedelta(minutes=history_minutes)).isoformat(),
                            time,
                            time,
                        ),
                    )
                ]
                if len(events) > 100000:
                    raise ValueError("history slice exceeds limit")
                identity = hashlib.sha256(
                    (json.dumps(manifest, sort_keys=True) + sample_id).encode()
                ).hexdigest()
                batch = PredictionBatch(
                    request_id=identity,
                    run_id="offline",
                    as_of=as_of,
                    schedule_version=manifest["schedule"],
                    telemetry=events,
                    schedule_context=[visit],
                    targets=[
                        dict(
                            prediction_id=sample_id,
                            tr_id=p["tr_id"],
                            target_stop_id=p["target_stop_id"],
                            target_time_begin=target_time,
                            cur_dev_s=number(p.get("cur_dev_s")),
                            cur_dev_source="dataset_point",
                            cur_dev_time=as_of,
                        )
                    ],
                )
                save(output / "snapshots" / name, batch.model_dump(mode="json"))
                results = await client.predict(batch)
                if len(results) != 1 or results[0].prediction_id != sample_id:
                    raise ValueError("result correlation mismatch")
                save(
                    result_path,
                    dict(
                        sample_id=sample_id,
                        request_id=identity,
                        result=results[0].model_dump(mode="json"),
                    ),
                )
                completed += 1
                previous = as_of
            return completed
        finally:
            db.close()


def results(output):
    values = {}
    for path in sorted((Path(output) / "results").glob("*.json")):
        data = json.loads(path.read_text())
        result = PredictionResult.model_validate(data["result"])
        key = data["sample_id"]
        if key in values:
            raise ValueError("duplicate result")
        if result.prediction_id != key:
            raise ValueError("result correlation mismatch")
        if result.status != "ok":
            raise ValueError("non-ok result: " + key)
        values[key] = result.delay_s
    return values


def submission(template, output, destination):
    records = list(rows(template, ";"))
    ids = [r["sample_id"] for r in records]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate template ID")
    values = results(output)
    if set(ids) != set(values):
        raise ValueError("submission coverage mismatch")
    destination = Path(destination)
    temp = destination.with_suffix(".tmp")
    with temp.open("w", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(["sample_id", "prediction"])
        writer.writerows((key, values[key]) for key in ids)
    temp.replace(destination)


def evaluate(labels, output, label_column="target", split="test"):
    if split not in {"train", "test"}:
        raise ValueError("evaluation only accepts train/test labeled splits")
    expected = {}
    for row in rows(labels):
        if row["sample_id"] in expected:
            raise ValueError("duplicate label ID")
        value = number(row[label_column])
        if value is None:
            raise ValueError("missing label")
        expected[row["sample_id"]] = value
    values = results(output)
    if not expected or set(expected) != set(values):
        raise ValueError("evaluation coverage mismatch")
    return dict(
        split=split,
        count=len(values),
        mae_s=sum(abs(values[k] - v) for k, v in expected.items()) / len(values),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("replay")
    for key in ("points", "traffic", "schedule", "output", "timezone", "url"):
        run.add_argument("--" + key, required=True)
    run.add_argument("--history-minutes", type=int, default=30)
    run.add_argument("--max-steps", type=int)
    run.add_argument("--speed", type=float, default=0)
    export = sub.add_parser("submission")
    for key in ("template", "output", "destination"):
        export.add_argument("--" + key, required=True)
    score = sub.add_parser("evaluate")
    for key in ("labels", "output", "label-column"):
        score.add_argument("--" + key, required=True)
    score.add_argument("--split", choices=["train", "test"], required=True)
    args = vars(parser.parse_args())
    command = args.pop("command")
    if command == "replay":
        url = args.pop("url")

        async def run():
            client = MLClient(url, history_seconds=args["history_minutes"] * 60)
            try:
                return await replay(**args, client=client)
            finally:
                await client.close()

        print(json.dumps({"completed": asyncio.run(run())}))
    elif command == "submission":
        submission(**args)
    else:
        print(json.dumps(evaluate(**args)))


if __name__ == "__main__":
    main()
