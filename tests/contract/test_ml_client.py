import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from processing_worker.ml_client import MLClient, MLClientError
from transport_contracts import PredictionBatch

NOW = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)


def make_batch():
    return PredictionBatch.model_validate(
        {
            "request_id": "req-1",
            "run_id": "run-1",
            "as_of": NOW,
            "schedule_version": "sched-1",
            "targets": [
                {
                    "prediction_id": "p1",
                    "tr_id": "bus-1",
                    "target_stop_id": "visit-1",
                    "target_time_begin": NOW + timedelta(minutes=11),
                },
                {
                    "prediction_id": "p2",
                    "tr_id": "bus-2",
                    "target_stop_id": "visit-2",
                    "target_time_begin": NOW + timedelta(minutes=12),
                },
            ],
        }
    )


def model_info(**updates):
    value = {
        "model_version": "model-1",
        "feature_version": "features-1",
        "supported_schema_versions": ["1"],
        "history_minutes": 30,
        "min_observations": 0,
        "max_age_seconds": 60,
        "required_fields": [],
        "supports_missing_cur_dev_s": True,
        "max_batch_size": 100,
        "requires_neighbor_vehicles": False,
        "requires_network": False,
        "batch_independent": True,
        "supports_late_probability": True,
        "supports_intervals": True,
        "reason_codes": [],
    }
    value.update(updates)
    return value


def handlers(predict_handler, *, info=None, ready_status=200):
    async def handle(request):
        if request.url.path == "/health/ready":
            return httpx.Response(ready_status, json={"status": "ready"})
        if request.url.path == "/v1/model-info":
            return httpx.Response(200, json=info or model_info())
        if request.url.path == "/v1/predict-batch":
            return await predict_handler(request)
        return httpx.Response(404)

    return handle


def response(request, predictions, **updates):
    body = {
        "request_id": "req-1",
        "schema_version": "1",
        "model_version": "model-1",
        "feature_version": "features-1",
        "predictions": predictions,
    }
    body.update(updates)
    return httpx.Response(200, json=body)


@pytest.mark.asyncio
async def test_disabled_client_returns_one_explicit_error_per_target():
    results = await MLClient(None).predict(make_batch())
    assert [r.error_code for r in results] == ["ml_disabled", "ml_disabled"]


@pytest.mark.asyncio
async def test_timeout_retries_once_with_the_identical_body_and_correlates_results():
    attempts = []

    async def predict(request):
        attempts.append(request.content)
        if len(attempts) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return response(
            request,
            [
                {"prediction_id": "p1", "status": "ok", "delay_s": 12.5},
                {
                    "prediction_id": "p2",
                    "status": "insufficient_data",
                    "delay_s": None,
                    "reason_codes": ["too_few_points"],
                },
            ],
        )

    client = MLClient("http://ml", transport=httpx.MockTransport(handlers(predict)))
    results = await client.predict(make_batch())
    await client.close()

    assert attempts[0] == attempts[1]
    assert [r.prediction_id for r in results] == ["p1", "p2"]
    assert [r.status for r in results] == ["ok", "insufficient_data"]


@pytest.mark.asyncio
async def test_retryable_gateway_status_retries_once_but_schema_error_does_not():
    calls = 0

    async def predict(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503) if calls == 1 else response(request, [])

    client = MLClient("http://ml", transport=httpx.MockTransport(handlers(predict)))
    results = await client.predict(make_batch())
    assert calls == 2
    assert all(r.error_code == "missing_result" for r in results)
    await client.close()


@pytest.mark.asyncio
async def test_partial_and_duplicate_predictions_preserve_valid_siblings_as_errors():
    async def predict(request):
        return response(
            request,
            [
                {"prediction_id": "p1", "status": "ok", "delay_s": 8},
                {"prediction_id": "p1", "status": "ok", "delay_s": 9},
            ],
        )

    client = MLClient("http://ml", transport=httpx.MockTransport(handlers(predict)))
    results = await client.predict(make_batch())
    assert [(r.prediction_id, r.error_code) for r in results] == [
        ("p1", "duplicate_result"),
        ("p2", "missing_result"),
    ]
    await client.close()


