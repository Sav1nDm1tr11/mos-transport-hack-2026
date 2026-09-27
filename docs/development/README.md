# docs/development

Рабочие руководства для локального backend, ML-интеграции и развития контрактов.

## Основные документы

- [backend.md](backend.md) -- запуск backend, API, NDTP и Docker Compose;
- [ml-integration.md](ml-integration.md) -- запуск `ml-service`, preflight и offline replay;
- [ml-models.md](ml-models.md) -- модели, качество, cold start и production-routing.

## Правило изменения ML-контракта

Если меняется `PredictionBatch`, `ModelInfo` или `PredictionResult`, нужно согласованно обновить:

1. Pydantic-модели в `packages/contracts`;
2. JSON Schema в `contracts/ml` через `scripts/dev/export-contracts.py`;
3. `processing-worker`/ML client;
4. `ml-service` feature builder;
5. документацию и contract/integration tests.

## Связанные документы

- [Карта проекта](../../README.md)
- [Архитектура](../../ARCHITECTURE.md)
- [Техническое задание](../superpowers/specs/2026-09-25-transport-delay-system-design.md)
