#!/usr/bin/env python3
"""Import planned stop visits from a dataset schedule CSV into the API."""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

_POINT = re.compile(r"^\s*POINT\s*\(\s*([^\s]+)\s+([^\s]+)\s*\)\s*$", re.IGNORECASE)


def parse_time(value: str, timezone_name: str | None) -> str:
    """Return an aware ISO timestamp in UTC; naive inputs require an explicit zone."""
    text = value.strip()
    if not text:
        raise ValueError("time_begin is empty")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid time_begin {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if not timezone_name:
            raise ValueError(
                "naive time_begin requires --timezone (IANA timezone name)"
            )
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone {timezone_name!r}") from exc
    return parsed.astimezone(UTC).isoformat()


def parse_point(value: str | None) -> tuple[float | None, float | None]:
    """Parse dataset WKT POINT(lon lat), returning (lat, lon)."""
    if not value or not value.strip():
        return None, None
    match = _POINT.fullmatch(value)
    if not match:
        raise ValueError(
            f"unsupported geom value {value!r}; expected POINT (longitude latitude)"
        )
    try:
        lon, lat = float(match.group(1)), float(match.group(2))
    except ValueError as exc:
        raise ValueError(f"invalid geom coordinate in {value!r}") from exc
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError(f"geom coordinates are outside valid ranges: {value!r}")
    return lat, lon


def parse_visit(
    row: dict[str, str], schedule_version: str, timezone_name: str | None
) -> dict:
    """Map one dataset plan row to ScheduleVisit without reading actual-time fields."""
    stop_id = (row.get("tt_action_item_id") or "").strip()
    tr_id = (row.get("tr_id") or "").strip()
    if not stop_id:
        raise ValueError("tt_action_item_id is empty")
    if not tr_id:
        raise ValueError("tr_id is empty")
    lat, lon = parse_point(row.get("geom"))
    return {
        "schedule_version": schedule_version,
        "stop_visit_id": stop_id,
        "tr_id": tr_id,
        "time_begin": parse_time(row.get("time_begin") or "", timezone_name),
        "lat": lat,
        "lon": lon,
        "building_address": (row.get("building_address") or "").strip() or None,
    }


def iter_schedule_batches(
    path: str | Path,
    schedule_version: str,
    timezone_name: str | None,
    batch_size: int = 1000,
) -> Iterator[list[dict]]:
    """Yield bounded ScheduleVisit request batches from the input CSV."""
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be from 1 through 1000")
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"tt_action_item_id", "tr_id", "time_begin"}
        missing = sorted(required - set(reader.fieldnames or []))
        if missing:
            raise ValueError(
                "schedule CSV is missing required columns: " + ", ".join(missing)
            )
        batch: list[dict] = []
        for line_number, row in enumerate(reader, start=2):
            try:
                batch.append(parse_visit(row, schedule_version, timezone_name))
            except ValueError as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
            if len(batch) == batch_size:
                yield batch
                batch = []
        if batch:
            yield batch


def import_schedule(
    path: str | Path,
    base_url: str,
    schedule_version: str,
    timezone_name: str | None,
    token: str,
    batch_size: int = 1000,
) -> int:
    headers = {"Authorization": f"Bearer {token}"}
    count = 0
    with httpx.Client(timeout=30.0, headers=headers) as client:
        for batch_number, batch in enumerate(
            iter_schedule_batches(path, schedule_version, timezone_name, batch_size),
            start=1,
        ):
            response = client.post(
                f"{base_url.rstrip('/')}/api/v1/schedules", json=batch
            )
            response.raise_for_status()
            count += len(batch)
            print(
                f"Imported batch {batch_number}: {len(batch)} visits (total {count})",
                file=sys.stderr,
            )
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "csv",
        type=Path,
        help="planned schedule CSV (schedule.csv or schedule_plan.csv)",
    )
    parser.add_argument(
        "--base-url",
        required=True,
        help="backend base URL, for example http://127.0.0.1:8000",
    )
    parser.add_argument(
        "--timezone",
        help="IANA timezone used only for CSV timestamps without an offset; required if any time_begin is naive",
    )
    parser.add_argument("--schedule-version", default="default")
    parser.add_argument(
        "--batch-size", type=int, default=1000, help="request size, maximum 1000"
    )
    args = parser.parse_args(argv)
    token = os.environ.get("TRANSPORT_API_TOKEN")
    if not token:
        parser.error("set TRANSPORT_API_TOKEN in the environment")
    try:
        count = import_schedule(
            args.csv,
            args.base_url,
            args.schedule_version,
            args.timezone,
            token,
            args.batch_size,
        )
    except (OSError, ValueError, httpx.HTTPError) as exc:
        print(f"schedule import failed: {exc}", file=sys.stderr)
        return 1
    print(f"Imported {count} planned visits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
