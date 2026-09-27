# Инструкция для жюри: запуск и проверка решения

Эта страница — **каноническая инструкция запуска текущего `main`**.

Есть два независимых демонстрационных сценария:

1. **Docker + настоящий NDTP-эмулятор** — проверка живого TCP-потока, парсинга NDTP, хранения и realtime UI.
2. **Historical replay + ML** — детерминированная демонстрация полного контура расписание → телеметрия → prediction cycle → ML → dashboard.

> Важно: исходный файл `dataset/ndtp-telemetry-emulator.tar` выдан организаторами, но намеренно не хранится в Git. Для сценария с NDTP-эмулятором положите этот архив в `dataset/`. Всё остальное, включая checkpoints моделей, находится в репозитории.

---

# 1. Требования

Для основного Docker-сценария нужны:

- Git;
- Docker Engine / Docker Desktop;
- Docker Compose v2 (`docker compose`);
- свободные локальные порты `8000`, `8090`, `9201`, `18080`.

Проверка:

```bash
docker --version
docker compose version
```

На Apple Silicon исходный образ эмулятора запускается через эмуляцию `linux/amd64`; это уже указано в Compose.

---

# Сценарий A. Docker + NDTP-эмулятор

## 2. Клонируем проект

```bash
git clone https://github.com/Sav1nDm1tr11/mos-transport-hack-2026.git
cd mos-transport-hack-2026
```

## 3. Создаём `.env`

Для Docker Compose достаточно:

```bash
cat > .env <<'EOF'
TRANSPORT_API_TOKEN=jury-local-token-change-me-2026
TRANSPORT_ML_URL=http://ml-service:8090
EOF
```

Токен должен быть длиной не менее 16 символов.

**Почему здесь не `127.0.0.1:8090`:** внутри контейнера backend адрес `127.0.0.1` указывает на сам backend. Имя `ml-service` — DNS-имя ML-контейнера в сети Compose.

## 4. Поднимаем backend + ML

```bash
docker compose --env-file .env -f infra/compose/compose.yaml up --build -d ml-service backend
```

Проверяем:

```bash
curl http://127.0.0.1:8000/api/v1/health/live
curl http://127.0.0.1:8000/api/v1/health/ready
curl http://127.0.0.1:8090/health/ready
curl http://127.0.0.1:8090/v1/model-info
```

Backend:

```text
http://127.0.0.1:8000/
```

Swagger:

```text
http://127.0.0.1:8000/docs
```

ML service:

```text
http://127.0.0.1:8090/
```

В UI введите тот же `TRANSPORT_API_TOKEN`, который задан в `.env`.

## 5. Загружаем образ эмулятора

Положите выданный организаторами файл в:

```text
dataset/ndtp-telemetry-emulator.tar
```

Затем:

```bash
docker load -i dataset/ndtp-telemetry-emulator.tar
docker image inspect ndtp-telemetry-emulator:1.0 >/dev/null
```

## 6. Запускаем эмулятор

```bash
docker compose --env-file .env -f infra/compose/compose.yaml --profile emulator up -d emulator
```

Конфигурация задаётся через `POST /api/config`.

## 7. Регистрируем терминал в backend

Backend специально **не создаёт ТС из неизвестного NDTP unit_id автоматически**. Это защита от случайной/ошибочной идентичности терминала.

```bash
set -a
. ./.env
set +a

curl -X PUT http://127.0.0.1:8000/api/v1/devices/1166336 \
  -H "Authorization: Bearer $TRANSPORT_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"tr_id":"demo-bus-1"}'
```

Привязка `unit_id → tr_id` неизменяема: попытка перепривязать тот же терминал к другому ТС вернёт конфликт.

## 8. Включаем живой NDTP-поток

Эмулятор и backend находятся в одной Docker-сети, поэтому target host — имя сервиса `backend`:

```bash
curl -X POST http://127.0.0.1:18080/api/config \
  -H 'Content-Type: application/json' \
  -d '{
    "targetHost":"backend",
    "targetPort":9201,
    "units":[
      {
        "unitId":1166336,
        "intervalMs":1000,
        "autoGenerate":true,
        "cells":[]
      }
    ]
  }'
```

Через несколько секунд проверяем состояние:

