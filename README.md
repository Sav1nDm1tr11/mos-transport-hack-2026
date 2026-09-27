# Система раннего прогнозирования задержек наземного транспорта

Система принимает потоковую телеметрию ТС в формате NDTP, сопоставляет её с плановым расписанием и строит прогноз отклонения от графика для первой остановки в горизонте **10–15 минут**.

На 27.09.2026 в репозитории реализован рабочий end-to-end контур:

```text
NDTP / historical replay
        ↓
telemetry gateway
        ↓
durable inbox
        ↓
processing worker
        ↓
prediction batch
        ↓
ML service
        ↓
SQLite
        ↓
REST / SSE
        ↓
dispatcher dashboard
```

Это не макет: прием NDTP, хранение, обработка состояния ТС, планировщик прогнозов, отдельный ML-сервис, REST/SSE и UI работают совместно и покрыты интеграционными и e2e-тестами.

## Быстрый старт для жюри

Полная пошаговая инструкция находится в [`docs/JURY_GUIDE.md`](docs/JURY_GUIDE.md).

Там есть два сценария:

1. **Docker + настоящий NDTP-эмулятор** — проверка живого TCP-потока, NDTP parser, хранения и realtime UI.
2. **Historical replay + ML** — детерминированная демонстрация полного prediction pipeline на validation-данных.

> Важно: `dataset/ndtp-telemetry-emulator.tar` выдан организаторами и не хранится в Git. Для проверки настоящего NDTP-эмулятора архив нужно положить в `dataset/`.

---

# 1. Что реализовано

## 1.1. Приём телеметрии

Backend поднимает отдельный TCP-сервер NDTP.

Реализованы:

- handshake NDTP 6.2;
- проверка структуры frame;
- CRC16 Modbus;
- разбор realtime navigation packet;
- координаты, скорость, направление движения;
- проверка GPS validity;
- защита от подмены identity внутри соединения;
- обработка fragmented TCP frames;
- изоляция ошибок отдельного соединения;
- настраиваемый лимит до 1000 одновременных TCP-соединений.

Первый пакет соединения обязан быть handshake. Повреждённый или неизвестный пакет фиксируется как data issue и не останавливает сервис.

## 1.2. Durable ingestion

После нормализации телеметрия сначала записывается в persistent inbox и только после этого считается принятой.

SQLite работает с:

- `WAL`;
- `synchronous=FULL`;
- foreign keys;
- атомарными транзакциями;
- индексами по ТС и времени;
- очередью необработанных событий.

Повторная доставка одного события безопасна. Семантический digest не включает транспортные поля вроде `ingested_at`, поэтому сетевой retry не создаёт новое событие.

Если одинаковый `event_id` приходит с другим содержимым — это конфликт, а не молчаливое перезаписывание.

## 1.3. Состояние ТС

Worker отдельно поддерживает:

- время последнего пакета;
- последнее состояние;
- последнюю **валидную** GPS-позицию;
- `position_time`;
- скорость и heading;
- diagnostic flags.

Если новый пакет сообщает невалидные координаты, ТС не перемещается в `(0, 0)`: в интерфейсе остаётся последняя пригодная GPS-позиция с соответствующим статусом качества.

Поздний старый пакет не откатывает актуальное состояние.

## 1.4. Работа с расписанием

Расписание импортируется отдельно и версионируется.

Для момента прогноза `T` worker ищет первое плановое посещение ТС строго в окне:

```text
(T + 10 минут, T + 15 минут]
```

Таким образом горизонт, заданный конкурсной постановкой, обеспечивается самим orchestration layer, а не только моделью.

Если две первые цели имеют одинаковое плановое время и выбор неоднозначен, прогноз для такого ТС не строится на случайной остановке.

---

# 2. ML-сервис

ML вынесен в отдельный FastAPI-сервис.

Backend не знает внутреннее устройство модели и работает через версионированный контракт:

