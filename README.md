# Система прогнозирования задержек транспорта

Рабочее место оператора-аналитика: обнаружение ожидаемых отклонений от расписания, изучение ситуации, наблюдение, сводка и история изменений.

**Статус на 27.09.2026: работающий локальный end-to-end контур.** Реализованы NDTP-приемник, долговременная очередь и SQLite, обработка состояния, REST/SSE, дашборд и отдельный FastAPI `ml-service`. ML-сервис автоматически выбирает cold-start Random Forest, а при наличии full checkpoint и достаточной истории -- CatBoost. Открывайте `/` после запуска backend; [инструкция интерфейса](apps/frontend/README.md), [модели и качество](docs/development/ml-models.md). Целевая распределенная архитектура ниже остается следующим этапом.

**Запуск, API и подключение модели:** [руководство backend](docs/development/backend.md). **Результаты проверок:** [отчёт](docs/operations/acceptance-2026-09-26.md). **План:** [backend core](docs/superpowers/plans/2026-09-26-backend-core.md).

## Исходные документы

- [Архитектура и стек](ARCHITECTURE.md).
- [Техническое задание](docs/superpowers/specs/2026-09-25-transport-delay-system-design.md) -- требования к полной распределенной системе; локальный профиль уже включает ML-интеграцию, но не все инфраструктурные компоненты ТЗ.
- [Исследование сценариев оператора](CJM_RESEARCH.md).
- [Дизайн интерфейса](DESIGN.md).
- [Описание датасета](dataset/README.md) и [спецификация NDTP](dataset/docs/Emulator-and-Telematic-Packets-Specification.md).
- [Навигация по документации](docs/README.md).

При расхождениях по протоколу и конкурсным данным приоритет имеют документы датасета. Состав и поведение полной версии описаны в ТЗ; текущий каркас не означает выполнение этих требований.

## Структура

Ниже сохранена карта исходного каркаса. Исполняемые файлы и тесты, добавленные 26.09.2026, описаны в руководстве backend; дерево не является полным списком текущих файлов.

```text
./
├── README.md
├── ARCHITECTURE.md
├── CJM_RESEARCH.md
├── DESIGN.md
├── dataset/                    # исходные файлы, сохранены без изменений
├── apps/
│   └── frontend/
│       ├── public/
│       ├── src/
│       │   ├── app/
│       │   ├── features/
│       │   │   ├── attention_queue/
│       │   │   ├── data_quality/
│       │   │   ├── history/
│       │   │   ├── incident_details/
│       │   │   ├── map/
│       │   │   ├── overview/
│       │   │   ├── summary/
│       │   │   └── watchlist/
│       │   ├── pages/
│       │   └── shared/
│       │       ├── api/
│       │       ├── config/
│       │       ├── styles/
│       │       └── ui/
│       └── tests/
├── contracts/
│   ├── events/
│   ├── http/
│   └── ml/
├── docs/
│   ├── decisions/
│   ├── development/
│   └── operations/
├── infra/
│   ├── compose/
│   ├── kafka/
│   ├── observability/
│   │   ├── grafana/
│   │   └── prometheus/
│   ├── postgres/
│   │   ├── migrations/
│   │   └── seeds/
│   ├── proxy/
│   └── redis/
├── packages/
│   ├── contracts/
│   │   ├── src/
│   │   └── tests/
│   ├── domain/
│   │   ├── src/
│   │   └── tests/
│   ├── runtime/
│   │   ├── src/
│   │   └── tests/
│   └── storage/
│       ├── src/
│       └── tests/
├── scripts/
│   ├── backup/
│   ├── dev/
│   └── restore/
├── services/
│   ├── backend/
│   │   ├── src/
│   │   │   └── backend/
│   │   │       ├── actions/
│   │   │       ├── api/
│   │   │       ├── auth/
│   │   │       ├── exports/
│   │   │       ├── queries/
│   │   │       └── sse/
│   │   └── tests/
│   ├── ml-service/
│   ├── processing-worker/
│   │   ├── src/
│   │   │   └── processing_worker/
│   │   │       ├── consumption/
│   │   │       ├── incidents/
│   │   │       ├── ml_client/
│   │   │       ├── persistence/
│   │   │       ├── prediction_orchestration/
│   │   │       ├── schedule_matching/
│   │   │       └── vehicle_state/
│   │   └── tests/
│   └── telemetry-gateway/
│       ├── src/
│       │   └── telemetry_gateway/
│       │       ├── normalization/
│       │       ├── protocol/
│       │       └── publishing/
│       └── tests/
├── tests/
│   ├── contract/
│   ├── e2e/
│   ├── fixtures/
│   └── integration/
└── tools/
    ├── admin/
    ├── csv-replay/
    └── data-import/
```

