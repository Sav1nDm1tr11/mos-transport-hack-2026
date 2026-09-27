from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from transport_contracts import BatchResponse, PredictionBatch, PredictionResult

from .features import ColdFeatureBuilder
from .full_features import FullFeatureBuilder
from .full_predictor import CatBoostPredictor
from .predictor import ColdPredictor
from .router import ModelRouter

log = logging.getLogger(__name__)

ROUTER_VERSION = os.getenv("ML_MODEL_VERSION", "hybrid-router-v1")
FEATURE_VERSION = os.getenv("ML_FEATURE_VERSION", "hybrid-online-v1")

STRICT_PATH = Path(
    os.getenv(
        "COLD_STRICT_MODEL_PATH",
        "/models/cold_start_rf/checkpoints/cold_strict.pkl",
    )
)
PLUS_DEV_PATH = Path(
    os.getenv(
        "COLD_PLUS_DEV_MODEL_PATH",
        "/models/cold_start_rf/checkpoints/cold_plus_dev.pkl",
    )
)
FULL_PATH = Path(
    os.getenv(
        "FULL_CATBOOST_MODEL_PATH",
        "/models/catboost/checkpoints/catboost_final.cbm",
    )
)
FULL_METADATA_PATH = Path(
    os.getenv(
        "FULL_CATBOOST_METADATA_PATH",
        "/models/catboost/checkpoints/catboost_final_metadata.json",
    )
)

FULL_HISTORY_MINUTES = int(os.getenv("FULL_HISTORY_MINUTES", "30"))
FULL_MIN_SPAN_MINUTES = float(os.getenv("FULL_MIN_SPAN_MINUTES", "15"))
FULL_MIN_OBSERVATIONS = int(os.getenv("FULL_MIN_OBSERVATIONS", "6"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    strict_model = ColdPredictor(
        STRICT_PATH,
        model_version=os.getenv("COLD_STRICT_MODEL_VERSION", "cold-rf-strict-v2"),
    )
    plus_dev_model = (
        ColdPredictor(
            PLUS_DEV_PATH,
            model_version=os.getenv(
                "COLD_PLUS_DEV_MODEL_VERSION", "cold-rf-plus-dev-v2"
            ),
        )
        if PLUS_DEV_PATH.exists()
        else None
    )

    full_model = None
    if FULL_PATH.exists():
        try:
            full_model = CatBoostPredictor(
                FULL_PATH,
                metadata_path=(
                    FULL_METADATA_PATH if FULL_METADATA_PATH.exists() else None
                ),
                model_version=os.getenv(
                    "FULL_CATBOOST_MODEL_VERSION", "catboost-optuna-v1"
                ),
            )
        except Exception:
            log.exception("full_catboost_load_failed")

    app.state.router = ModelRouter(
        strict_model=strict_model,
        plus_dev_model=plus_dev_model,
        full_model=full_model,
        full_history_minutes=FULL_HISTORY_MINUTES,
        full_min_span_minutes=FULL_MIN_SPAN_MINUTES,
        full_min_observations=FULL_MIN_OBSERVATIONS,
    )
    yield


app = FastAPI(
    title="Transport Delay ML Service",
    version=ROUTER_VERSION,
    lifespan=lifespan,
)


@app.get("/health/live")
def live():
    return {"status": "live"}


@app.get("/health/ready")
def ready():
    router = app.state.router
    return {
        "status": "ready",
        "ready": True,
        "model_version": ROUTER_VERSION,
        "available_modes": router.available_modes,
    }


@app.get("/v1/model-info")
def model_info():
    router = app.state.router
    return {
        "model_version": ROUTER_VERSION,
        "feature_version": FEATURE_VERSION,
        "supported_schema_versions": ["1"],
        "history_minutes": FULL_HISTORY_MINUTES,
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
            "FULL_CATBOOST",
            "COLD_PLUS_DEV",
            "COLD_STRICT",
            "FEATURE_BUILD_ERROR",
            "MODEL_ERROR",
        ],
        "available_modes": router.available_modes,
        "routing_policy": (
            "full CatBoost when cur_dev_s is known and history is sufficient; "
            "otherwise cold RF with/without cur_dev_s"
        ),
    }


@app.post("/v1/predict-batch", response_model=BatchResponse)
def predict_batch(batch: PredictionBatch):
    cold_builder = ColdFeatureBuilder(batch)
    full_builder = FullFeatureBuilder(batch)
    router = app.state.router
    predictions = []

    for target in batch.targets:
        decision = router.choose(target, cold_builder, full_builder)
        predictor = decision.predictor

        try:
            features = decision.builder.build(target, predictor.features)
            if decision.reason_code == "FULL_CATBOOST":
                delay_s = predictor.predict(features, target.cur_dev_s)
            else:
                delay_s = predictor.predict(features, target)

            result = PredictionResult(
                prediction_id=target.prediction_id,
                status="ok",
                delay_s=delay_s,
                reason_codes=[decision.reason_code],
                model_version=predictor.model_version,
                feature_version=predictor.feature_version,
            )
        except Exception as exc:
            log.exception(
                "prediction_failed: %s mode=%s",
                target.prediction_id,
                decision.reason_code,
            )
            result = PredictionResult(
                prediction_id=target.prediction_id,
                status="error",
                delay_s=None,
                error_code="ml_inference_error",
                reason_codes=[decision.reason_code, type(exc).__name__],
                model_version=getattr(predictor, "model_version", ROUTER_VERSION),
                feature_version=getattr(predictor, "feature_version", FEATURE_VERSION),
            )

        predictions.append(result)

    return BatchResponse(
        request_id=batch.request_id,
        schema_version=batch.schema_version,
        model_version=ROUTER_VERSION,
        feature_version=FEATURE_VERSION,
        predictions=predictions,
    )
