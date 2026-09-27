import time
from datetime import UTC, datetime, timedelta

import pytest
from backend.app import create_app
from backend.config import Settings
from fastapi.testclient import TestClient

TOKEN = "test-secret-token-long-enough"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path):
    app = create_app(
        Settings(
            db_path=str(tmp_path / "api.db"),
            api_token=TOKEN,
            ndtp_port=0,
            prediction_interval=3600,
        )
    )
    with TestClient(app) as c:
        yield c


def payload(eid="e1"):
    t = datetime.now(UTC).isoformat()
    return dict(
        run_id="live",
        event_id=eid,
        source="scenario",
        tr_id="bus",
        unit_id="42",
        event_time=t,
        receive_time=t,
        ingested_at=t,
        lat=55.7,
        lon=37.6,
        location_valid=True,
    )


def test_auth_health_and_invalid_body(client):
    assert client.get("/api/v1/health/live").status_code == 200
    assert client.get("/api/v1/health/ready").status_code == 200
    assert client.get("/api/v1/vehicles").status_code == 401
    assert client.post("/api/v1/telemetry", json={}, headers=HEADERS).status_code == 422
    assert client.get("/api/v1/vehicles?limit=999", headers=HEADERS).status_code == 422


def test_accept_read_duplicate_conflict_and_unknown_binding(client):
    e = payload()
    assert client.post("/api/v1/telemetry", json=e, headers=HEADERS).status_code == 422
    assert (
        client.put(
            "/api/v1/devices/42", json={"tr_id": "bus"}, headers=HEADERS
        ).status_code
        == 200
    )
    response = client.post("/api/v1/telemetry", json=e, headers=HEADERS)
    assert response.status_code == 202, response.text
    assert response.json()["accepted"] is True
    assert (
        client.post("/api/v1/telemetry", json=e, headers=HEADERS).json()["accepted"]
        is False
    )
    e["lat"] = 55.8
    assert client.post("/api/v1/telemetry", json=e, headers=HEADERS).status_code == 409
    for _ in range(100):
        r = client.get("/api/v1/vehicles/bus", headers=HEADERS)
        if r.status_code == 200:
            break
        time.sleep(0.02)
    assert r.json()["vehicle"]["lat"] == 55.7
    assert (
        client.get("/api/v1/dashboard", headers=HEADERS).json()["counts"]["events"] == 1
    )


def test_ml_absence_returns_explicit_unavailable_not_zero(client):
    client.put("/api/v1/devices/42", json={"tr_id": "bus"}, headers=HEADERS)
    client.post("/api/v1/telemetry", json=payload(), headers=HEADERS)
    t = datetime.now(UTC)
    visit = {
        "schedule_version": "default",
        "stop_visit_id": "stop",
        "tr_id": "bus",
        "time_begin": (t + timedelta(minutes=12)).isoformat(),
    }
    assert (
        client.post("/api/v1/schedules", json=[visit], headers=HEADERS).status_code
        == 201
    )
    r = client.post(
        "/api/v1/prediction-cycles",
        json={"as_of": (t + timedelta(seconds=1)).isoformat()},
        headers=HEADERS,
    )
    assert r.status_code == 202, r.text
    for _ in range(100):
        p = client.get("/api/v1/predictions", headers=HEADERS).json()["items"]
        if p:
            break
        time.sleep(0.02)
    assert len(p) == 1
    assert p[0]["delay_s"] is None and p[0]["status"] == "error"
    assert p[0]["error_code"] == "ml_disabled"


def test_request_id_and_json_error_contract(client):
    r = client.get("/api/v1/vehicles/missing", headers=HEADERS)
    assert r.status_code == 404
    assert r.json()["error_code"] == "vehicle_not_found"
    assert r.json()["request_id"] == r.headers["x-request-id"]
    r = client.post(
        "/api/v1/telemetry",
        content="{",
        headers={**HEADERS, "Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert "request_id" in r.json()


def test_schedule_version_conflict_is_atomic(client):
    t = datetime.now(UTC).isoformat()
    visit = {
        "schedule_version": "default",
        "stop_visit_id": "stop",
        "tr_id": "bus",
        "time_begin": t,
    }
    assert (
        client.post("/api/v1/schedules", json=[visit], headers=HEADERS).status_code
        == 201
    )
    visit["tr_id"] = "other"
    assert (
        client.post("/api/v1/schedules", json=[visit], headers=HEADERS).status_code
        == 409
    )


def test_malformed_quality_flags_with_invalid_gps_returns_422_not_500(client):
    e = payload()
    e.update(lat=None, lon=None, quality_flags=123)
    response = client.post("/api/v1/telemetry", json=e, headers=HEADERS)
    assert response.status_code == 422


def test_nonfinite_nested_metadata_is_rejected_before_storage(client):
    import json

    e = payload()
    e["source_identity"] = {"nested": [float("nan")]}
    response = client.post(
        "/api/v1/telemetry",
        content=json.dumps(e),
        headers={**HEADERS, "Content-Type": "application/json"},
    )
    assert response.status_code == 422
