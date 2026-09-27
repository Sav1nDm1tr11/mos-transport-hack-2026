# scripts/dev

Локальные команды разработки.

## Backend

```bash
scripts/dev/run-backend.sh
```

Backend слушает `127.0.0.1:8000` и использует `.env`/`TRANSPORT_*` настройки.

## ML service

```bash
scripts/dev/run-ml-service.sh
```

Обязателен `artifacts/models/cold_start_rf/checkpoints/cold_strict.pkl`. `cold_plus_dev.pkl` и full CatBoost подключаются автоматически, если файлы существуют. Пути можно переопределить переменными окружения.

Для end-to-end локального запуска откройте два терминала:

```bash
scripts/dev/run-ml-service.sh
```

```bash
scripts/dev/run-backend.sh
```

В `.env`:

```env
TRANSPORT_ML_URL=http://127.0.0.1:8090
```

После этого UI на `http://127.0.0.1:8000/` получает прогнозы только через backend; браузеру адрес ML-сервиса не нужен.

## Контракты

```bash
.venv/bin/python scripts/dev/export-contracts.py
```

Команда обновляет OpenAPI и JSON Schema из исполняемых Pydantic-контрактов.

## Дополнительные проверки

- `soak.py` -- длительный прогон NDTP;
- model preflight и offline replay описаны в `docs/development/ml-integration.md`.

## Historical replay с живым UI

В `.env`:

```env
TRANSPORT_RUN_ID=validation-demo
TRANSPORT_SCHEDULE_VERSION=validation
TRANSPORT_CLOCK_MODE=replay
TRANSPORT_PREDICTION_INTERVAL=60
TRANSPORT_ML_URL=http://127.0.0.1:8090
```

После перезапуска ML и backend в третьем терминале достаточно:

```bash
scripts/dev/run-replay-demo.sh
```

Скрипт делает causal warm-up, затем около 90 секунд проигрывает 15 виртуальных минут с автоматическими prediction cycles. Откройте `http://127.0.0.1:8000/` и выберите поток из `TRANSPORT_RUN_ID`. Dashboard, карта, свежесть телеметрии и актуальность прогноза используют виртуальное время. Подробности -- `tools/csv-replay/README.md`.