```text
GET  /health/ready
GET  /v1/model-info
POST /v1/predict-batch
```

Перед inference worker сохраняет immutable `PredictionBatch` с:

- `as_of`;
- run и schedule version;
- целевыми остановками;
- доступной на этот момент телеметрией;
- schedule context;
- deterministic request/prediction IDs.

После этого сетевой вызов ML можно безопасно повторить без изменения входа.

Подробное описание моделей, признаков и обучения находится в [`docs/development/ml-models.md`](docs/development/ml-models.md).

## 2.1. Production routing и cold start

ML service содержит три режима:

```text
FULL_CATBOOST
COLD_PLUS_DEV
COLD_STRICT
```

### COLD_STRICT

Используется, если `cur_dev_s` неизвестен.

Это полноценная Random Forest модель, которая работает по доступным online данным:

- положению ТС;
- положению целевой остановки;
- расписанию;
- времени;
- расстояниям;
- направлению;
- скорости;
- географическим grid-признакам.

То есть новое ТС не остаётся вообще без прогноза только потому, что для него ещё нет богатой истории.

### COLD_PLUS_DEV

Если известно текущее отклонение `cur_dev_s`, используется отдельная RF-модель с дополнительными признаками текущего отклонения.

### FULL_CATBOOST

Используется, когда одновременно:

- присутствует `cur_dev_s`;
- загружен CatBoost checkpoint;
- накоплено не менее 6 пригодных наблюдений;
- наблюдения покрывают не менее 15 минут;
- анализируется окно истории до 30 минут.

Пороговые значения настраиваются.

> Текущий штатный live-worker пока не вычисляет `cur_dev_s`, поэтому обычный end-to-end запрос из backend сейчас обычно маршрутизируется в `COLD_STRICT`. `COLD_PLUS_DEV` и `FULL_CATBOOST` реализованы в ML service и покрыты routing-тестами, но требуют передачи `cur_dev_s`.

---

# 3. Модели

В ходе исследования были обучены и проверены несколько семейств моделей.

| Модель | Real test MAE |
|---|---:|
| Ridge | 65.91 с |
| Random Forest | 43.06 с |
| Sequence LSTM | 41.28 с |
| CatBoost | **35.23 с** |
| Cold RF + `cur_dev_s` | 47.32 с |
| Cold RF strict | 47.55 с |
| baseline `cur_dev_s` | 93.36 с |

CatBoost используется как full-context production model.

Cold RF используется не вместо основной модели, а как отдельный production-сценарий для неполного контекста.

Sequence LSTM исследована offline и в текущий production router не подключена.

---

# 4. Feature engineering

Offline pipeline формирует **706 признаков**.

Основные семейства:

## 4.1. Расписание

- позиция остановки внутри графика;
- число остановок;
- progress по расписанию;
- интервалы до предыдущей/следующей остановки;
- длины плановых сегментов;
- требуемая плановая скорость;
- headway;
- число остановок до target;
- длина proxy-маршрута.

## 4.2. Время

- горизонт в секундах/минутах;
- minute of day;
- hour;
- sin/cos времени;
- аналогичные признаки времени target.

## 4.3. Текущее отклонение

При наличии `cur_dev_s`:

- абсолютное значение;
- знак;
- signed log;
- sqrt;
- square;
- deviation per minute;
- взаимодействие с расстоянием, горизонтом, скоростью и маршрутом.

## 4.4. GPS и физика движения

- последняя позиция;
- последняя валидная позиция;
- возраст GPS;
- расстояние до target;
- bearing;
- heading error;
- heading alignment;
- projected speed;
- required speed;
- speed margin;
- ETA;
- ETA margin.

## 4.5. Оконные признаки телеметрии

Используются окна:

```text
1 / 3 / 5 / 10 / 15 / 30 минут
```

Для них считаются:

