#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
if [ ! -x .venv/bin/python ]; then
  echo 'Create .venv and install requirements.lock.txt, then pip install --no-deps -e . first.' >&2
  exit 1
fi
exec .venv/bin/python -m uvicorn backend.app:create_app --factory --host 127.0.0.1 --port "${PORT:-8000}" --workers 1