```bash
curl http://127.0.0.1:8000/api/v1/vehicles/demo-bus-1 \
  -H "Authorization: Bearer $TRANSPORT_API_TOKEN"
```

Или откройте:

```text
http://127.0.0.1:8000/
```

ТС появится в списке и на карте после получения валидной позиции.

### Что именно проверяет этот сценарий

```text
NDTP emulator
    ↓ TCP :9201
telemetry-gateway
    ↓ handshake / frame parsing / CRC / normalization
durable SQLite inbox
    ↓
processing worker
    ↓
vehicle state + data quality
    ↓
REST / SSE
    ↓
dashboard
```

Это **настоящий NDTP-путь**, а не отправка готового JSON напрямую в API.

## 9. Остановка эмуляции

Сначала можно остановить генерацию пакетов:

```bash
curl -X POST http://127.0.0.1:18080/api/config \
  -H 'Content-Type: application/json' \
  -d '{"targetHost":"backend","targetPort":9201,"units":[]}'
```

Полностью остановить стенд:

```bash
docker compose --env-file .env -f infra/compose/compose.yaml --profile emulator down
```

Данные backend хранятся в Docker volume `transport-data`.

Для полностью чистого запуска:

```bash
docker compose --env-file .env -f infra/compose/compose.yaml --profile emulator down -v
```

---

# Сценарий B. Полный ML-demo на historical replay

Этот сценарий удобнее для проверки именно прогноза: он использует validation-расписание и телеметрию из датасета, виртуальные часы и автоматически запускает prediction cycles.

## 10. Локальное Python-окружение

Нужен Python 3.12+.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m pip install -r services/ml-service/requirements.txt
.venv/bin/python -m pip install --no-deps -e .
```

Создайте `.env`:

```bash
cat > .env <<'EOF'
TRANSPORT_API_TOKEN=jury-local-token-change-me-2026
TRANSPORT_DB_PATH=.runtime/transport.sqlite
TRANSPORT_NDTP_HOST=127.0.0.1
TRANSPORT_NDTP_PORT=9201

TRANSPORT_RUN_ID=validation-demo
TRANSPORT_SCHEDULE_VERSION=validation
TRANSPORT_PREDICTION_INTERVAL=60
TRANSPORT_CLOCK_MODE=replay

TRANSPORT_ML_URL=http://127.0.0.1:8090

COLD_STRICT_MODEL_PATH=artifacts/models/cold_start_rf/checkpoints/cold_strict.pkl
COLD_PLUS_DEV_MODEL_PATH=artifacts/models/cold_start_rf/checkpoints/cold_plus_dev.pkl
FULL_CATBOOST_MODEL_PATH=artifacts/models/catboost/checkpoints/catboost_final.cbm