- число пакетов;
- span;
- packets/min;
- доля валидного GPS;
- mean/median/std/min/max/q10/q90 скорости;
- stopped/slow/fast share;
- изменение и slope скорости;
- stop transitions;
- задержки receive/event time;
- gaps между событиями;
- направление;
- path length;
- displacement;
- tortuosity;
- progress в сторону target.

Только оконных признаков в сохранённой offline feature matrix — более 450.

## 4.6. Контекст соседних ТС

Для радиусов:

```text
300 / 500 / 1000 / 2000 м
```

считаются:

- число других ТС;
- средняя/медианная скорость;
- доля медленно движущихся ТС;

как около текущего положения, так и около target.

Это позволяет модели учитывать локальное дорожное состояние без полноценного дорожного графа.

## 4.7. Дополнительные преобразования

Также используются:

- log/signed-log преобразования;
- ratio;
- cross features;
- power features;
- grid features;
- 14 PCA-компонент физически интерпретируемого ядра offline-признаков.

Production feature builder намеренно не подставляет unavailable offline-информацию. Недоступные online признаки остаются missing.

---

# 5. Как обучались модели

Главная метрика — **MAE в секундах**.

Для модели нельзя использовать обычный random split: соседние точки одного ТС сильно коррелированы по времени.

Используется purged blocked validation:

1. реальные ТС сортируются по времени;
2. validation формируется временными блоками;
3. вокруг validation удаляется зона ±45 минут;
4. это покрывает 30 минут feature history и 15 минут prediction horizon;
5. synthetic ТС могут участвовать только в train;
6. их вес подбирается отдельно;
7. официальный test не участвует в Optuna и используется после выбора модели.

Так снижается риск leakage между близкими prediction points.

Гиперпараметры Ridge, Random Forest, CatBoost, Sequence RNN и Cold RF подбирались через Optuna.

---

# 6. Sequence model

Отдельно исследована sequence-модель на PyTorch.

Финальная архитектура:

- bidirectional LSTM;
- 3 recurrent layers;
- hidden size 96;
- последовательность до 20 последних минут;
- шаг 60 секунд;
- отдельная tabular-ветка;
- fusion MLP.

На каждом временном шаге формируются 16 sequence features, включая:

- число пакетов;
- GPS validity;
- среднюю/последнюю скорость;
- stopped share;
- heading sin/cos;
- receive/GPS lag;
- относительное положение до target.

Модель показала real test MAE **41.28 с**, но текущая production-версия использует CatBoost/RF router как более простой и воспроизводимый deployment path.

---

# 7. Realtime prediction pipeline

```text
NDTP frame
   ↓
parse + CRC
   ↓
normalize
   ↓
durable enqueue
   ↓
vehicle state projection
   ↓
scheduler
   ↓
target selection: T+10..T+15
   ↓
immutable PredictionBatch
   ↓
ML compatibility check
   ↓
model router
   ↓
prediction
   ↓
atomic persistence
   ↓
SSE
   ↓
dashboard
```

Приём телеметрии и ML inference работают в независимых asyncio-задачах. Медленная модель не блокирует TCP ingestion.

ML client имеет:

- timeout до 8 секунд на попытку;
- один bounded retry для timeout/502/503/504;
- общий budget до 20 секунд;
- проверку schema/model/feature versions;
- максимальный response size;
- per-target обработку отсутствующих/некорректных результатов.

---

# 8. Historical replay

Реализован отдельный replay исторической телеметрии.

Он:

- сохраняет исходные `event_time`;
- учитывает `receive_time`;
- сортирует события по моменту фактической доступности;
- имеет virtual clock;
- поддерживает `--speed`;
- поддерживает causal warm-up;
- позволяет откатывать виртуальное время;
- не показывает future telemetry до наступления соответствующего virtual time.

Это позволяет демонстрировать реальный scheduler и ML на исторических данных без нарушения temporal causality.

---

# 9. Dashboard

Frontend написан на dependency-free ES modules + CSS и отдаётся непосредственно backend.

Реализованы:

