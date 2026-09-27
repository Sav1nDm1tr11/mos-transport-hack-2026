# tools/csv-replay

Воспроизведение исторической телеметрии через тот же backend API, что и обычный поток.

Replay сохраняет исходные `event_time` и `receive_time`, сортирует события по моменту доступности `receive_time` и двигает виртуальные часы запуска. Если `receive_time` отсутствует, используется `event_time` и добавляется `receive_time_assumed`. `--speed=1` идет в исходном темпе, `--speed=10` ускоряет предметное время в 10 раз, `--speed=0` отправляет без ожиданий.

Для backend в replay-режиме задайте:

```env
TRANSPORT_RUN_ID=validation-demo
TRANSPORT_SCHEDULE_VERSION=validation
TRANSPORT_CLOCK_MODE=replay
TRANSPORT_PREDICTION_INTERVAL=60
TRANSPORT_ML_URL=http://127.0.0.1:8090
```

`TRANSPORT_PREDICTION_INTERVAL=60` согласован с текущим окном актуальности прогноза 90 секунд: dashboard не будет терять покрытие между соседними минутными пересчетами.

Для визуального demo проще использовать готовый сценарий:

```bash
scripts/dev/run-replay-demo.sh
```

Он сначала загружает 30 минут истории с `--no-advance-clock`, затем выставляет виртуальное время на `06.01.2026 12:30 MSK` и примерно за 90 реальных секунд проигрывает 15 виртуальных минут. Scheduler автоматически запускает около 15 prediction cycles, а UI на `http://127.0.0.1:8000/` в потоке из `TRANSPORT_RUN_ID` показывает движение, свежесть и прогнозы относительно виртуального времени.

Ручной запуск анимированной части:

```bash
set -a; source .env; set +a

.venv/bin/python tools/csv-replay/replay.py \
  dataset/validate/traffic.csv \
  --base-url http://127.0.0.1:8000 \
  --timezone Europe/Moscow \
  --run-id "$TRANSPORT_RUN_ID" \
  --from-time "2026-01-06 12:30:00.000001" \
  --until-time "2026-01-06 12:44:59.999999" \
  --speed 10
```

`--no-advance-clock` нужен для pre-roll/warm-up: события сохраняются и доступны модели как история, но не двигают виртуальное `now`. `--limit` применяется после сортировки и фильтрации по виртуальному времени.

Повтор тех же строк безопасен: `event_id` делает телеметрию идемпотентной. После явного отката `/api/v1/replay-clock` повтор уже сохраненных replay-событий снова двигает виртуальные часы, не создавая дубли telemetry.
