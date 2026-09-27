# ML API v1 handoff

The worker sends a fixed, versioned JSON snapshot to `POST /v1/predict-batch`. The service must not read mutable platform state to complete it. `GET /health/ready` and `GET /v1/model-info` are checked before inference.

## Required model-info response

The client requires this exact metadata shape (unknown fields currently reject the metadata response):

```json
{
  "model_version": "delay-model-1",
  "feature_version": "features-1",
  "supported_schema_versions": ["1"],
  "history_minutes": 30,
  "min_observations": 3,
  "max_age_seconds": 60,
  "required_fields": ["telemetry.lat", "telemetry.lon"],
  "supports_missing_cur_dev_s": true,
  "max_batch_size": 256,
  "requires_neighbor_vehicles": false,
  "requires_network": false,
  "batch_independent": true,
  "supports_late_probability": false,
  "supports_intervals": false,
  "reason_codes": ["FULL_CATBOOST", "COLD_PLUS_DEV", "COLD_STRICT"],
  "available_modes": ["COLD_STRICT", "COLD_PLUS_DEV", "FULL_CATBOOST"],
  "routing_policy": "full model when history is sufficient, otherwise cold-start"
}
```

`available_modes` and `routing_policy` are optional diagnostic fields for routed ML services. `required_fields` accepts top-level contract names and paths under `target.`, `telemetry.`, and `schedule_context.`. Neighbor context is not part of schema v1, so `requires_neighbor_vehicles: true` is rejected. If `requires_network` is true, the request must carry a `network_version`. The worker rejects unsupported schema versions and oversized batches before inference. It checks `history_minutes`, `min_observations`, `max_age_seconds`, required fields, and `supports_missing_cur_dev_s` per target; insufficient targets receive `status: "insufficient_data"` while eligible sibling targets can still be inferred. When any target is sent for inference, the worker sends the original complete batch with the same request ID and serialized input; local insufficiency results override those target results in the returned list. This preserves identical request bytes for retry and avoids changing context boundaries for models where targets interact.

## Request example

Times are ISO 8601 with an explicit UTC offset. Target lead time is greater than 10 minutes and at most 15 minutes from `as_of`. Telemetry `event_time` and `receive_time` cannot be later than `as_of`; planned schedule times can be in the future.

```json
{
  "request_id": "batch-00042",
  "run_id": "replay-20260926-a",
  "schema_version": "1",
  "as_of": "2026-09-26T09:00:00Z",
  "schedule_version": "schedule-20260926-v3",
  "network_version": null,
  "config_version": "1",
  "targets": [
    {
      "prediction_id": "bus-129964-visit-53700336299-0900",
      "tr_id": "129964",
      "target_stop_id": "53700336299",
      "target_time_begin": "2026-09-26T09:12:00Z",
      "cur_dev_s": 45.0,
      "cur_dev_source": "observed_stop_arrival",
      "cur_dev_time": "2026-09-26T08:58:40Z",
      "cur_dev_quality": "high"
    }
  ],
  "telemetry": [
    {
      "schema_version": "1",
      "run_id": "replay-20260926-a",
      "event_id": "row-0017",
      "source": "replay",
      "tr_id": "129964",
      "unit_id": "unit-001",
      "event_time": "2026-09-26T08:59:40Z",
      "receive_time": "2026-09-26T08:59:41Z",
      "ingested_at": "2026-09-26T08:59:41Z",
      "lat": 55.751244,
      "lon": 37.618423,
      "speed_kmh": 18.2,
      "heading_deg": 92.0,
      "location_valid": true,
      "quality_flags": [],
      "source_identity": {"file_version": "traffic-v1", "row": 17}
    }
  ],
  "schedule_context": [
    {
      "schedule_version": "schedule-20260926-v3",
      "stop_visit_id": "53700336299",
      "tr_id": "129964",
      "time_begin": "2026-09-26T09:12:00Z",
      "lat": 55.752001,
      "lon": 37.619111,
      "building_address": "Moscow"
    }
  ]
}
```

Invalid positions stay in telemetry with `lat` and `lon` cleared and `invalid_position` added to `quality_flags`; the event itself is retained. A model should use those quality signals when interpreting its input.

## Response example

The envelope echoes request/schema IDs and the router/service model and feature versions returned by model-info. There is one prediction result per target. A routed service may additionally set `model_version` and `feature_version` on each result to identify the concrete submodel; the client preserves those values and falls back to the envelope versions when they are omitted. `delay_s` is populated only for `status: "ok"`. Probability and interval groups are each all-present or all-null.

```json
{
  "request_id": "batch-00042",
  "schema_version": "1",
  "model_version": "delay-model-1",
  "feature_version": "features-1",
  "predictions": [
    {
      "prediction_id": "bus-129964-visit-53700336299-0900",
      "status": "ok",
      "delay_s": 132.5,
      "error_code": null,
      "reason_codes": [],
      "quality_flags": [],
      "late_probability": null,
      "late_threshold_s": null,
      "interval_lower_s": null,
      "interval_upper_s": null,
      "interval_coverage": null
    }
  ]
}
```

`status` is `ok`, `insufficient_data`, or `error`. `ok` requires a finite `delay_s`; other statuses require a null delay, and `error` requires `error_code`. Probability is in `[0,1]` and comes with `late_threshold_s`. Intervals have ordered bounds and a coverage in `[0,1]`. The client preserves good siblings; missing and duplicate IDs become per-target errors, malformed target values become `invalid_result`, and extra or uncorrelatable IDs reject the response batch. Request/schema/model/feature mismatches also reject the batch.

The default request timeout is 8 seconds. Prediction response bodies are limited to 16 MiB; readiness and model-info responses are limited to 1 MiB. Only prediction timeout and HTTP 502/503/504 receive one retry with the identical serialized input, within a 20-second total client budget. Schema failures are not retried. With ML disabled the client returns `ml_disabled` errors and invents no predictions.
