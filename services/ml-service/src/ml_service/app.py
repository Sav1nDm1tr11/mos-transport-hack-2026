from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from transport_contracts import BatchResponse, PredictionBatch, PredictionResult

from .features import ColdFeatureBuilder
from .predictor import ColdPredictor

log = logging.getLogger(__name__)

MODEL_VERSION = os.getenv("ML_MODEL_VERSION", "cold-rf-v1")
FEATURE_VERSION = os.getenv("ML_FEATURE_VERSION", "cold-online-v1")
STRICT_PATH = Path(
    os.getenv(
        "COLD_STRICT_MODEL_PATH",
        "/models/cold_strict.pkl",
    )
)
PLUS_DEV_PATH = Path(
    os.getenv(
        "COLD_PLUS_DEV_MODEL_PATH",
        "/models/cold_plus_dev.pkl",
    )
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.strict_model = ColdPredictor(STRICT_PATH)
    app.state.plus_dev_model = (
        ColdPredictor(PLUS_DEV_PATH)
        if PLUS_DEV_PATH.exists()
        else None
    )
    yield


app = FastAPI(
    title="Transport Delay ML Service",
    version=MODEL_VERSION,
    lifespan=lifespan,
)


@app.get("/health/live")
def live():
    return {"status": "live"}


@app.get("/health/ready")
def ready():
    return {
        "status": "ready",
        "ready": True,
        "model_version": MODEL_VERSION,
    }


@app.get("/v1/model-info")
def model_info():
    return {
        "model_version": MODEL_VERSION,
        "feature_version": FEATURE_VERSION,
        "supported_schema_versions": ["1"],
        "history_minutes": 30,
        "min_observations": 1,
        "max_age_seconds": 300,
        "required_fields": [
            "target.prediction_id",
            "target.tr_id",
            "target.target_stop_id",
            "target.target_time_begin",
            "telemetry.event_time",
            "telemetry.receive_time",
            "telemetry.lat",
            "telemetry.lon",
            "schedule_context.time_begin",
            "schedule_context.lat",
            "schedule_context.lon",
        ],
        "supports_missing_cur_dev_s": True,
        "max_batch_size": 256,
        "requires_neighbor_vehicles": False,
        "requires_network": False,
        "batch_independent": True,
        "supports_late_probability": False,
        "supports_intervals": False,
        "reason_codes": [
            "COLD_STRICT",
            "COLD_PLUS_DEV",
            "FEATURE_BUILD_ERROR",
            "MODEL_ERROR",
        ],
    }


@app.post("/v1/predict-batch", response_model=BatchResponse)
def predict_batch(batch: PredictionBatch):
    builder = ColdFeatureBuilder(batch)
    predictions = []

    for target in batch.targets:
        model = app.state.strict_model
        reason = "COLD_STRICT"

        if target.cur_dev_s is not None and app.state.plus_dev_model is not None:
            model = app.state.plus_dev_model
            reason = "COLD_PLUS_DEV"

        try:
            features = builder.build(target, model.features)
            delay_s = model.predict(features, target)
            result = PredictionResult(
                prediction_id=target.prediction_id,
                status="ok",
                delay_s=delay_s,
                reason_codes=[reason],
                model_version=MODEL_VERSION,
                feature_version=FEATURE_VERSION,
            )
        except Exception as exc:
            log.exception("prediction_failed: %s", target.prediction_id)
            result = PredictionResult(
                prediction_id=target.prediction_id,
                status="error",
                delay_s=None,
                error_code="ml_inference_error",
                reason_codes=[type(exc).__name__],
                model_version=MODEL_VERSION,
                feature_version=FEATURE_VERSION,
            )

        predictions.append(result)

    return BatchResponse(
        request_id=batch.request_id,
        schema_version=batch.schema_version,
        model_version=MODEL_VERSION,
        feature_version=FEATURE_VERSION,
        predictions=predictions,
    )
