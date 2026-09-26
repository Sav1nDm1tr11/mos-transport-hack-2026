"""Regenerate the API and model JSON schemas from the executable contracts."""

import json
from pathlib import Path

from backend.app import create_app
from backend.config import Settings
from transport_contracts import BatchResponse, PredictionBatch, TelemetryEvent

root = Path(__file__).resolve().parents[2]
for relative, schema in (
    (
        "contracts/http/openapi.json",
        create_app(Settings(api_token="schema-generation-only")).openapi(),
    ),
    (
        "contracts/events/telemetry.normalized.v1.schema.json",
        TelemetryEvent.model_json_schema(),
    ),
    (
        "contracts/ml/predict-batch.request.schema.json",
        PredictionBatch.model_json_schema(),
    ),
    (
        "contracts/ml/predict-batch.response.schema.json",
        BatchResponse.model_json_schema(),
    ),
):
    (root / relative).write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
    )
