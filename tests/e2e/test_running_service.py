"""Real HTTP/TCP boundaries; external model is explicitly a test double."""

import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

TOKEN = "socket-test-secret-123456"
HEADERS = {"Authorization": "Bearer " + TOKEN}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ndtp_frame(service, kind, body, unit=42, rid=1):
    nph = struct.pack("<HHHI", service, kind, 1, rid) + body
    crc = 0xFFFF
    for byte in nph:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return (
        struct.pack("<HHH", 0x7E7E, len(nph), 0)
        + struct.pack(">H", crc)
        + struct.pack("<BIH", 2, unit, 0)
        + nph
    )


def handshake(unit=42):
    return ndtp_frame(0, 100, struct.pack("<HHHIII", 6, 2, 0, unit, 65535, 0), unit)


def navigation(unit=42, rid=2):
    body = b"\0\0" + struct.pack(
        "<IIIBBHHHHHBB",
        int(time.time()),
        376000000,
        557000000,
        224,
        10,
        20,
        20,
        90,
        1,
        100,
        12,
        1,
    )
    return ndtp_frame(1, 101, body, unit, rid)


@pytest.fixture
def running(tmp_path):
    port, tcp = free_port(), free_port()
    processes = []
    logs = []

    def start(ml_url=None):
        env = {
            **os.environ,
            "TRANSPORT_API_TOKEN": TOKEN,
            "TRANSPORT_DB_PATH": str(tmp_path / "live.db"),
            "TRANSPORT_NDTP_PORT": str(tcp),
            "TRANSPORT_NDTP_HOST": "127.0.0.1",
            "TRANSPORT_ML_URL": ml_url or "",
            "TRANSPORT_PREDICTION_INTERVAL": "3600",
        }
        log = open(tmp_path / f"service-{len(processes)}.log", "w+")
        logs.append(log)
        p = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "backend.app:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        processes.append(p)
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base, headers=HEADERS, timeout=3) as c:
            for _ in range(100):
                try:
                    if c.get("/api/v1/health/ready").status_code == 200:
                        return p, base, tcp
                except httpx.HTTPError:
                    pass
                if p.poll() is not None:
                    log.seek(0)
                    pytest.fail(log.read())
                time.sleep(0.05)
        pytest.fail("service startup timeout")

    yield start
    for p in processes:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
    for log in logs:
        log.close()


def test_real_ndtp_fragment_reconnect_invalid_isolation_and_restart(running):
    p, base, tcp = running()
    with httpx.Client(base_url=base, headers=HEADERS) as c:
        c.put("/api/v1/devices/42", json={"tr_id": "bus"}).raise_for_status()
        data = navigation()
        with socket.create_connection(("127.0.0.1", tcp)) as s:
            s.sendall(handshake())
            for b in data:
                s.sendall(bytes([b]))
        for _ in range(100):
            response = c.get("/api/v1/vehicles/bus")
            if response.status_code == 200:
                break
            time.sleep(0.02)
        assert response.json()["vehicle"]["lat"] == 55.7
        with socket.create_connection(("127.0.0.1", tcp)) as s:
            s.sendall(handshake() + data)
        with socket.create_connection(("127.0.0.1", tcp)) as s:
            s.sendall(handshake() + data[:-1] + bytes([data[-1] ^ 1]))
        for _ in range(100):
            issues = c.get("/api/v1/data-issues").json()["items"]
            if issues:
                break
            time.sleep(0.02)
        assert any("CRC" in x["detail"] for x in issues)
        assert c.get("/api/v1/dashboard").json()["counts"]["events"] == 1
        assert c.get("/api/v1/health/ready").status_code == 200
    p.terminate()
    assert p.wait(timeout=5) in (0, -15)
    _, base, _ = running()
    with httpx.Client(base_url=base, headers=HEADERS) as c:
        assert c.get("/api/v1/vehicles/bus").json()["vehicle"]["lat"] == 55.7
        assert c.get("/api/v1/dashboard").json()["counts"]["events"] == 1


def test_external_ml_http_isolated_from_ingestion_and_persists_versions(running):
    entered = threading.Event()
    release = threading.Event()
    received = []

    class ModelHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, payload):
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health/ready":
                return self.reply({"status": "ready"})
            self.reply(
                dict(
                    model_version="TEST-ONLY",
                    feature_version="features-1",
                    supported_schema_versions=["1"],
                    history_minutes=30,
                    min_observations=1,
                    max_age_seconds=120,
                    required_fields=[],
                    supports_missing_cur_dev_s=True,
                    max_batch_size=100,
                    requires_neighbor_vehicles=False,
                    requires_network=False,
                    batch_independent=False,
                    supports_late_probability=False,
                    supports_intervals=False,
                    reason_codes=[],
                )
            )

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(body)
            entered.set()
            release.wait(3)
            self.reply(
                dict(
                    request_id=body["request_id"],
                    schema_version="1",
                    model_version="TEST-ONLY",
                    feature_version="features-1",
                    predictions=[
                        {
                            "prediction_id": t["prediction_id"],
                            "status": "ok",
                            "delay_s": 23.0,
                        }
                        for t in body["targets"]
                    ],
                )
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _, base, _ = running(f"http://127.0.0.1:{server.server_port}")
        with httpx.Client(base_url=base, headers=HEADERS, timeout=5) as c:
            c.put("/api/v1/devices/42", json={"tr_id": "bus"}).raise_for_status()
            t = datetime.now(UTC)
            event = dict(
                run_id="live",
                event_id="e1",
                source="scenario",
                tr_id="bus",
                unit_id="42",
                event_time=t.isoformat(),
                receive_time=t.isoformat(),
                ingested_at=t.isoformat(),
                lat=55.7,
                lon=37.6,
                location_valid=True,
            )
            c.post("/api/v1/telemetry", json=event).raise_for_status()
            c.post(
                "/api/v1/schedules",
                json=[
                    dict(
                        schedule_version="default",
                        stop_visit_id="s",
                        tr_id="bus",
                        time_begin=(t + timedelta(minutes=12)).isoformat(),
                    )
                ],
            ).raise_for_status()
            c.post(
                "/api/v1/prediction-cycles", json={"as_of": t.isoformat()}
            ).raise_for_status()
            assert entered.wait(3)
            event["event_id"] = "while-ml"
            event["event_time"] = (t + timedelta(seconds=1)).isoformat()
            assert c.post("/api/v1/telemetry", json=event).status_code == 202
            assert c.get("/api/v1/health/ready").status_code == 200
            assert not c.get("/api/v1/predictions").json()["items"]
            release.set()
            for _ in range(100):
                results = c.get("/api/v1/predictions").json()["items"]
                if results:
                    break
                time.sleep(0.02)
            assert results[0]["delay_s"] == 23
            assert results[0]["model_version"] == "TEST-ONLY"
            assert len(received) == 1
            assert [e["event_id"] for e in received[0]["telemetry"]] == ["e1"]
            with c.stream("GET", "/api/v1/events?after=0") as stream:
                for line in stream.iter_lines():
                    if line.startswith("data:"):
                        assert json.loads(line[5:])["run_id"] == "live"
                        break
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