@pytest.mark.asyncio
async def test_malformed_point_isolated_and_extra_or_mismatched_batch_rejected():
    async def malformed(request):
        return httpx.Response(
            200,
            content=(
                b'{"request_id":"req-1","schema_version":"1","model_version":"model-1",'
                b'"feature_version":"features-1","predictions":['
                b'{"prediction_id":"p1","status":"ok","delay_s":NaN},'
                b'{"prediction_id":"p2","status":"ok","delay_s":2}]}'
            ),
        )

    client = MLClient("http://ml", transport=httpx.MockTransport(handlers(malformed)))
    results = await client.predict(make_batch())
    assert results[0].error_code == "invalid_result"
    assert results[1].delay_s == 2
    await client.close()

    async def mismatched(request):
        return response(request, [], request_id="another-request")

    client = MLClient("http://ml", transport=httpx.MockTransport(handlers(mismatched)))
    with pytest.raises(MLClientError, match="request_id"):
        await client.predict(make_batch())
    await client.close()


@pytest.mark.asyncio
async def test_incompatible_schema_and_unsupported_required_resources_fail_before_inference():
    called = False

    async def predict(request):
        nonlocal called
        called = True
        return response(request, [])

    for info in (
        model_info(supported_schema_versions=["2"]),
        model_info(requires_network=True),
        model_info(requires_neighbor_vehicles=True),
    ):
        client = MLClient(
            "http://ml", transport=httpx.MockTransport(handlers(predict, info=info))
        )
        with pytest.raises(MLClientError):
            await client.predict(make_batch())
        await client.close()
    assert called is False


@pytest.mark.asyncio
async def test_not_ready_model_is_reported_for_each_target_without_prediction_call():
    called = False

    async def predict(request):
        nonlocal called
        called = True
        return response(request, [])

    client = MLClient(
        "http://ml", transport=httpx.MockTransport(handlers(predict, ready_status=503))
    )
    results = await client.predict(make_batch())
    assert {r.error_code for r in results} == {"ml_unavailable"}
    assert called is False
    await client.close()


@pytest.mark.asyncio
async def test_response_version_must_match_metadata_and_schema_errors_are_not_retried():
    calls = 0

    async def wrong_version(request):
        nonlocal calls
        calls += 1
        return response(request, [], model_version="other-model")

    client = MLClient(
        "http://ml", transport=httpx.MockTransport(handlers(wrong_version))
    )
    with pytest.raises(MLClientError, match="versions"):
        await client.predict(make_batch())
    assert calls == 1
    await client.close()

    calls = 0

    async def schema_rejected(request):
        nonlocal calls
        calls += 1
        return httpx.Response(422, json={"error_code": "schema_mismatch"})

    client = MLClient(
        "http://ml", transport=httpx.MockTransport(handlers(schema_rejected))
    )
    with pytest.raises(MLClientError, match="HTTP 422"):
        await client.predict(make_batch())
    assert calls == 1
    await client.close()


@pytest.mark.asyncio
async def test_declared_minimum_history_and_freshness_are_applied_per_target():
    def telemetry(event_id, tr_id, seconds_ago):
        return {
            "run_id": "run-1",
            "event_id": event_id,
            "source": "replay",
            "tr_id": tr_id,
            "unit_id": f"unit-{tr_id}",
            "event_time": NOW - timedelta(seconds=seconds_ago),
            "receive_time": NOW - timedelta(seconds=seconds_ago),
            "ingested_at": NOW,
            "lat": 55.75,
            "lon": 37.61,
            "location_valid": True,
        }

    batch = PredictionBatch.model_validate(
        {
            **make_batch().model_dump(),
            "telemetry": [
                telemetry("e1", "bus-1", 10),
                telemetry("e2", "bus-1", 20),
                telemetry("e3", "bus-2", 120),
            ],
        }
    )
    calls = 0

    async def predict(request):
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        assert [target["prediction_id"] for target in payload["targets"]] == [
            "p1",
            "p2",
        ]
        assert request.content == batch.model_dump_json(exclude_none=False).encode()
        return response(
            request,
            [
                {"prediction_id": "p1", "status": "ok", "delay_s": 6},
                {"prediction_id": "p2", "status": "ok", "delay_s": 7},
            ],
        )

    client = MLClient(
        "http://ml",
        transport=httpx.MockTransport(
            handlers(
                predict,
                info=model_info(
                    min_observations=2, max_age_seconds=60, batch_independent=False
                ),
            )
        ),
    )
    results = await client.predict(batch)
    assert calls == 1
    assert results[0].status == "ok"
    assert (results[0].model_version, results[0].feature_version) == (
        "model-1",
        "features-1",
    )
    assert (results[1].status, results[1].reason_codes) == (
        "insufficient_data",
        ["insufficient_observations", "stale_telemetry"],
    )
    assert (results[1].model_version, results[1].feature_version) == (
        "model-1",
        "features-1",
    )
    await client.close()


