#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."

if [ ! -x .venv/bin/python ]; then
  echo 'Create .venv and install the project first.' >&2
  exit 1
fi
if [ ! -f .env ]; then
  echo 'Missing .env.' >&2
  exit 1
fi

set -a
. ./.env
set +a

: "${TRANSPORT_API_TOKEN:?Set TRANSPORT_API_TOKEN in .env}"
: "${TRANSPORT_RUN_ID:?Set TRANSPORT_RUN_ID in .env}"
: "${TRANSPORT_SCHEDULE_VERSION:?Set TRANSPORT_SCHEDULE_VERSION in .env}"

if [ "${TRANSPORT_CLOCK_MODE:-wall}" != "replay" ]; then
  echo 'Set TRANSPORT_CLOCK_MODE=replay in .env and restart backend.' >&2
  exit 1
fi

BASE_URL=${BASE_URL:-http://127.0.0.1:8000}
RUN_ID=$TRANSPORT_RUN_ID
SCHEDULE_VERSION=$TRANSPORT_SCHEDULE_VERSION

DASHBOARD=$(curl -fsS \
  -H "Authorization: Bearer $TRANSPORT_API_TOKEN" \
  "$BASE_URL/api/v1/dashboard?run_id=$RUN_ID&limit=1")
case "$DASHBOARD" in
  *'"clock_mode":"replay"'*) ;;
  *)
    echo 'Backend is not running in replay clock mode. Restart scripts/dev/run-backend.sh.' >&2
    exit 1
    ;;
esac

curl -fsS \
  -H "Authorization: Bearer $TRANSPORT_API_TOKEN" \
  -X POST \
  "$BASE_URL/api/v1/ml/check" >/dev/null

echo '1/4 Importing validation schedule...'
.venv/bin/python tools/data-import/import_schedule.py \
  dataset/validate/schedule_plan.csv \
  --base-url "$BASE_URL" \
  --timezone Europe/Moscow \
  --schedule-version "$SCHEDULE_VERSION"

echo '2/4 Loading 30 minutes of causal history without moving virtual time...'
.venv/bin/python tools/csv-replay/replay.py \
  dataset/validate/traffic.csv \
  --base-url "$BASE_URL" \
  --timezone Europe/Moscow \
  --run-id "$RUN_ID" \
  --from-time '2026-01-06 12:00:00' \
  --until-time '2026-01-06 12:29:59.999999' \
  --no-advance-clock \
  --speed 0

echo '3/4 Setting virtual clock to 12:30 Moscow time...'
curl -fsS \
  -H "Authorization: Bearer $TRANSPORT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -X PUT \
  "$BASE_URL/api/v1/replay-clock?run_id=$RUN_ID" \
  -d '{"as_of":"2026-01-06T12:30:00+03:00"}'
echo

echo '4/4 Live replay: 15 virtual minutes, about 90 seconds at x10.'
echo "Open $BASE_URL and select stream: $RUN_ID"
.venv/bin/python tools/csv-replay/replay.py \
  dataset/validate/traffic.csv \
  --base-url "$BASE_URL" \
  --timezone Europe/Moscow \
  --run-id "$RUN_ID" \
  --from-time '2026-01-06 12:30:00.000001' \
  --until-time '2026-01-06 12:44:59.999999' \
  --speed 10

echo 'Replay demo complete.'