FULL_HISTORY_MINUTES=30
FULL_MIN_SPAN_MINUTES=15
FULL_MIN_OBSERVATIONS=6
EOF
```

## 11. Запускаем сервисы

Терминал 1:

```bash
set -a; . ./.env; set +a
scripts/dev/run-ml-service.sh
```

Терминал 2:

```bash
set -a; . ./.env; set +a
scripts/dev/run-backend.sh
```

Проверка:

```bash
curl http://127.0.0.1:8090/health/ready
curl http://127.0.0.1:8000/api/v1/health/ready
```

## 12. Запускаем готовый replay

Терминал 3:

```bash
scripts/dev/run-replay-demo.sh
```

Скрипт сам:

1. импортирует `dataset/validate/schedule_plan.csv`;
2. загружает 30 минут истории как causal warm-up, не двигая virtual clock;
3. ставит виртуальное время на 06.01.2026 12:30 MSK;
4. воспроизводит 15 виртуальных минут примерно за 90 реальных секунд при `--speed 10`;
5. scheduler запускает prediction cycles по предметному времени.

Во время replay откройте:

```text
http://127.0.0.1:8000/
```

и выберите поток:

```text
validation-demo
```

---

# 13. Что смотреть в интерфейсе

На главном экране доступны:

- текущие ТС и последняя пригодная GPS-позиция;
- свежесть последнего пакета;
- поиск и фильтрация;
- интерактивная карта;
- прогноз задержки и целевая остановка;
- model/feature version и фактический ML mode;
- причины отсутствия/ошибки прогноза;
- покрытие свежими прогнозами;
- последние prediction cycles и их длительность;
- backlog прогнозирования;
- data issues;
- экспорт прогнозов в CSV.

Обновления идут через SSE. При разрыве соединения UI переподключается, умеет resync по cursor и использует контрольный polling.

---

# 14. Важный нюанс ML routing

ML-сервис реализует три режима:

```text
FULL_CATBOOST  — cur_dev_s известен + достаточно истории
COLD_PLUS_DEV  — cur_dev_s известен, но full-контекста недостаточно
COLD_STRICT    — cur_dev_s отсутствует
```

Текущий live worker **не вычисляет `cur_dev_s` самостоятельно**, поэтому стандартный end-to-end online/replay запрос из backend сейчас попадает в `COLD_STRICT`.

Это не fallback «после ошибки»: это осознанный cold-start режим.

`COLD_PLUS_DEV` и `FULL_CATBOOST` полностью реализованы в ML service, покрыты routing-тестами и автоматически включаются, когда `PredictionTarget` содержит корректный `cur_dev_s`.

---

# 15. Диагностика

## Backend не видит ML

Для Docker:

```env
TRANSPORT_ML_URL=http://ml-service:8090
```

Для локального запуска:

```env
TRANSPORT_ML_URL=http://127.0.0.1:8090
```

Логи:

```bash
docker compose --env-file .env -f infra/compose/compose.yaml logs ml-service backend
```

## Эмулятор работает, но ТС не появляется

Проверьте:

1. создана ли привязка `unit_id → tr_id`;
2. `targetHost=backend` в Compose;
3. `targetPort=9201`;
4. логи:

```bash
docker compose --env-file .env -f infra/compose/compose.yaml logs -f backend emulator
```

Ошибки неизвестного устройства, CRC и протокола доступны также через `GET /api/v1/data-issues`.

## ML check

```bash
curl -X POST \
  http://127.0.0.1:8000/api/v1/ml/check \
  -H "Authorization: Bearer $TRANSPORT_API_TOKEN"
```

## Порт уже занят

```bash
docker compose --env-file .env -f infra/compose/compose.yaml --profile emulator down
```

Либо остановите локальные процессы на 8000/8090/9201/18080.

## Нужно начать с пустой БД

Docker:

```bash
docker compose --env-file .env -f infra/compose/compose.yaml --profile emulator down -v
```

Локально:

```bash
rm -f .runtime/transport.sqlite \
      .runtime/transport.sqlite-shm \
      .runtime/transport.sqlite-wal \
      .runtime/transport.sqlite.lock
```

---

# 16. Быстрая проверка API

```bash
set -a
. ./.env
set +a

curl "http://127.0.0.1:8000/api/v1/dashboard?run_id=live" \
  -H "Authorization: Bearer $TRANSPORT_API_TOKEN"

curl "http://127.0.0.1:8000/api/v1/predictions?run_id=live" \
  -H "Authorization: Bearer $TRANSPORT_API_TOKEN"

curl -X POST \
  http://127.0.0.1:8000/api/v1/ml/check \
  -H "Authorization: Bearer $TRANSPORT_API_TOKEN"
```

Полная OpenAPI-схема доступна на:

```text
http://127.0.0.1:8000/docs
```

и в `contracts/http/openapi.json`.

---

# 17. Что текущая поставка сознательно не заявляет

Чтобы не смешивать рабочий код с целевой архитектурой:

- Kafka **не используется** текущим runtime;
- PostgreSQL/PostGIS **не используются** текущим runtime;
- Redis **не используется** текущим runtime;
- frontend сейчас — dependency-free ES modules + CSS, а не React;
- полноценного map matching по дорожному графу нет;
- What-if оптимизатора диспетчерских действий нет;
- ONNX/TensorRT-квантизации нет;
- Sequence RNN обучен и исследован offline, но не подключён к production router;
- промышленного multi-node HA/RBAC/TLS нет.

Текущий интеграционный профиль намеренно собран вокруг SQLite WAL и одного backend-процесса, зато весь путь NDTP → durable storage → state → ML → REST/SSE → UI можно воспроизвести локально.