- список ТС;
- карта OpenStreetMap;
- поиск;
- фильтрация;
- свежесть данных;
- карточка ТС;
- target stop;
- прогноз задержки;
- модель и feature version;
- фактический ML mode;
- reason codes;
- prediction coverage;
- prediction cycle history;
- ML backlog;
- data issues;
- CSV export.

Realtime обновления идут через SSE.

Поддерживаются:

- cursor;
- `Last-Event-ID`;
- `resync_required`;
- heartbeat;
- reconnect;
- polling fallback.

---

# 10. Производительность

Сохранены два нагрузочных прогона.

| Профиль | ТС | Время | События | Throughput | HTTP p95 | Остаток |
|---|---:|---:|---:|---:|---:|---:|
| local | 20 | 120 с | 11 988 | 99.899 event/s | 11.886 ms | 0 |
| Docker | 60 | 60 с | 17 975 | **299.581 event/s** | 19.447 ms | 0 |

Это подтверждённые рабочие точки, а **не заявленный предел производительности**.

В обоих сохранённых прогонах:

- request errors: 0;
- final pending: 0;
- invalid issue delta: 0.

---

# 11. Надёжность

Тестами проверены в том числе:

- 100 конкурентных доставок одного события;
- atomic rollback при ошибке projection;
- persistence после рестарта;
- fragmented NDTP frame;
- неверный CRC;
- reconnect терминала;
- обработка ML timeout;
- недоступный ML;
- malformed ML response;
- частичные результаты;
- stale telemetry;
- causal historical snapshot;
- невозможность второго backend-процесса использовать ту же SQLite БД.

---

# 12. API

Backend:

```text
http://127.0.0.1:8000
```

Swagger:

```text
http://127.0.0.1:8000/docs
```

Основные API:

```text
PUT  /api/v1/devices/{unit_id}
POST /api/v1/telemetry
GET  /api/v1/telemetry
POST /api/v1/schedules
GET  /api/v1/schedules
GET  /api/v1/vehicles
GET  /api/v1/vehicles/{tr_id}
GET  /api/v1/dashboard
GET  /api/v1/data-issues
GET  /api/v1/predictions
POST /api/v1/prediction-cycles
POST /api/v1/ml/check
PUT  /api/v1/replay-clock
GET  /api/v1/events
```

---

# 13. Структура ключевых файлов

```text
services/
├── backend/                 FastAPI, REST/SSE, lifecycle
├── telemetry-gateway/       NDTP TCP + protocol
├── processing-worker/       projection + prediction orchestration
└── ml-service/              feature builders + model router

packages/
├── contracts/               Pydantic contracts
└── storage/                 SQLite durable storage

apps/frontend/               dashboard

tools/
├── csv-replay/              historical replay
├── data-import/             schedule import
└── ml/                      ML diagnostic tools

notebooks/
├── 01_EDA.ipynb
├── 02_feature_engineering.ipynb
├── 03_baselines.ipynb
├── 04_ridge_optuna.ipynb
├── 05_random_forest_optuna.ipynb
├── 06_catboost_optuna.ipynb
├── 07_sequence_rnn.ipynb
└── 08_cold_start_rf.ipynb

artifacts/models/            checkpoints + metrics
submissions/                 generated submissions
docs/                        technical documentation
```

---

# 14. Что не реализовано

В репозитории сохранены каталоги целевой архитектуры, но текущий runtime **не использует**:

- Kafka;
- PostgreSQL/PostGIS;
- Redis;
- Prometheus/Grafana;
- React/MapLibre;
- полноценный map matching по дорожному графу;
- What-if optimizer;
- ONNX/TensorRT;
- промышленный RBAC/HA/TLS.

Эти части нельзя путать с работающим интеграционным контуром.

Текущая версия сознательно использует SQLite WAL и один backend-процесс, зато полностью воспроизводит путь от NDTP до ML-прогноза и интерфейса.
