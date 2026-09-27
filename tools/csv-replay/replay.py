#!/usr/bin/env python3
"""Stream dataset traffic CSV into a run-scoped backend replay using virtual time."""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
import os
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx


def parse_time(value: str, timezone_name: str | None) -> datetime:
    text = value.strip()
    if not text:
        raise ValueError("event_time is empty")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid event_time {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if not timezone_name:
            raise ValueError(
                "naive event_time requires --timezone (IANA timezone name)"
            )
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone {timezone_name!r}") from exc
    return parsed.astimezone(UTC)


def _float_or_none(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _bool_value(value: str | None) -> bool:
    normalized = (value or "").strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return False


def parse_traffic_row(
    row: dict[str, str],
    run_id: str,
    file_digest: str,
    row_number: int,
    timezone_name: str | None,
    advance_clock: bool = True,
) -> dict:
    tr_id = (row.get("tr_id") or "").strip()
    unit_id = (row.get("unit_id") or "").strip()
    if not tr_id:
        raise ValueError("tr_id is empty")
    if not unit_id:
        raise ValueError(
            "unit_id is missing; replay needs the CSV's explicit unit_id for device binding"
        )
    event_dt = parse_time(row.get("event_time") or "", timezone_name)
    event_time = event_dt.isoformat()
    receive_raw = (row.get("receive_time") or "").strip()
    if receive_raw:
        receive_dt = parse_time(receive_raw, timezone_name)
    else:
        receive_dt = event_dt

    source_valid = _bool_value(row.get("location_valid"))
    lon = _float_or_none(row.get("lon"))
    lat = _float_or_none(row.get("lat"))
    flags: list[str] = []
    if source_valid:
        if lon is None or lat is None:
            flags.append("gps_unparseable")
            lon = lat = None
        elif not (-180 <= lon <= 180 and -90 <= lat <= 90) or (lat == 0 and lon == 0):
            flags.append("invalid_position")
            lon = lat = None
        else:
            location_valid = True
    else:
        flags.append("gps_invalid")
        lon = lat = None
    location_valid = bool(source_valid and lon is not None and lat is not None)

    speed = _float_or_none(row.get("speed"))
    heading = _float_or_none(row.get("heading"))
    if row.get("speed", "").strip() and speed is None:
        flags.append("speed_unparseable")
    if row.get("heading", "").strip() and heading is None:
        flags.append("heading_unparseable")

    if not receive_raw:
        flags.append("receive_time_assumed")

    return {
        "schema_version": "1",
        "run_id": run_id,
        "event_id": hashlib.sha256(f"{file_digest}:{row_number}".encode()).hexdigest(),
        "source": "replay",
        "tr_id": tr_id,
        "unit_id": unit_id,
        "event_time": event_time,
        "receive_time": receive_dt.isoformat(),
        "ingested_at": datetime.now(UTC).isoformat(),
        "lat": lat,
        "lon": lon,
        "speed_kmh": speed,
        "heading_deg": heading,
        "location_valid": location_valid,
        "quality_flags": flags,
        "source_identity": {
            "file_sha256": file_digest,
            "row_number": row_number,
            "advance_clock": advance_clock,
        },
    }


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_traffic_events(
    path: str | Path,
    run_id: str,
    timezone_name: str | None,
    limit: int | None = None,
    from_time: str | None = None,
    until_time: str | None = None,
    advance_clock: bool = True,
) -> Iterator[dict]:
    """Yield events in virtual availability order (receive_time, then source row)."""
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    start = parse_time(from_time, timezone_name) if from_time else None
    end = parse_time(until_time, timezone_name) if until_time else None
    if start is not None and end is not None and start > end:
        raise ValueError("--from-time must not be after --until-time")
    digest = file_sha256(path)
    events: list[dict] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"tr_id", "unit_id", "event_time", "location_valid", "lon", "lat"}
        missing = sorted(required - set(reader.fieldnames or []))
        if missing:
            raise ValueError(
                "traffic CSV is missing required columns: " + ", ".join(missing)
            )
        for row_number, row in enumerate(reader, start=1):
            try:
                events.append(
                    parse_traffic_row(
                        row,
                        run_id,
                        digest,
                        row_number,
                        timezone_name,
                        advance_clock,
                    )
                )
            except ValueError as exc:
                raise ValueError(f"{path}: data row {row_number}: {exc}") from exc
    events.sort(
        key=lambda event: (
            event["receive_time"],
            event["source_identity"]["row_number"],
        )
    )
    if start is not None:
        start_iso = start.isoformat()
        events = [event for event in events if event["receive_time"] >= start_iso]
    if end is not None:
        end_iso = end.isoformat()
        events = [event for event in events if event["receive_time"] <= end_iso]
    selected = events if limit is None else events[:limit]
    yield from selected


def _request_with_503_retry(
    client: httpx.Client, method: str, url: str, *, json: dict
) -> httpx.Response:
    delays = (0.5, 1.0, 2.0)
    for attempt in range(len(delays) + 1):
        response = client.request(method, url, json=json)
        if response.status_code != 503 or attempt == len(delays):
            response.raise_for_status()
            return response
        time.sleep(delays[attempt])
    raise AssertionError("unreachable")


def replay_csv(
    path: str | Path,
    base_url: str,
    token: str,
    run_id: str,
    timezone_name: str | None,
    limit: int | None = None,
    speed: float = 0,
    from_time: str | None = None,
    until_time: str | None = None,
    advance_clock: bool = True,
) -> int:
    if speed < 0 or not math.isfinite(speed):
        raise ValueError("speed must be a finite number greater than or equal to 0")
    base = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    bound: dict[str, str] = {}
    count = 0
    prior_time: datetime | None = None
    with httpx.Client(timeout=30.0, headers=headers) as client:
        for event in iter_traffic_events(
            path,
            run_id,
            timezone_name,
            limit,
            from_time,
            until_time,
            advance_clock,
        ):
            current_time = parse_time(event["receive_time"], "UTC")
            if speed > 0 and prior_time is not None:
                delay = max(0.0, (current_time - prior_time).total_seconds()) / speed
                if delay:
                    time.sleep(delay)
            prior_time = current_time

            unit_id, tr_id = event["unit_id"], event["tr_id"]
            previous = bound.get(unit_id)
            if previous is not None and previous != tr_id:
                raise ValueError(
                    f"unit_id {unit_id!r} maps to both {previous!r} and {tr_id!r} in this CSV"
                )
            if previous is None:
                bind = client.put(
                    f"{base}/api/v1/devices/{unit_id}", json={"tr_id": tr_id}
                )
                bind.raise_for_status()
                bound[unit_id] = tr_id
            _request_with_503_retry(
                client, "POST", f"{base}/api/v1/telemetry", json=event
            )
            count += 1
            if count % 100 == 0:
                print(
                    f"Replayed {count} rows · virtual time {current_time.isoformat()}",
                    file=sys.stderr,
                )
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__
        + " Events are replayed in receive_time order. Original event_time is preserved; if receive_time is absent it falls back to event_time with a quality flag."
    )
    parser.add_argument("csv", type=Path, help="traffic.csv")
    parser.add_argument(
        "--base-url",
        required=True,
        help="backend base URL, for example http://127.0.0.1:8000",
    )
    parser.add_argument(
        "--run-id", required=True, help="separate run identifier for this replay"
    )
    parser.add_argument(
        "--timezone",
        help="IANA timezone used only for timestamps without an offset; required if any event_time is naive",
    )
    parser.add_argument(
        "--limit", type=int, help="maximum number of events after virtual-time sorting/filtering"
    )
    parser.add_argument(
        "--from-time",
        help="start receive_time (inclusive); naive values use --timezone",
    )
    parser.add_argument(
        "--until-time",
        help="end receive_time (inclusive); naive values use --timezone",
    )
    parser.add_argument(
        "--no-advance-clock",
        action="store_true",
        help="load telemetry without moving the replay clock (useful for history warm-up)",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=0,
        help="virtual replay speed multiplier; 0 sends as fast as possible, 1 follows source time",
    )
    args = parser.parse_args(argv)
    token = os.environ.get("TRANSPORT_API_TOKEN")
    if not token:
        parser.error("set TRANSPORT_API_TOKEN in the environment")
    try:
        count = replay_csv(
            args.csv,
            args.base_url,
            token,
            args.run_id,
            args.timezone,
            args.limit,
            args.speed,
            args.from_time,
            args.until_time,
            not args.no_advance_clock,
        )
    except (OSError, ValueError, httpx.HTTPError) as exc:
        print(f"CSV replay failed: {exc}", file=sys.stderr)
        return 1
    print(f"Replayed {count} telemetry rows into run {args.run_id!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