@pytest.mark.asyncio
async def test_required_telemetry_and_network_fields_mark_targets_insufficient():
    called = False

    async def predict(request):
        nonlocal called
        called = True
        return response(request, [])

    for info in (
        model_info(min_observations=0, required_fields=["telemetry.lat"]),
        model_info(min_observations=0, required_fields=["network_version"]),
    ):
        client = MLClient(
            "http://ml", transport=httpx.MockTransport(handlers(predict, info=info))
        )
        results = await client.predict(make_batch())
        assert all(result.status == "insufficient_data" for result in results)
        assert called is False
        await client.close()


@pytest.mark.asyncio
async def test_response_body_is_bounded_before_json_parsing():
    async def oversized(request):
        return httpx.Response(200, content=b"x" * 40)

    client = MLClient("http://ml", transport=httpx.MockTransport(handlers(oversized)))
    client.MAX_RESPONSE_BYTES = 32
    with pytest.raises(MLClientError, match="size limit"):
        await client.predict(make_batch())
    await client.close()


async def test_public_check_rejects_short_history_before_inference():
    async def unexpected(request):
        pytest.fail("inference must not run")

    client = MLClient(
        "http://ml",
        history_seconds=60,
        transport=httpx.MockTransport(handlers(unexpected)),
    )
    try:
        with pytest.raises(MLClientError, match="history"):
            await client.check()
    finally:
        await client.close()


async def test_independent_split_is_deterministic_and_preserves_context():
    bodies = []

    async def predict(request):
        body = json.loads(request.content)
        bodies.append(body)
        return response(
            request,
            [
                dict(prediction_id=t["prediction_id"], status="ok", delay_s=0.0)
                for t in body["targets"]
            ],
            request_id=body["request_id"],
        )

    client = MLClient(
        "http://ml",
        transport=httpx.MockTransport(
            handlers(predict, info=model_info(max_batch_size=1))
        ),
    )
    try:
        first = await client.predict(make_batch())
        second = await client.predict(make_batch())
    finally:
        await client.close()
    assert [r.prediction_id for r in first] == ["p1", "p2"]
    assert first == second
    assert bodies[:2] == bodies[2:]
    assert bodies[0]["request_id"] != bodies[1]["request_id"]
    assert all(b["schedule_version"] == "sched-1" for b in bodies)


@pytest.mark.parametrize(
    "updates",
    [
        {"supported_schema_versions": ["2"]},
        {"requires_neighbor_vehicles": True},
        {"required_fields": ["telemetry.unavailable"]},
    ],
)
async def test_metadata_check_rejects_unsupported_capabilities_without_batch(updates):
    async def unexpected(request):
        pytest.fail("check must not invoke model")

    client = MLClient(
        "http://ml",
        transport=httpx.MockTransport(handlers(unexpected, info=model_info(**updates))),
    )
    try:
        with pytest.raises(MLClientError):
            await client.check()
    finally:
        await client.close()


async def test_oversized_dependent_batch_never_calls_inference():
    async def unexpected(request):
        pytest.fail("dependent batch cannot be split")

    client = MLClient(
        "http://ml",
        transport=httpx.MockTransport(
            handlers(
                unexpected, info=model_info(max_batch_size=1, batch_independent=False)
            )
        ),
    )
    try:
        with pytest.raises(MLClientError, match="maximum"):
            await client.predict(make_batch())
    finally:
        await client.close()
