"""Bounded asynchronous client for the external versioned ML API."""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from datetime import timedelta

import httpx
from pydantic import ValidationError
from transport_contracts import (
    BatchResponse,
    ModelInfo,
    PredictionBatch,
    PredictionResult,
)


class MLClientError(RuntimeError):
    """The ML service rejected a batch or returned an incompatible contract."""

    def __init__(self, message: str, reject_batch: bool = False) -> None:
        super().__init__(message)
        self.reject_batch = reject_batch


class MLClient:
    """Calls readiness, compatibility metadata, then predicts with one bounded retry.

    Required `/v1/model-info` fields are: `model_version`, `feature_version`,
    `supported_schema_versions`, `history_minutes`, `min_observations`,
    `max_age_seconds`, `required_fields`, `supports_missing_cur_dev_s`,
    `max_batch_size`, `requires_neighbor_vehicles`, `requires_network`,
    `batch_independent`, `supports_late_probability`, `supports_intervals`,
    and `reason_codes`. A model requiring neighbor context or a network resource
    is rejected because schema v1 has no neighbor payload and `network_version`
    is currently null unless the caller supplies it.
    """

    _RETRYABLE_STATUS = {502, 503, 504}
    MAX_RESPONSE_BYTES = 16 * 1024 * 1024
    MAX_METADATA_BYTES = 1024 * 1024

    def __init__(
        self,
        base_url: str | None,
        timeout: float = 8,
        transport: httpx.AsyncBaseTransport | None = None,
        history_seconds: int | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.base_url = base_url.rstrip("/") if base_url else None
        self.timeout = timeout
        if history_seconds is not None and history_seconds < 0:
            raise ValueError("history_seconds must be nonnegative")
        self.history_seconds = history_seconds
        self._transport = transport
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            assert self.base_url is not None
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self._transport,
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def predict(self, batch: PredictionBatch) -> list[PredictionResult]:
        try:
            return await asyncio.wait_for(self._predict(batch), timeout=20.0)
        except TimeoutError:
            return self._target_errors(
                batch, "ml_timeout", "ML integration exceeded its 20 second budget"
            )

    async def _predict(self, batch: PredictionBatch) -> list[PredictionResult]:
        if self.base_url is None:
            return self._target_errors(batch, "ml_disabled", "ML client is disabled")

        try:
            info = await self.check()
        except (httpx.HTTPError, ValueError, ValidationError, MLClientError) as exc:
            if isinstance(exc, MLClientError) and exc.reject_batch:
                raise
            return self._target_errors(batch, "ml_unavailable", str(exc))

        self._validate_compatibility(batch, info)
        if len(batch.targets) > info.max_batch_size:
            results = []
            for offset in range(0, len(batch.targets), info.max_batch_size):
                request_id = hashlib.sha256(
                    f"{batch.request_id}:{info.model_version}:{info.feature_version}:{info.max_batch_size}:{offset}".encode()
                ).hexdigest()
                part = batch.model_copy(
                    update={
                        "request_id": request_id,
                        "targets": batch.targets[offset : offset + info.max_batch_size],
                    }
                )
                results.extend(await self._infer(part, info))
            return results
        return await self._infer(batch, info)

    async def _infer(self, batch: PredictionBatch, info: ModelInfo):
        locally_insufficient = self._insufficient_targets(batch, info)
        eligible_targets = [
            target
            for target in batch.targets
            if target.prediction_id not in locally_insufficient
        ]
        local_results = {
            prediction_id: PredictionResult(
                prediction_id=prediction_id,
                status="insufficient_data",
                delay_s=None,
                reason_codes=reasons,
                model_version=info.model_version,
                feature_version=info.feature_version,
            )
            for prediction_id, reasons in locally_insufficient.items()
        }
        if not eligible_targets:
            return [local_results[target.prediction_id] for target in batch.targets]
        # Keep the immutable input and request_id byte-for-byte stable even when
        # local context checks identify individual targets as insufficient.
        inference_batch = batch
        body = batch.model_dump_json(exclude_none=False)
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                response = await self._post_predict(body)
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt == 0:
                    continue
                unavailable = self._target_errors(
                    inference_batch,
                    "ml_timeout",
                    "ML request timed out twice",
                    model_version=info.model_version,
                    feature_version=info.feature_version,
                )
                return self._merge_local_results(batch, unavailable, local_results)
            except httpx.HTTPError as exc:
                unavailable = self._target_errors(
                    inference_batch,
                    "ml_unavailable",
                    str(exc),
                    model_version=info.model_version,
                    feature_version=info.feature_version,
                )
                return self._merge_local_results(batch, unavailable, local_results)

            if response.status_code in self._RETRYABLE_STATUS and attempt == 0:
                continue
            if response.status_code in self._RETRYABLE_STATUS:
                unavailable = self._target_errors(
                    inference_batch,
                    "ml_unavailable",
                    f"ML service returned HTTP {response.status_code}",
                    model_version=info.model_version,
                    feature_version=info.feature_version,
                )
                return self._merge_local_results(batch, unavailable, local_results)
            if response.status_code >= 400:
                raise MLClientError(
                    f"ML prediction request rejected with HTTP {response.status_code}",
                    reject_batch=True,
                )
            inferred = self._parse_response(inference_batch, response, info)
            return self._merge_local_results(batch, inferred, local_results)

        unavailable = self._target_errors(
            inference_batch,
            "ml_unavailable",
            str(last_exc or "ML unavailable"),
            model_version=info.model_version,
            feature_version=info.feature_version,
        )
        return self._merge_local_results(batch, unavailable, local_results)

    async def _post_predict(self, body: str) -> httpx.Response:
        return await self._bounded_request(
            "POST",
            "/v1/predict-batch",
            body=body,
            maximum=self.MAX_RESPONSE_BYTES,
        )

    async def _bounded_request(
        self,
        method: str,
        path: str,
        *,
        body: str | None = None,
        maximum: int,
    ) -> httpx.Response:
        headers = {"content-type": "application/json"} if body is not None else None
        async with self._client().stream(
            method,
            path,
            content=body,
            headers=headers,
        ) as streamed:
            content_length = streamed.headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > maximum:
                        raise MLClientError(
                            f"ML response exceeds the {maximum}-byte size limit",
                            reject_batch=True,
                        )
                except ValueError as exc:
                    raise MLClientError(
                        "ML response has an invalid Content-Length", reject_batch=True
                    ) from exc
            if streamed.status_code >= 400:
                return httpx.Response(
                    streamed.status_code,
                    request=streamed.request,
                    headers=streamed.headers,
                )
            chunks: list[bytes] = []
            total = 0
            async for chunk in streamed.aiter_bytes():
                total += len(chunk)
                if total > maximum:
                    raise MLClientError(
                        f"ML response exceeds the {maximum}-byte size limit",
                        reject_batch=True,
                    )
                chunks.append(chunk)
            return httpx.Response(
                streamed.status_code,
                request=streamed.request,
                headers=streamed.headers,
                content=b"".join(chunks),
            )

    @staticmethod
    def _merge_local_results(
        batch: PredictionBatch,
        inferred: list[PredictionResult],
        local_results: dict[str, PredictionResult],
    ) -> list[PredictionResult]:
        by_id = {result.prediction_id: result for result in inferred}
        by_id.update(local_results)
        return [by_id[target.prediction_id] for target in batch.targets]

    async def check(self) -> ModelInfo:
        """Bounded readiness and configured history compatibility check."""
        if self.base_url is None:
            raise MLClientError("ML client is disabled")
        info = await asyncio.wait_for(self._check_ready_and_info(), timeout=20)
        if (
            self.history_seconds is not None
            and info.history_minutes * 60 > self.history_seconds
        ):
            raise MLClientError(
                "configured history is shorter than model history", reject_batch=True
            )
        return info

    def validate_compatibility(self, batch: PredictionBatch, info: ModelInfo) -> None:
        self._validate_compatibility(batch, info)

    def inspect_batch(
        self, batch: PredictionBatch, info: ModelInfo
    ) -> dict[str, list[str]]:
        self.validate_compatibility(batch, info)
        return self._insufficient_targets(batch, info)

    async def _check_ready_and_info(self) -> ModelInfo:
        ready = await self._bounded_request(
            "GET", "/health/ready", maximum=self.MAX_METADATA_BYTES
        )
        if ready.status_code >= 400:
            raise MLClientError(f"ML model is not ready (HTTP {ready.status_code})")
        try:
            ready_payload = ready.json()
        except ValueError as exc:
            raise MLClientError("ML readiness response is not JSON") from exc
        if (
            not isinstance(ready_payload, dict)
            or ready_payload.get("ready") is False
            or ready_payload.get("status") in {"not_ready", "unready", "error"}
        ):
            raise MLClientError("ML model is not ready")

        response = await self._bounded_request(
            "GET", "/v1/model-info", maximum=self.MAX_METADATA_BYTES
        )
        if response.status_code >= 400:
            raise MLClientError(
                f"ML model-info failed with HTTP {response.status_code}"
            )
        try:
            info = ModelInfo.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise MLClientError(
                f"ML model-info is malformed: {exc}", reject_batch=True
            ) from exc
        return info

    @staticmethod
    def _validate_compatibility(batch: PredictionBatch, info: ModelInfo) -> None:
        if batch.schema_version not in info.supported_schema_versions:
            raise MLClientError(
                f"ML model does not support schema_version {batch.schema_version}",
                reject_batch=True,
            )
        if len(batch.targets) > info.max_batch_size and not info.batch_independent:
            raise MLClientError(
                f"batch has {len(batch.targets)} targets; ML maximum is {info.max_batch_size}",
                reject_batch=True,
            )
        if info.requires_network and not batch.network_version:
            raise MLClientError(
                "ML model requires a network resource not present in this batch",
                reject_batch=True,
            )
        if info.requires_neighbor_vehicles:
            raise MLClientError(
                "ML model requires unsupported neighboring-vehicle context",
                reject_batch=True,
            )
        available_fields = {
            "request_id",
            "run_id",
            "schema_version",
            "as_of",
            "schedule_version",
            "network_version",
            "config_version",
            "targets",
            "telemetry",
            "schedule_context",
            "target.prediction_id",
            "target.tr_id",
            "target.target_stop_id",
            "target.target_time_begin",
            "target.cur_dev_s",
            "target.cur_dev_source",
            "target.cur_dev_time",
            "target.cur_dev_quality",
            "telemetry.event_time",
            "telemetry.receive_time",
            "telemetry.lat",
            "telemetry.lon",
            "telemetry.speed_kmh",
            "telemetry.heading_deg",
            "telemetry.quality_flags",
            "schedule_context.time_begin",
            "schedule_context.lat",
            "schedule_context.lon",
        }
        unsupported = set(info.required_fields) - available_fields
        if unsupported:
            raise MLClientError(
                "ML model requires unsupported input fields: "
                + ", ".join(sorted(unsupported)),
                reject_batch=True,
            )

    @staticmethod
    def _insufficient_targets(
        batch: PredictionBatch, info: ModelInfo
    ) -> dict[str, list[str]]:
        """Apply the model-declared context limits to each target independently."""
        insufficient: dict[str, list[str]] = {}
        history_start = batch.as_of - timedelta(minutes=info.history_minutes)
        required_telemetry_fields = [
            name.removeprefix("telemetry.")
            for name in info.required_fields
            if name.startswith("telemetry.")
        ]
        required_schedule_fields = [
            name.removeprefix("schedule_context.")
            for name in info.required_fields
            if name.startswith("schedule_context.")
        ]
        required_target_fields = [
            name.removeprefix("target.")
            for name in info.required_fields
            if name.startswith("target.")
        ]

        required_top_level_fields = {
            name for name in info.required_fields if "." not in name
        }
        top_level = batch.model_dump(exclude_none=False)
        missing_top_level = {
            name
            for name in required_top_level_fields
            if top_level.get(name) is None or top_level.get(name) == []
        }
        minimum_observations = max(
            info.min_observations,
            1 if required_telemetry_fields else 0,
        )

        for target in batch.targets:
            reasons: list[str] = []
            for name in sorted(missing_top_level):
                reasons.append(f"missing_required_input:{name}")
            if not info.supports_missing_cur_dev_s and target.cur_dev_s is None:
                reasons.append("missing_cur_dev_s")
            for field in required_target_fields:
                if getattr(target, field) is None:
                    reasons.append(f"missing_required_target_field:{field}")

            history = [
                event
                for event in batch.telemetry
                if event.tr_id == target.tr_id
                and history_start <= event.event_time <= batch.as_of
                and all(
                    getattr(event, field) is not None
                    for field in required_telemetry_fields
                )
            ]
            if minimum_observations > 0 and len(history) < minimum_observations:
                reasons.append("insufficient_observations")
            if (
                history
                and (
                    batch.as_of - max(event.event_time for event in history)
                ).total_seconds()
                > info.max_age_seconds
            ):
                reasons.append("stale_telemetry")

            visits = [
                visit for visit in batch.schedule_context if visit.tr_id == target.tr_id
            ]
            if required_schedule_fields and not any(
                all(
                    getattr(visit, field) is not None
                    for field in required_schedule_fields
                )
                for visit in visits
            ):
                reasons.append("missing_required_schedule_context")
            if reasons:
                insufficient[target.prediction_id] = list(dict.fromkeys(reasons))
        return insufficient

    def _parse_response(
        self,
        batch: PredictionBatch,
        response: httpx.Response,
        info: ModelInfo,
    ) -> list[PredictionResult]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise MLClientError(
                "ML prediction response is not valid JSON", reject_batch=True
            ) from exc
        if not isinstance(payload, dict):
            raise MLClientError(
                "ML prediction response must be an object", reject_batch=True
            )
        raw_predictions = payload.get("predictions")
        envelope = {
            key: value for key, value in payload.items() if key != "predictions"
        }
        try:
            metadata = BatchResponse.model_validate({**envelope, "predictions": []})
        except ValidationError as exc:
            raise MLClientError(
                f"ML response envelope is malformed: {exc}", reject_batch=True
            ) from exc
        if metadata.request_id != batch.request_id:
            raise MLClientError(
                "ML response request_id does not match request", reject_batch=True
            )
        if metadata.schema_version != batch.schema_version:
            raise MLClientError(
                "ML response schema_version does not match request", reject_batch=True
            )
        if (
            metadata.model_version != info.model_version
            or metadata.feature_version != info.feature_version
        ):
            raise MLClientError(
                "ML response versions do not match model-info", reject_batch=True
            )
        if not isinstance(raw_predictions, list):
            raise MLClientError(
                "ML response predictions must be an array", reject_batch=True
            )

        expected = {target.prediction_id for target in batch.targets}
        valid_ids: list[str | None] = []
        for item in raw_predictions:
            if not isinstance(item, dict) or not isinstance(
                item.get("prediction_id"), str
            ):
                raise MLClientError(
                    "ML response contains an uncorrelatable prediction",
                    reject_batch=True,
                )
            prediction_id = item["prediction_id"]
            if prediction_id not in expected:
                raise MLClientError(
                    f"ML response contains extra prediction_id {prediction_id}",
                    reject_batch=True,
                )
            valid_ids.append(prediction_id)
        counts = Counter(valid_ids)
        first_by_id: dict[str, PredictionResult] = {}
        for item in raw_predictions:
            prediction_id = item["prediction_id"]
            if counts[prediction_id] != 1:
                continue
            try:
                parsed = PredictionResult.model_validate(item)
                if (
                    parsed.late_probability is not None
                    and not info.supports_late_probability
                ):
                    raise ValueError("model-info declares no late-probability support")
                if parsed.interval_lower_s is not None and not info.supports_intervals:
                    raise ValueError("model-info declares no interval support")
                first_by_id[prediction_id] = parsed.model_copy(
                    update={
                        "model_version": info.model_version,
                        "feature_version": info.feature_version,
                    }
                )
            except (ValidationError, ValueError):
                # Per-target shape/value errors do not erase valid siblings.
                pass

        results: list[PredictionResult] = []
        for target in batch.targets:
            prediction_id = target.prediction_id
            if counts[prediction_id] > 1:
                results.append(
                    self._error(
                        prediction_id,
                        "duplicate_result",
                        "duplicate prediction_id in ML response",
                        model_version=info.model_version,
                        feature_version=info.feature_version,
                    )
                )
            elif counts[prediction_id] == 0:
                results.append(
                    self._error(
                        prediction_id,
                        "missing_result",
                        "ML response omitted this prediction",
                        model_version=info.model_version,
                        feature_version=info.feature_version,
                    )
                )
            elif prediction_id not in first_by_id:
                results.append(
                    self._error(
                        prediction_id,
                        "invalid_result",
                        "ML result failed schema validation",
                        model_version=info.model_version,
                        feature_version=info.feature_version,
                    )
                )
            else:
                results.append(first_by_id[prediction_id])
        return results

    @staticmethod
    def _error(
        prediction_id: str,
        error_code: str,
        reason: str,
        *,
        model_version: str | None = None,
        feature_version: str | None = None,
    ) -> PredictionResult:
        return PredictionResult(
            prediction_id=prediction_id,
            status="error",
            delay_s=None,
            error_code=error_code,
            reason_codes=[reason],
            model_version=model_version,
            feature_version=feature_version,
        )

    @classmethod
    def _target_errors(
        cls,
        batch: PredictionBatch,
        error_code: str,
        reason: str,
        *,
        model_version: str | None = None,
        feature_version: str | None = None,
    ) -> list[PredictionResult]:
        return [
            cls._error(
                target.prediction_id,
                error_code,
                reason,
                model_version=model_version,
                feature_version=feature_version,
            )
            for target in batch.targets
        ]