Существующие материалы `docs/superpowers/` также сохранены без изменений. Архив `dataset/ndtp-telemetry-emulator.tar` хранится локально и уже исключён из Git; наличие датасета в репозитории не означает наличие запускаемого эмулятора.

## Поток данных

```text
NDTP → telemetry-gateway ─┐
                         ├→ Kafka → processing-worker ↔ внешний ML API
CSV → csv-replay ────────┘                  ↓
CSV → data-import → PostgreSQL/PostGIS ← результаты и история
                                      Redis ← оперативный кеш
                       PostgreSQL/Redis → backend → REST/SSE → frontend
```

Это целевой поток. Целевой worker использует расписание, фиксирует входной срез прогноза, сохраняет результаты и обновляет ситуации. Backend проверяет доступ и обрабатывает действия пользователя; frontend отображает данные. Административные операции выполняются через будущий `tools/admin` с аудитом.

ML-интеграция реализована по [контракту](contracts/ml/README.md): worker фиксирует immutable batch и вызывает отдельный [`ml-service`](services/ml-service/README.md). В сервисе работают cold-start RF checkpoints, optional full CatBoost и routing по доступному контексту. История результатов сохраняется и отображается в UI вместе с фактическим режимом модели. Обучение и оценка описаны в [ML-документации](docs/development/ml-models.md).

## Где реализовывать задачу

| Задача | Раздел |
|---|---|
| NDTP, нормализация и отправка событий | [telemetry-gateway](services/telemetry-gateway/README.md) |
| Состояние ТС, расписание, оркестрация прогнозов и ситуаций | [processing-worker](services/processing-worker/README.md) |
| REST, права, действия, экспорт, SSE | [backend](services/backend/README.md) |
| Экраны, карта, очередь, карточка, история и сводка | [frontend](apps/frontend/README.md) |
| Общие правила ситуаций | [domain](packages/domain/README.md) |
| Спецификации HTTP, событий и ML | [contracts](contracts/README.md) |
| Исполняемые типы и валидаторы контрактов | [packages/contracts](packages/contracts/README.md) |
| Модели хранения и доступ к БД/кешу | [storage](packages/storage/README.md) |
| Общая конфигурация, логи и метрики | [runtime](packages/runtime/README.md) |
| Импорт, replay и администрирование | [tools](tools/README.md) |
| Compose, хранилища, прокси и наблюдаемость | [infra](infra/README.md) |
| Миграции и начальные данные | [PostgreSQL](infra/postgres/README.md) |
| Подготовка среды, резервное копирование и восстановление | [scripts](scripts/README.md) |
| Контрактные, интеграционные и сквозные проверки | [tests](tests/README.md) |
| Руководства и архитектурные решения | [docs](docs/README.md) |

## Планируемый стек

| Область | Технологии из архитектуры |
|---|---|
| Приём и обработка | Python 3.12, asyncio, confluent-kafka, Pydantic, HTTPX |
| Backend и хранение | FastAPI, SQLAlchemy, Alembic, PostgreSQL/PostGIS, Redis |
| Журнал событий | Apache Kafka, KRaft |
| Frontend | React, TypeScript, Vite, MapLibre GL JS, Apache ECharts |
| Развёртывание и диагностика | Docker Compose, Prometheus, Grafana, JSON-логи |

Зависимости локального ядра закреплены в `requirements.lock.txt`, запуск описан в `docs/development/backend.md`. В `infra/compose/compose.yaml` упакован локальный профиль; Kafka/PostgreSQL/Redis из целевой схемы пока не подключены.

## Как развивать каркас

1. Найти ответственность в таблице и прочитать README подсистемы вместе с соответствующими разделами ТЗ.
2. При изменении обмена согласованно обновлять спецификацию в `contracts/` и типы в `packages/contracts/`; совместимость проверять в `tests/contract/`.
3. Помещать локальные проверки рядом с сервисом или пакетом, а межкомпонентные сценарии — в корневой `tests/`.
4. По мере реализации заменять описание статуса фактическими возможностями и проверенными инструкциями. Не объявлять предусмотренную функцию работающей до проверки.

Исходные CSV и существующие документы сохраняются. Для структуры используются README; пустых исходников, фиктивных конфигураций и автотестов самого каркаса нет.
