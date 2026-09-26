from datetime import UTC, datetime, timedelta

import pytest
from backend.app import create_app
from backend.config import Settings
from fastapi.testclient import TestClient
from test_api import HEADERS, TOKEN, payload


def test_oversized_http_body_is_rejected_with_correlation(tmp_path):
    app = create_app(
        Settings(
            api_token=TOKEN,
            db_path=str(tmp_path / "body.db"),
            ndtp_port=0,
            max_http_bytes=1024,
        )
    )
    with TestClient(app) as c:
        r = c.post("/api/v1/telemetry", content=b"x" * 1025, headers=HEADERS)
        assert r.status_code == 413
        assert r.json()["request_id"] == r.headers["x-request-id"]


def test_bad_binding_and_future_timestamp_are_rejected(tmp_path):
    app = create_app(
        Settings(api_token=TOKEN, db_path=str(tmp_path / "future.db"), ndtp_port=0)
    )
    with TestClient(app) as c:
        assert (
            c.put(
                "/api/v1/devices/9999999999999", json={"tr_id": "b"}, headers=HEADERS
            ).status_code
            == 422
        )
        c.put("/api/v1/devices/42", json={"tr_id": "bus"}, headers=HEADERS)
        e = payload()
        e["event_time"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        r = c.post("/api/v1/telemetry", json=e, headers=HEADERS)
        assert r.status_code == 422 and r.json()["error_code"] == "future_event"


def test_second_process_owner_rejected_and_released_on_shutdown(tmp_path):
    settings = Settings(api_token=TOKEN, db_path=str(tmp_path / "lock.db"), ndtp_port=0)
    with TestClient(create_app(settings)), pytest.raises(BlockingIOError):
        with TestClient(create_app(settings)):
            pass
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/v1/health/ready").status_code == 200
