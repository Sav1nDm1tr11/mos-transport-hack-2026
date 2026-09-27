#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."

if [ ! -x .venv/bin/python ]; then
  echo 'Create .venv and install the project first.' >&2
  exit 1
fi

: "${COLD_STRICT_MODEL_PATH:=artifacts/models/cold_start_rf/checkpoints/cold_strict.pkl}"
: "${COLD_PLUS_DEV_MODEL_PATH:=artifacts/models/cold_start_rf/checkpoints/cold_plus_dev.pkl}"
: "${FULL_CATBOOST_MODEL_PATH:=artifacts/models/catboost/checkpoints/catboost_final.cbm}"

if [ ! -f "$COLD_STRICT_MODEL_PATH" ]; then
  echo "Missing required cold-start checkpoint: $COLD_STRICT_MODEL_PATH" >&2
  exit 1
fi

export COLD_STRICT_MODEL_PATH COLD_PLUS_DEV_MODEL_PATH FULL_CATBOOST_MODEL_PATH
export PYTHONPATH="services/ml-service/src:packages/contracts/src${PYTHONPATH:+:$PYTHONPATH}"

exec .venv/bin/python -m uvicorn ml_service.app:app \
  --host 127.0.0.1 \
  --port "${ML_PORT:-8090}" \
  --workers 1
