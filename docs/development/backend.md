# Backend: запуск и интеграция

Состояние на 27.09.2026: реализован локальный контур NDTP → очередь → обработка → ML → хранение → REST/SSE → UI. Отдельный `ml-service` использует cold-start RF и optional full CatBoost; [модели и качество](ml-models.md). Интерфейс доступен на `/`; [запуск и ограничения](../../apps/frontend/README.md).

## Установка и запуск

Python 3.12+; проверка на macOS ARM64 с Python 3.14.7. Версии проверенных Python-зависимостей закреплены в `requirements.lock.txt`.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m pip install --no-deps -e .
cp .env.example .env
# В .env задайте TRANSPORT_API_TOKEN: случайный секрет длиной не менее 16 символов.
scripts/dev/run-backend.sh
```

В текущей рабочей копии окружение и случайный токен уже созданы. `.env`, `.venv` и данные `.runtime` исключены из Git. Существующий `.env` повторно не перезаписывать.

- Backend: `http://127.0.0.1:8000`, OpenAPI: `/openapi.json`, интерактивная справка: `/docs`.
- NDTP TCP: `127.0.0.1:9201`.
- Health: `/api/v1/health/live`, `/api/v1/health/ready`.
- Бизнес-методы: заголовок `Authorization: Bearer <TRANSPORT_API_TOKEN>`.
- Один процесс/worker на файл БД: блокировка предотвращает второй планировщик. Не включать `--workers 2` или reload.

Readiness отражает работоспособность ядра. Поле `ml=disabled|configured` сообщает конфигурацию, а не гарантирует доступность модели; результат каждого прогноза имеет самостоятельный статус. Неизвестный терминал попадает в диагностику и не создаёт фиктивное ТС.

## Проверка эмулятора

Перед отправкой зарегистрируйте связь устройства и ТС:

```sh
set -a
. ./.env
set +a
curl -X PUT http://127.0.0.1:8000/api/v1/devices/1166336 \
  -H "Authorization: Bearer $TRANSPORT_API_TOKEN" \
  -H 'Content-Type: application/json' -d '{"tr_id":"demo-bus-1"}'
```

Загрузка исходного Docker-образа:

```sh
docker load -i dataset/ndtp-telemetry-emulator.tar
docker run --rm --name transport-emulator --platform linux/amd64 \
  -p 127.0.0.1:18080:18080 ndtp-telemetry-emulator:1.0
curl -X POST http://127.0.0.1:18080/api/config \
  -H 'Content-Type: application/json' \
  -d '{"targetHost":"host.docker.internal","targetPort":9201,"units":[{"unitId":1166336,"intervalMs":1000,"autoGenerate":true,"cells":[]}]}'
```

Для Docker-эмулятора и backend на хосте задайте `TRANSPORT_NDTP_HOST=0.0.0.0` перед запуском backend. По умолчанию TCP доступен только локально. Не публикуйте NDTP в Интернет: эмулятор не поддерживает аутентификацию терминала/TLS/надёжный ACK.

На этом Mac также подготовлен переносимый запуск исходного JAR из архива, без изменения его кода:

```sh
.runtime/jre/jdk-17.0.20.1+1-jre/Contents/Home/bin/java -jar .runtime/emulator/app.jar
```

В этом случае `targetHost=127.0.0.1`. JRE/JAR локальны и не входят в репозиторий. Эмуляция останавливается POST `/api/config` с тем же host/port и `units: []`.

```sh
curl http://127.0.0.1:8000/api/v1/vehicles/demo-bus-1 \
  -H "Authorization: Bearer $TRANSPORT_API_TOKEN"
```

## Docker Compose

```sh
docker compose --env-file .env -f infra/compose/compose.yaml up --build -d
docker load -i dataset/ndtp-telemetry-emulator.tar
docker compose --env-file .env -f infra/compose/compose.yaml --profile emulator up -d
```

