"""TEST-ONLY deterministic ML service. Never use as a production predictor."""

import asyncio
import os

from fastapi import FastAPI, HTTPException
from transport_contracts import ModelInfo, PredictionBatch


def create_app(mode=None):
    mode = mode or os.getenv("ML_SIMULATOR_MODE", "success")
    if mode not in {
        "success",
        "early",
        "zero",
        "insufficient",
        "timeout",
        "unavailable503",
        "partial",
        "wrong-version",
    }:
        raise ValueError("unknown simulator mode")
    app = FastAPI(title="TEST-ONLY ML simulator")
    info = ModelInfo(
        model_version="TEST-ONLY-simulator-1",
        feature_version="TEST-ONLY-features-1",
        supported_schema_versions=["1"],
        history_minutes=30,
        min_observations=1,
        max_age_seconds=120,
        required_fields=[],
        supports_missing_cur_dev_s=True,
        max_batch_size=100,
        requires_neighbor_vehicles=False,
        requires_network=False,
        batch_independent=True,
        supports_late_probability=False,
        supports_intervals=False,
        reason_codes=["simulated_insufficient"],
    )

    @app.get("/health/ready")
    def ready():
        return {"status": "ready", "test_only": True}

    @app.get("/v1/model-info")
    def model_info():
        return info

    @app.post("/v1/predict-batch")
    async def predict(batch: PredictionBatch):
        if mode == "timeout":
            await asyncio.sleep(30)
        if mode == "unavailable503":
            raise HTTPException(503, "simulated outage")
        items = [
            dict(
                prediction_id=t.prediction_id,
                status="insufficient_data" if mode == "insufficient" else "ok",
                delay_s=None
                if mode == "insufficient"
                else {"early": -45.0, "zero": 0.0}.get(mode, 132.5),
                reason_codes=["simulated_insufficient"]
                if mode == "insufficient"
                else [],
            )
            for t in batch.targets
        ]
        return dict(
            request_id=batch.request_id,
            schema_version="1",
            model_version="wrong" if mode == "wrong-version" else info.model_version,
            feature_version=info.feature_version,
            predictions=items[:1] if mode == "partial" else items,
        )

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8090)
