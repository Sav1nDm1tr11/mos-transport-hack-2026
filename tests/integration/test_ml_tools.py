"""Exercise the test simulator through the real ML client and HTTP contract."""

import importlib.util
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from processing_worker.ml_client import MLClient, MLClientError
from transport_contracts import PredictionBatch

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    path = ROOT / "tools" / "ml" / f"{name}.py"
    assert path.exists(), f"Missing ML tool: {name}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def batch():
    return PredictionBatch.model_validate(
        dict(
            request_id="repeat-me",
            run_id="demo",
            as_of="2026-09-27T10:00:00Z",
            schedule_version="demo",
            targets=[
                dict(
                    prediction_id=f"p-{i}",
                    tr_id="bus",
                    target_stop_id=f"s-{i}",
                    target_time_begin="2026-09-27T10:12:00Z",
                )
                for i in range(2)
            ],
            telemetry=[
                dict(
                    run_id="demo",
                    event_id="e1",
                    source="scenario",
                    tr_id="bus",
                    unit_id="42",
                    event_time="2026-09-27T09:59:50Z",
                    receive_time="2026-09-27T09:59:50Z",
                    ingested_at="2026-09-27T09:59:50Z",
                    lat=55.7,
                    lon=37.6,
                    location_valid=True,
                )
            ],
        )
    )


@pytest.mark.parametrize(
    "mode,status,delay",
    [
        ("success", "ok", 132.5),
        ("early", "ok", -45.0),
        ("zero", "ok", 0.0),
        ("insufficient", "insufficient_data", None),
        ("unavailable503", "error", None),
        ("partial", "ok", 132.5),
    ],
)
async def test_simulator_modes_preserve_real_client_semantics(mode, status, delay):
    app = load("simulator").create_app(mode)
    client = MLClient("http://simulator", transport=httpx.ASGITransport(app=app))
    try:
        results = await client.predict(batch())
    finally:
        await client.close()
    assert results[0].status == status
    assert results[0].delay_s == delay
    if mode == "partial":
        assert results[1].status == "error"
    if mode == "unavailable503":
        assert results[0].error_code == "ml_unavailable"


async def test_wrong_version_is_rejected():
    client = MLClient(
        "http://simulator",
        transport=httpx.ASGITransport(
            app=load("simulator").create_app("wrong-version")
        ),
    )
    try:
        with pytest.raises(MLClientError, match="versions"):
            await client.predict(batch())
    finally:
        await client.close()


def test_simulator_repeats_identical_bytes_and_labels_test_only():
    with TestClient(load("simulator").create_app("success")) as client:
        body = batch().model_dump_json()
        first = client.post(
            "/v1/predict-batch",
            content=body,
            headers={"content-type": "application/json"},
        )
        second = client.post(
            "/v1/predict-batch",
            content=body,
            headers={"content-type": "application/json"},
        )
        assert first.status_code == 200
        assert first.content == second.content
        assert first.json()["model_version"].startswith("TEST-ONLY")
        assert first.json()["predictions"][0]["prediction_id"] == "p-0"


async def test_preflight_uses_real_checks_and_detects_insufficiency():
    preflight = load("preflight")
    transport = httpx.ASGITransport(app=load("simulator").create_app("success"))
    report = await preflight.inspect(
        "http://simulator", batch(), 1800, False, transport
    )
    assert report["ready"] is True and report["compatible"] is True
    assert report["prediction_attempted"] is False
    empty = batch().model_copy(update={"telemetry": []})
    report = await preflight.inspect("http://simulator", empty, 1800, False, transport)
    assert report["compatible"] is False
    assert set(report["insufficient_targets"]) == {"p-0", "p-1"}


async def test_preflight_does_not_echo_secret_url_or_server_errors():
    report = await load("preflight").inspect(
        "http://name:secret@example.invalid?token=hidden",
        None,
        1800,
        False,
        httpx.MockTransport(lambda request: httpx.Response(503)),
    )
    assert report["ready"] is False
    assert "secret" not in str(report) and "hidden" not in str(report)
