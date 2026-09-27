# services

Самостоятельные серверные процессы локального профиля.

## Поток

```text
NDTP -> backend/worker -> ml-service -> storage -> REST/SSE -> frontend
```

В целевой архитектуре gateway/worker/storage разделяются сильнее, но текущий профиль уже сохраняет внешнюю ML-границу.

## Состав

- [backend](backend/README.md) -- API рабочего места оператора, SSE и UI;
- [processing-worker](processing-worker/README.md) -- состояние ТС, расписание, подготовка prediction batch и вызов ML;
- [ml-service](ml-service/README.md) -- cold-start RF, optional full CatBoost и model routing;
- [telemetry-gateway](telemetry-gateway/README.md) -- NDTP-разбор и нормализация.

## ML-граница

Worker передает immutable `PredictionBatch`; модельная предобработка и выбор конкретной модели остаются внутри `ml-service`. Браузер к ML-сервису напрямую не обращается.

## Связанные документы

- [Карта проекта](../README.md)
- [ML-модели и routing](../docs/development/ml-models.md)
- [Архитектура](../ARCHITECTURE.md)
