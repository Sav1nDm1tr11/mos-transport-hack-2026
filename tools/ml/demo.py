"""Run isolated simulator + backend + API smoke; Ctrl-C cleans up both processes."""

import argparse
import os
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--smoke", action="store_true", help="exit after successful HTTP roundtrip"
    )
    parser.add_argument(
        "--browser-test", action="store_true", help="run Playwright tests then exit"
    )
    args = parser.parse_args()
    token = secrets.token_urlsafe(32)
    processes = []
    with tempfile.TemporaryDirectory(prefix="transport-ml-demo-") as scratch:
        env = dict(
            os.environ,
            TRANSPORT_API_TOKEN=token,
            TRANSPORT_DB_PATH=str(Path(scratch) / "demo.sqlite"),
            TRANSPORT_NDTP_PORT="0",
            TRANSPORT_RUN_ID="demo",
            TRANSPORT_SCHEDULE_VERSION="demo",
            TRANSPORT_HISTORY_SECONDS="1800",
            TRANSPORT_ML_URL="http://127.0.0.1:8090",
            TRANSPORT_PREDICTION_INTERVAL="3600",
            ML_SIMULATOR_MODE="success",
        )
        try:
            for app, port in [
                ("tools.ml.simulator:create_app", "8090"),
                ("backend.app:create_app", "8010"),
            ]:
                processes.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "uvicorn",
                            app,
                            "--factory",
                            "--host",
                            "127.0.0.1",
                            "--port",
                            port,
                        ],
                        cwd=ROOT,
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                )
            with httpx.Client(
                base_url="http://127.0.0.1:8010",
                headers={"Authorization": "Bearer " + token},
            ) as client:
                for _ in range(100):
                    if any(p.poll() is not None for p in processes):
                        raise RuntimeError(
                            "demo startup failed; check ports 8010/8090 are unused"
                        )
                    try:
                        response = client.post("/api/v1/ml/check")
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("demo startup timed out")

                def api(path, method="GET", body=None):
                    r = client.request(method, "/api/v1/" + path, json=body)
                    r.raise_for_status()
                    return r.json()

                now = datetime.now(UTC)
                api("devices/4242", "PUT", {"tr_id": "demo-bus"})
                api(
                    "schedules",
                    "POST",
                    [
                        dict(
                            schedule_version="demo",
                            stop_visit_id="demo-stop",
                            tr_id="demo-bus",
                            time_begin=(now + timedelta(minutes=12)).isoformat(),
                        )
                    ],
                )
                api(
                    "telemetry",
                    "POST",
                    dict(
                        run_id="demo",
                        event_id="demo-event",
                        source="scenario",
                        tr_id="demo-bus",
                        unit_id="4242",
                        event_time=now.isoformat(),
                        receive_time=now.isoformat(),
                        ingested_at=now.isoformat(),
                        lat=55.75,
                        lon=37.62,
                        location_valid=True,
                        speed_kmh=30.0,
                    ),
                )
                api("prediction-cycles", "POST", {"as_of": now.isoformat()})
                for _ in range(100):
                    predictions = api("predictions?run_id=demo")["items"]
                    if predictions:
                        break
                    time.sleep(0.1)
                assert predictions and predictions[0]["status"] == "ok", predictions
                assert predictions[0]["model_version"].startswith("TEST-ONLY")
                assert (
                    api("dashboard?run_id=demo")["coverage"]["predicted_vehicles"] == 1
                )
                print(
                    "PASS: simulator → worker → durable result → API coverage",
                    flush=True,
                )
                if args.browser_test:
                    subprocess.run(
                        [
                            os.environ.get("NODE", "node"),
                            "apps/frontend/tests/browser.cjs",
                        ],
                        cwd=ROOT,
                        env=dict(
                            env,
                            TEST_BASE_URL="http://127.0.0.1:8010",
                            TEST_API_TOKEN=token,
                        ),
                        check=True,
                    )
                elif not args.smoke:
                    print(
                        "Dashboard: http://127.0.0.1:8010/ ; stream: demo", flush=True
                    )
                    print("Temporary local demo token: " + token, flush=True)
                    while True:
                        time.sleep(1)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
