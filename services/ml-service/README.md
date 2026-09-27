# ML service

FastAPI-сервис инференса с автоматическим routing между cold-start RF и optional full CatBoost.

## Режимы

- `COLD_STRICT` -- `cold_strict.pkl`, когда `cur_dev_s` отсутствует;
- `COLD_PLUS_DEV` -- `cold_plus_dev.pkl`, когда `cur_dev_s` доступен;
- `FULL_CATBOOST` -- `catboost_final.cbm`, когда модель загружена, `cur_dev_s` известен и накоплена достаточная история.

Backend не выбирает модель. `processing-worker` всегда отправляет один `PredictionBatch`, а `ml-service` возвращает обычный `PredictionResult` с `reason_codes`, `model_version` и `feature_version`.

> Текущий live worker пока не оценивает `cur_dev_s`, поэтому без внешнего источника реальные online-запросы будут идти через `COLD_STRICT`. `COLD_PLUS_DEV` и `FULL_CATBOOST` включатся автоматически после появления корректного `cur_dev_s` в `PredictionTarget`.

Подробности качества и обучения: [`docs/development/ml-models.md`](../../docs/development/ml-models.md).

## Локальный запуск

Из корня проекта, в том же `.venv`, где доступны версии sklearn/catboost для checkpoints:

```bash
PYTHONPATH=services/ml-service/src:packages/contracts/src \
COLD_STRICT_MODEL_PATH=artifacts/models/cold_start_rf/checkpoints/cold_strict.pkl \
COLD_PLUS_DEV_MODEL_PATH=artifacts/models/cold_start_rf/checkpoints/cold_plus_dev.pkl \
FULL_CATBOOST_MODEL_PATH=artifacts/models/catboost/checkpoints/catboost_final.cbm \
python -m uvicorn ml_service.app:app --host 0.0.0.0 --port 8090
```

Full CatBoost optional. Если `.cbm` отсутствует или не загрузился, сервис остается `ready` и работает на cold-start RF.

Worker подключается через:

```env
TRANSPORT_ML_URL=http://127.0.0.1:8090
```

Проверка:

```bash
curl http://127.0.0.1:8090/health/ready
```

Ответ показывает `available_modes`.

## Routing full-модели

Порог задается окружением:

```env
FULL_HISTORY_MINUTES=30
FULL_MIN_SPAN_MINUTES=15
FULL_MIN_OBSERVATIONS=6
```

Full CatBoost включается для конкретной точки, если:

1. `.cbm` успешно загружен;
2. `cur_dev_s != null`;
3. в указанном окне достаточно валидных GPS-наблюдений;
4. span истории не меньше порога.

Иначе автоматически используется одна из cold-start моделей.

## API

- `GET /health/live`;
- `GET /health/ready`;
- `GET /v1/model-info`;
- `POST /v1/predict-batch`.

Контракт: [`contracts/ml/README.md`](../../contracts/ml/README.md).

## Train-serving consistency

Feature builder соблюдает `as_of` и использует `available_time = max(event_time, receive_time)`. Full builder восстанавливает online-safe rolling/schedule/spatial признаки. Offline-only признаки остаются missing -- CatBoost умеет с ними работать, но это может ухудшить качество относительно offline MAE.

## Docker

Compose монтирует весь `artifacts/models` read-only в `/models`. Cold checkpoints обязательны, CatBoost optional.

```bash
docker compose --env-file .env -f infra/compose/compose.yaml up --build -d
```

Pickle sklearn должен загружаться совместимой версией `scikit-learn`. Strong cold-start notebook записывает версию библиотеки в checkpoint, и сервис предупреждает при несовпадении.
