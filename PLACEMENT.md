# Куда положить файлы

В архиве уже сохранена правильная структура каталогов.

## 1. `README.md`

Путь в репозитории:

```text
/README.md
```

Что делать:

- полностью заменить текущий корневой `README.md`;
- старый README лучше не объединять с новым вручную, потому что в нём остались устаревшие ссылки на целевой Kafka/Postgres/Redis runtime.

## 2. `docs/JURY_GUIDE.md`

Путь:

```text
/docs/JURY_GUIDE.md
```

Что делать:

- это новый файл;
- положить его в `docs/`;
- на него уже ссылается новый корневой `README.md`.

Это основной файл, который стоит отправлять жюри как инструкцию запуска.

## 3. `docs/development/ml-models.md`

Путь:

```text
/docs/development/ml-models.md
```

Что делать:

- полностью заменить текущий `docs/development/ml-models.md`;
- новая версия исправляет устаревшее отсутствие test MAE у Sequence RNN;
- явно описывает 706 признаков;
- честно объясняет разницу между offline feature engineering и online-serving;
- отдельно фиксирует нюанс `cur_dev_s=null` в текущем worker.

## 4. Что желательно поправить следующим коммитом

После этих трёх файлов желательно пройти старые README и убрать противоречия.

В первую очередь:

```text
tests/README.md
infra/compose/README.md
infra/kafka/README.md
infra/postgres/README.md
infra/redis/README.md
infra/observability/README.md
apps/frontend/src/features/*/README.md
docs/operations/acceptance-2026-09-26.md
```

### `tests/README.md`

Сейчас пишет, что тесты ещё не реализованы. Это уже неверно.

Нужно описать реальные:

- contract tests;
- integration tests;
- e2e real-process tests;
- NDTP protocol tests;
- ML routing tests;
- resilience/safety tests.

### `infra/*`

Kafka/PostgreSQL/Redis/Prometheus/Grafana лучше пометить явно:

```text
Статус: архитектурная заготовка. Текущий runtime этот компонент не использует.
```

И дать ссылку на root README.

### frontend feature README

Папки `attention_queue`, `incident_details`, `watchlist` и т.п. выглядят как будто внутри есть отдельная реализованная feature architecture.

В текущем runtime основная реализация находится в:

```text
apps/frontend/src/app.js
apps/frontend/src/api.js
apps/frontend/src/map.js
apps/frontend/src/model.js
apps/frontend/src/index.html
apps/frontend/src/styles.css
```

Вложенные README лучше обозначить как design decomposition / planned modularization.

### `docs/operations/acceptance-2026-09-26.md`

Это исторический документ.

В нём остались фразы, что UI, настоящая модель и replay-clock ещё не реализованы.

Лучший вариант — не удалять исторический отчёт, а добавить сверху:

```markdown
> Исторический отчёт: состояние на 26.09.2026.
> Текущий статус проекта см. в `/README.md`, `/docs/JURY_GUIDE.md`
> и `/docs/development/ml-models.md`.
```

И не использовать его как главный документ для жюри.

## Самый простой способ установки

Распаковать архив в корень клона с заменой файлов:

```bash
unzip mos-transport-docs.zip -d /path/to/mos-transport-hack-2026
```

Внутри ZIP лежат:

```text
README.md
docs/
├── JURY_GUIDE.md
└── development/
    └── ml-models.md
PLACEMENT.md
```

`PLACEMENT.md` можно не коммитить — это памятка для вас.
