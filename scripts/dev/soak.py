#!/usr/bin/env python3
"""Run a sustained NDTP emulator load against the local backend profile."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


def load_token() -> str:
    for key in ("TRANSPORT_API_TOKEN", "API_TOKEN"):
        value = os.environ.get(key)
        if value:
            return value

    env_file = Path(".env")
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if text.startswith("export "):
                text = text[7:].lstrip()
            key, separator, value = text.partition("=")
            if not separator or key.strip() not in ("TRANSPORT_API_TOKEN", "API_TOKEN"):
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            else:
                value = value.split(" #", 1)[0].strip()
            if value:
                return value
    raise RuntimeError(
        "Set TRANSPORT_API_TOKEN or API_TOKEN in the environment or .env"
    )


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--emulator-url", default="http://127.0.0.1:18080")
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, default=9201)
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--units", type=int, default=20)
    parser.add_argument("--interval-ms", type=int, default=200)
    parser.add_argument("--report", type=Path, default=Path(".runtime/soak.json"))
    args = parser.parse_args()
    if not math.isfinite(args.duration) or args.duration <= 0:
        parser.error("--duration must be positive")
    if not 1 <= args.units <= 1000:
        parser.error("--units must be between 1 and 1000")
    if args.interval_ms <= 0:
        parser.error("--interval-ms must be positive")
    if not 1 <= args.target_port <= 65535:
        parser.error("--target-port must be between 1 and 65535")
    return args


class RequestMetrics:
    def __init__(self) -> None:
        self.latencies_ms: list[float] = []
        self.requests = 0
        self.errors = 0

    async def json_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
        ignored_statuses: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        started = time.perf_counter()
        self.requests += 1
        try:
            response = await client.request(method, path, json=payload)
        except httpx.HTTPError as exc:
            self.errors += 1
            raise RuntimeError(
                f"{method} {path} failed: {type(exc).__name__}"
            ) from None
        finally:
            self.latencies_ms.append((time.perf_counter() - started) * 1000)
        if response.status_code not in expected:
            if response.status_code not in ignored_statuses:
                self.errors += 1
            raise RuntimeError(f"{method} {path} returned HTTP {response.status_code}")
        if not response.content:
            return {}
        try:
            value = response.json()
        except ValueError:
            self.errors += 1
            raise RuntimeError(f"{method} {path} returned invalid JSON") from None
        if not isinstance(value, dict):
            self.errors += 1
            raise RuntimeError(f"{method} {path} returned a non-object JSON response")
        return value


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)], 3)


def issue_max_id(issues: dict[str, Any]) -> int:
    return max(
        (int(issue.get("id", 0)) for issue in issues.get("items", [])), default=0
    )


async def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    token = load_token()
    api = httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=5.0,
    )
    emulator = httpx.AsyncClient(base_url=args.emulator_url.rstrip("/"), timeout=5.0)
    metrics = RequestMetrics()
    unit_ids = list(range(1_800_000, 1_800_000 + args.units))
    tr_ids = [f"soak-bus-{index}" for index in range(args.units)]
    start_utc = datetime.now(UTC).isoformat()
    start_time = datetime.fromisoformat(start_utc)
    start_clock: float | None = None
    baseline_events = 0
    baseline_pending = 0
    baseline_issue_id = 0
    event_count = 0
    pending = 0
    issue_ids: set[int] = set()
    seen_states: set[str] = set()
    samples = 0
    emulator_touched = False
    failures: list[str] = []
    final_dashboard: dict[str, Any] = {}
    stop_payload: dict[str, Any] = {
        "targetHost": args.target_host,
        "targetPort": args.target_port,
        "units": [],
    }

    async def backend(
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200,),
        ignored_statuses: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        return await metrics.json_request(
            api,
            method,
            path,
            payload=payload,
            expected=expected,
            ignored_statuses=ignored_statuses,
        )

    async def emulator_call(payload: dict[str, Any]) -> dict[str, Any]:
        return await metrics.json_request(
            emulator, "POST", "/api/config", payload=payload, expected=(200, 201, 202)
        )

    try:
        # A not-ready response during startup is expected and is excluded from
        # the API error total. Network failures still count as errors.
        ready_deadline = time.monotonic() + 60
        while True:
            try:
                await backend("GET", "/api/v1/health/ready", ignored_statuses=(503,))
                break
            except RuntimeError as exc:
                if (
                    "returned HTTP 503" not in str(exc)
                    or time.monotonic() >= ready_deadline
                ):
                    raise
                await asyncio.sleep(1)

        initial = await backend("GET", "/api/v1/dashboard?limit=200")
        initial_counts = initial.get("counts", {})
        baseline_events = int(initial_counts.get("events", 0))
        baseline_pending = int(initial_counts.get("pending", 0))
        issues = await backend("GET", "/api/v1/data-issues?limit=200")
        baseline_issue_id = issue_max_id(issues)

        for unit_id, tr_id in zip(unit_ids, tr_ids, strict=True):
            await backend("PUT", f"/api/v1/devices/{unit_id}", payload={"tr_id": tr_id})

        units = [
            {
                "unitId": unit_id,
                "intervalMs": args.interval_ms,
                "autoGenerate": True,
                "cells": [],
            }
            for unit_id in unit_ids
        ]
        emulator_touched = True
        start_clock = time.monotonic()
        start_utc = datetime.now(UTC).isoformat()
        start_time = datetime.fromisoformat(start_utc)
        await emulator_call(
            {
                "targetHost": args.target_host,
                "targetPort": args.target_port,
                "units": units,
            }
        )

        deadline = start_clock + args.duration
        next_poll = start_clock
        sample_index = 0
        while time.monotonic() < deadline:
            await backend("GET", "/api/v1/health/ready")
            dashboard = await backend("GET", "/api/v1/dashboard?limit=200")
            counts = dashboard.get("counts", {})
            event_count = int(counts.get("events", 0))
            pending = int(counts.get("pending", 0))
            for state in dashboard.get("vehicles", []):
                tr_id = state.get("tr_id")
                if tr_id in tr_ids:
                    seen_states.add(tr_id)

            if seen_states:
                tr_id = sorted(seen_states)[sample_index % len(seen_states)]
                sample = await backend("GET", f"/api/v1/vehicles/{tr_id}")
                if sample.get("vehicle", {}).get("tr_id") == tr_id:
                    samples += 1
                sample_index += 1

            issues = await backend("GET", "/api/v1/data-issues?limit=200")
            issue_ids.update(
                int(issue["id"])
                for issue in issues.get("items", [])
                if int(issue.get("id", 0)) > baseline_issue_id
            )
            final_dashboard = dashboard
            next_poll += 1
            await asyncio.sleep(
                max(0, min(next_poll - time.monotonic(), deadline - time.monotonic()))
            )

        measured_duration = time.monotonic() - start_clock
    except Exception as exc:
        failures.append(str(exc))
        measured_duration = (
            (time.monotonic() - start_clock) if start_clock is not None else 0.0
        )
    finally:
        if emulator_touched:
            try:
                await emulator_call(stop_payload)
            except Exception as exc:
                failures.append(f"could not stop emulator units: {exc}")

            drain_deadline = time.monotonic() + 5
            while time.monotonic() < drain_deadline:
                try:
                    final_dashboard = await backend(
                        "GET", "/api/v1/dashboard?limit=200"
                    )
                    counts = final_dashboard.get("counts", {})
                    event_count = int(counts.get("events", 0))
                    pending = int(counts.get("pending", 0))
                    for state in final_dashboard.get("vehicles", []):
                        tr_id = state.get("tr_id")
                        if tr_id in tr_ids:
                            seen_states.add(tr_id)
                    issues = await backend("GET", "/api/v1/data-issues?limit=200")
                    issue_ids.update(
                        int(issue["id"])
                        for issue in issues.get("items", [])
                        if int(issue.get("id", 0)) > baseline_issue_id
                    )
                    if pending == 0:
                        break
                except Exception as exc:
                    failures.append(f"drain poll failed: {exc}")
                    break
                await asyncio.sleep(
                    min(0.25, max(0, drain_deadline - time.monotonic()))
                )

            # Dashboard snapshots are capped at 200 vehicles. Check each
            # configured id directly so --units values above 200 are verified
            # too, and require a state produced during this run.
            if start_clock is not None:
                for tr_id in tr_ids:
                    try:
                        value = await backend(
                            "GET", f"/api/v1/vehicles/{tr_id}", expected=(200, 404)
                        )
                    except Exception as exc:
                        failures.append(
                            f"vehicle verification failed for {tr_id}: {exc}"
                        )
                        continue
                    vehicle = value.get("vehicle")
                    if not isinstance(vehicle, dict) or not vehicle.get("event_time"):
                        continue
                    try:
                        event_time = datetime.fromisoformat(
                            str(vehicle["event_time"]).replace("Z", "+00:00")
                        )
                    except ValueError:
                        continue
                    if event_time >= start_time:
                        seen_states.add(tr_id)

        await api.aclose()
        await emulator.aclose()

    accepted_events = max(0, event_count - baseline_events)
    processed_events = max(
        0,
        (event_count - pending) - (baseline_events - baseline_pending),
    )
    throughput = (
        round(accepted_events / measured_duration, 3) if measured_duration > 0 else 0.0
    )
    missing_states = sorted(set(tr_ids) - seen_states)
    if start_clock is None:
        failures.append("load did not start")
    if missing_states:
        failures.append(
            f"vehicle state missing for {len(missing_states)} configured units"
        )
    if accepted_events < args.units:
        failures.append(
            f"accepted fewer events than configured units: {accepted_events}"
        )
    if pending != 0:
        failures.append(f"pending inbox did not drain: {pending}")
    if issue_ids:
        failures.append(f"new data issues observed: {len(issue_ids)}")
    if metrics.errors:
        failures.append(f"API request errors: {metrics.errors}")
    if samples == 0:
        failures.append("no vehicle sample was read")

    report = {
        "started_at": start_utc,
        "finished_at": datetime.now(UTC).isoformat(),
        "base_url": args.base_url,
        "emulator_url": args.emulator_url,
        "target_host": args.target_host,
        "target_port": args.target_port,
        "duration_seconds": round(measured_duration, 3),
        "configured_units": args.units,
        "interval_ms": args.interval_ms,
        "accepted_events": accepted_events,
        "processed_event_delta": processed_events,
        "throughput_events_per_second": throughput,
        "final_pending": pending,
        "invalid_issue_delta": len(issue_ids),
        "request_count": metrics.requests,
        "request_errors": metrics.errors,
        "request_latency_p95_ms": p95(metrics.latencies_ms),
        "vehicle_states_found": len(seen_states),
        "vehicle_states_missing": missing_states,
        "vehicle_samples": samples,
        "failures": failures,
        "passed": not failures,
    }
    await asyncio.to_thread(
        args.report.parent.mkdir,
        parents=True,
        exist_ok=True,
    )
    await asyncio.to_thread(
        args.report.write_text, json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    return report, 0 if report["passed"] else 1


def main() -> int:
    args = arguments()
    try:
        report, exit_code = asyncio.run(run(args))
    except Exception as exc:
        report = {
            "passed": False,
            "failures": [str(exc)],
            "report_error": "report could not be completed",
        }
        exit_code = 1
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps({"passed": report["passed"], "report": str(args.report)}, indent=2)
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
