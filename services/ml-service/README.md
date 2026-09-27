# ML service

FastAPI-обертка текущего cold-start Random Forest.

Режим выбирается внутри сервиса:

- `cur_dev_s is None` -- `cold_strict.pkl`;
- `cur_dev_s` доступен -- `cold_plus_dev.pkl`;
- позже сюда же добавляется full CatBoost после накопления истории.

## Локальный запуск

Сервис лучше запускать из того же `.venv`, в котором сохранены sklearn checkpoints.

```bash
PYTHONPATH=services/ml-service/src:packages/contracts/src \
COLD_STRICT_MODEL_PATH=artifacts/models/cold_start_rf/checkpoints/cold_strict.pkl \
COLD_PLUS_DEV_MODEL_PATH=artifacts/models/cold_start_rf/checkpoints/cold_plus_dev.pkl \
python -m uvicorn ml_service.app:app --host 0.0.0.0 --port 8090
```

Worker подключается через:

```env
TRANSPORT_ML_URL=http://127.0.0.1:8090
```

## Docker

Перед сборкой Docker стоит зафиксировать ту же версию `scikit-learn`, на которой сохранен pickle.
Новая strong-версия ноутбука записывает версии библиотек внутрь checkpoint.