Контейнер эмулятора подключать к `targetHost=backend`, `targetPort=9201`. Порты Compose конфликтуют с локальными процессами: сначала остановите локальный backend и Java-эмулятор. Данные контейнерного запуска находятся в отдельном Docker volume; локальная `.runtime/transport.sqlite` автоматически в него не копируется. При подключении ML на Mac: `TRANSPORT_ML_URL=http://host.docker.internal:8090`.

## API ядра

Полная схема: `contracts/http/openapi.json`. Идентификаторы — строки; время в JSON — ISO 8601 с обязательным смещением, нормализация в UTC.

| Метод | Назначение |
|---|---|
| `PUT /api/v1/devices/{unit_id}` | Явная неизменяемая привязка устройства; конфликт — 409 |
| `POST /api/v1/telemetry` | Одиночное нормализованное событие; 202 только после долговременной записи; повтор `accepted=false`, конфликт содержимого — 409 |
| `GET /api/v1/telemetry` | История, `run_id`, `tr_id`, `limit`, `offset` |
| `POST /api/v1/schedules` | Массив плановых посещений; одна версия неизменяема, запрос атомарен |
| `GET /api/v1/schedules` | Посещения настроенной версии расписания |
| `GET /api/v1/vehicles`, `/vehicles/{tr_id}` | Текущая позиция, качество, расписание, последние прогнозы |
| `GET /api/v1/dashboard` | Согласованный снимок и курсор событий; UI не требуется |
| `GET /api/v1/data-issues` | Последние ошибки разбора/привязки/обработки |
| `POST /api/v1/prediction-cycles` | Запрос цикла, `{}` или `{ "as_of": "…+00:00" }`; 202 `queued` либо `no_target` |
| `GET /api/v1/predictions` | История результатов, статусы и версии модели |
| `GET /api/v1/events?after=0` | SSE, возобновление также через `Last-Event-ID`; heartbeat 15 секунд |

Максимальная страница — 200, стандартная — 50. Тело HTTP ограничено 2 MB, очередь — 10 000 ожидающих событий, TCP — 1 000 соединений. Эти параметры настраиваются через `TRANSPORT_*`. Недоступная БД/заполненная очередь возвращают 503. Ошибки имеют `error_code`, `request_id`, заголовок `X-Request-ID`; некорректные входы не отражаются в ответах.

Координаты последней пригодной позиции и время последнего пакета хранятся отдельно. `location_valid` относится к последнему пакету: при false координаты в состоянии могут оставаться от более ранней пригодной позиции (`position_time`). Поле `delay_s=null` означает отсутствие результата, никогда не нулевую задержку.

## Внешняя ML-модель

Точный контракт и пример `/v1/model-info`: `contracts/ml/README.md`. JSON Schema запроса/ответа лежат рядом. Клиент вызывает `/health/ready`, `/v1/model-info`, `/v1/predict-batch`.

1. Реализовать эти методы по контракту, поддержать nullable `cur_dev_s` и схему `1`.
2. Указать `TRANSPORT_ML_URL`, перезапустить backend.
3. Импортировать плановые посещения используемой версии.
4. Подать телеметрию и вызвать `POST /api/v1/prediction-cycles` или дождаться цикла (30 секунд).
5. Проверить `/predictions`: `status`, `mode`, `model_version`, `feature_version`.

Выбирается первое однозначное посещение в окне `(T+10 минут, T+15 минут]`. Одинаковое время двух первых посещений считается неоднозначностью, цель пропускается. Вход сохраняется до HTTP-вызова; повтор использует исходный `request_id` и тот же срез. Будущие наблюдения не включаются. История по умолчанию 30 минут, предел 100 000 событий на цикл; при превышении цикл отклоняется, а не обрезается незаметно. Модель должна согласовать это окно; меняется `TRANSPORT_HISTORY_SECONDS`.

На этапе ядра `cur_dev_s=null`: GPS-оценка текущего отставания не реализована. `schedule_context` содержит выбранные целевые посещения. Соседний граф/сеть не поддерживаются. Не объявляйте такие поля обязательными без расширения интеграции.

Таймаут попытки — 8 секунд, максимум один повтор timeout/502/503/504, общий предел — 20 секунд. Инференс не блокирует приём. Ошибка схемы становится `ml_contract_error`; отсутствующая модель — `ml_disabled`; недоступная — `ml_unavailable`/`ml_timeout`. Частичные результаты и недостаточная история учитываются отдельно. Возобновление ожидающего batch после рестарта безопасно: ML-сервис должен принимать повторы того же request_id идемпотентно.

## Датасет

Часовой пояс файлов не подтверждён владельцем данных, поэтому CLI требует указать его для времён без offset. Для демонстрации ниже Москва — явное допущение, не доказанное свойство датасета.

```sh
set -a; . ./.env; set +a
.venv/bin/python tools/data-import/import_schedule.py dataset/validate/schedule_plan.csv \
  --base-url http://127.0.0.1:8000 --timezone Europe/Moscow --schedule-version validation
.venv/bin/python tools/csv-replay/replay.py dataset/validate/traffic.csv \
  --base-url http://127.0.0.1:8000 --timezone Europe/Moscow --run-id validation --limit 1000
```

Импорт планов читает только плановые поля; `time_fact_begin` и labels не передаются. Replay имеет устойчивые ID файла/строки и отдельный run. Устройство берётся из CSV; отсутствующая/противоречивая связь — явная ошибка. Частично выполненный импорт можно повторить, уже сохранённые одинаковые записи не размножаются.

Для исторического replay запустите backend с нужными `TRANSPORT_RUN_ID`, `TRANSPORT_SCHEDULE_VERSION` и `TRANSPORT_CLOCK_MODE=replay`. Replay двигает часы по `receive_time`; scheduler сравнивает именно виртуальное время и запускает prediction cycle через `TRANSPORT_PREDICTION_INTERVAL` предметных секунд. Dashboard, `/vehicles`, `/telemetry`, история циклов, карта, свежесть телеметрии и покрытие прогнозами читаются на том же causal `as_of`, поэтому заранее загруженные будущие события не попадают в текущий снимок. Пауза replay не старит предметные данные; HTTP timeout и health-check остаются на реальных часах.

Для warm-up replay поддерживает `--no-advance-clock`: история загружается в БД, но виртуальное время не меняется. `PUT /api/v1/replay-clock` позволяет явно поставить или откатить часы; повтор уже сохраненных replay-событий после отката снова продвигает часы без дублей telemetry.

Для визуального validation-demo используйте `TRANSPORT_PREDICTION_INTERVAL=60` и `scripts/dev/run-replay-demo.sh`. Скрипт загружает 30 минут causal history, ставит часы на `12:30 MSK`, а затем примерно за 90 секунд проигрывает `12:30--12:45` с `--speed 10`. Получается около 15 автоматических циклов, при этом интервал пересчета остается меньше текущего TTL прогноза 90 секунд.

## Проверки

```sh
.venv/bin/python -m pytest -q
.venv/bin/python scripts/dev/export-contracts.py
.venv/bin/python scripts/dev/soak.py --duration 60 --units 20 --interval-ms 200
```

Soak использует поставленный эмулятор, изменяет его конфиг, останавливает созданную нагрузку и сохраняет измерения в `.runtime/soak.json`. Не запускать одновременно с чужой демонстрацией.

## Границы поставки

Это локальный E1-профиль, а не полная реализация ТЗ. SQLite WAL обеспечивает транзакционную очередь, историю, входы прогнозов и SSE-журнал в одном процессе. Kafka/PostgreSQL/Redis, разделение по зонам и пользователям, инциденты/действия оператора, PostGIS, автоматическая политика архивирования и часовой профиль приёмки A26 не реализованы. Docker упаковывает этот же профиль, не превращает его в распределённый контур.

Исходные архитектура и ТЗ сохранены как целевой объём. Чтобы перейти к ним, выделить durable inbox в Kafka, проекции/историю в PostgreSQL и кеш Redis, сохраняя существующие контракты и проверки. До этого не объявлять промышленную отказоустойчивость или гарантированную доставку NDTP: эмулятор не подтверждает приём на стороне приложения.
