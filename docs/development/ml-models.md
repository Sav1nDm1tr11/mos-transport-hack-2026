# ML: модели, признаки и pipeline

Состояние на 27.09.2026.

## 1. Постановка

Для каждой prediction point `(tr_id, T)` нужно предсказать:

```text
target_delay_s =
фактическое время прибытия − плановое время прибытия
```

на первой остановке в интервале:

```text
(T + 10 минут, T + 15 минут]
```

Основная метрика — MAE в секундах.

---

# 2. Защита от leakage

Мы не используем обычный random split.

Для реальных ТС строится purged blocked CV:

- точки одного `tr_id` упорядочиваются по времени;
- validation представляет временной блок;
- вокруг него удаляется ±45 минут train;
- 45 минут покрывают максимальные 30 минут feature history + 15 минут prediction horizon;
- synthetic ТС не попадают в validation;
- их вес в train подбирается как гиперпараметр;
- официальный real test не используется в Optuna.

Любые обучаемые preprocessing transformations внутри CV fit'ятся только на train fold.

---

# 3. Feature engineering

Основная offline feature matrix содержит **706 признаков**:

- **454** windowed telemetry features;
- **53** признака контекста соседних ТС/городского snapshot;
- **189** log/signed-log transform features;
- **14** PCA features;
- плюс расписание, GPS, physics, cross и grid features.

## 3.1. Временные окна

```text
1, 3, 5, 10, 15, 30 минут
```

В каждом окне рассчитываются:

- количество пакетов;
- фактический span;
- packets/min;
- GPS valid share;
- historical share;
- speed outlier share;
- mean / median / std / min / max / q10 / q90;
- stopped / slow / fast share;
- first / last / delta / slope скорости;
- среднее абсолютное изменение скорости;
- stop transitions;
- receive lag;
- GPS lag;
- gaps между событиями;
- heading resultant;
- расстояние до target;
- progress;
- path length;
- displacement;
- tortuosity;
- trajectory speed.

## 3.2. Пространственные признаки

Используется Haversine distance.

Строятся:

- расстояние до target;
- bearing;
- heading error;
- cosine alignment;
- projected speed;
- required speed;
- ETA;
- ETA margin;
- расстояние до предыдущей/следующей остановки;
- route proxy;
- schedule path length.

## 3.3. Контекст других ТС

На каждом prediction snapshot берётся последнее доступное состояние других ТС.

Радиусы:

```text
300 / 500 / 1000 / 2000 м
```

Отдельно около:

- текущего положения ТС;
- целевой остановки.

В каждом радиусе:

- vehicle count;
- mean speed;
- median speed;
- slow share.

## 3.4. Текущее отклонение

Если доступно `cur_dev_s`, строятся:

- `cur_dev_abs`;
- `cur_dev_sign`;
- `cur_dev_log`;
- `cur_dev_sqrt`;
- `cur_dev_sq`;
- `cur_dev_per_min`;

и cross-features с:

- horizon;
- required speed;
- distance;
- number of stops;
- route distance;
- recent speed;
- time of day.

---

# 4. Модели

## 4.1. Baselines

Были исследованы:

- zero;
- train mean;
- train median;
- `cur_dev_s`;
- Gaussian Naive Bayes;
- KNN;
- SVR;
- MLP.

Они использовались как ориентиры, а не production-модели.

## 4.2. Ridge

Linear baseline поверх расширенного feature set.

Использовались:

- fold-safe preprocessing;
- Optuna;
- direct/residual objective;
- synthetic sample weight;
- prediction clipping.

Real test MAE:

```text
65.91 с
```

## 4.3. Random Forest

Нелинейный tabular baseline.

Real test MAE:

```text
43.06 с
```

## 4.4. CatBoost

Основная full-context модель.

Optuna:

```text
50 trials direct
50 trials residual
```

Лучший CV в study:

```text
direct:   32.46 с
residual: 35.34 с
```

Real test MAE:

```text
35.23 с
```

Baseline `cur_dev_s` на том же test:

```text
93.36 с
```

Именно CatBoost checkpoint находится в:

```text
artifacts/models/catboost/checkpoints/catboost_final.cbm
```

и подключён к production ML-service.

## 4.5. Sequence RNN

Для последовательностей телеметрии исследовалась отдельная нейросетевая модель.

Финальная конфигурация:

```text
RNN type:       LSTM
layers:         3
hidden size:    96
bidirectional:  true
history steps:  20
step size:      60 секунд
```

Sequence features per step:

```text
16
```

Среди них:

- packet count;
- GPS valid share;
- historical share;
- speed mean/std/last;
- stopped share;
- heading sin/cos;
- receive lag;
- GPS lag;
- relative dx/dy до target;
- distance до target;
- position availability;
- speed outlier share.

Последовательная ветка объединяется с tabular branch.

Purged OOF MAE:

```text
32.44 с
```

Real test MAE из сохранённого `metadata.json`:

```text
41.28 с
```

Модель обучалась с использованием Apple MPS.

Sequence RNN остаётся offline experiment и не включена в текущий production router.

## 4.6. Cold-start Random Forest

Cold-start выделен в отдельный production use case.

Причина: при подключении нового ТС или при частичной деградации данных нельзя считать, что `cur_dev_s = 0`.

`0` означает известное отсутствие отклонения.

`null` означает, что отклонение неизвестно.

Поэтому обучены разные модели.

### COLD_STRICT

Не использует `cur_dev_s`.

Основные признаки:

- положение target;
- положение ТС;
- расписание;
- schedule progress;
- время суток;
- route geometry proxies;
- расстояние;
- heading;
- speed;
- required speed;
- grid location.

Purged OOF:

```text
43.40 с
```

Real test:

```text
47.55 с
```

### COLD_PLUS_DEV

Использует тот же контекст плюс `cur_dev_s` и его производные.

Purged OOF:

```text
43.92 с
```

Real test:

```text
47.32 с
```

Для финальных cold моделей используется до 2500 деревьев.

Checkpoints:

```text
artifacts/models/cold_start_rf/checkpoints/cold_strict.pkl
artifacts/models/cold_start_rf/checkpoints/cold_plus_dev.pkl
```

Checkpoint содержит:

- модель;
- preprocessing;
- точный список features;
- prediction mode;
- clipping;
- model version;
- feature version.

---

# 5. Production router

```text
PredictionBatch
      ↓
  ml-service
      ↓
┌─────────────────────────────────────┐
│ cur_dev_s есть?                     │
└─────────────────────────────────────┘
      │
   нет│                         да
      ▼                          ▼
COLD_STRICT            достаточно истории?
                              │
                       нет    │    да
                        ▼     │     ▼
                 COLD_PLUS_DEV│ FULL_CATBOOST
```

Для `FULL_CATBOOST` по умолчанию требуется:

```text
history window       = 30 минут
minimum history span = 15 минут
minimum observations = 6
```

Все параметры настраиваются переменными окружения.

## 5.1. Текущий live нюанс

Текущий `processing-worker` пока создаёт target без рассчитанного `cur_dev_s`.

Поэтому сейчас обычный end-to-end pipeline:

```text
backend → worker → ML service
```

маршрутизируется в:

```text
COLD_STRICT
```

`COLD_PLUS_DEV` и `FULL_CATBOOST` не являются псевдокодом: они реализованы в сервисе и покрыты тестами, но требуют появления `cur_dev_s` во входном контракте.

---

# 6. Train-serving consistency

Production features строятся только из immutable `PredictionBatch`.

Используются лишь события, доступные на `as_of`.

Для телеметрии:

```text
available_time = max(event_time, receive_time)
```

Future labels и фактические времена будущих остановок в ML API не передаются.

Cold builder восстанавливает online-safe признаки непосредственно из:

- target;
- schedule context;
- telemetry history.

Full builder дополнительно восстанавливает:

- multi-window history;
- neighbor context;
- recent-vs-long-term dynamics;
- distribution transforms.

Некоторые offline признаки невозможно честно восстановить сейчас online:

- `alt`;
- часть GPS metadata;
- `manual_fill`;
- offline-fitted PCA.

Такие признаки не подделываются и остаются missing.

Это означает, что текущий CatBoost online path является best-effort переносом offline модели. Для следующей production-итерации логичный шаг — переобучить full CatBoost на полностью online-reconstructable feature set либо сохранить все необходимые preprocessing artifacts.

---

# 7. ML API

Сервис предоставляет:

```text
GET /health/live
GET /health/ready
GET /v1/model-info
POST /v1/predict-batch
```

Model info сообщает:

```text
schema version      1
history             30 min
max age             300 s
max batch           256
batch independent   true
cur_dev nullable    true
```

Модель не объявляет обязательным:

- neighboring vehicle graph;
- road network resource;
- probability output;
- prediction interval.

## 7.1. Ошибки

Ошибка одного target не падает всем batch.

Для него формируется отдельный result:

```text
status = error
delay_s = null
error_code = ml_inference_error
```

Backend дополнительно умеет различать:

- `ml_disabled`;
- `ml_unavailable`;
- `ml_timeout`;
- `ml_contract_error`;
- `insufficient_data`.

---

# 8. Артефакты

```text
artifacts/models/
├── catboost/
│   ├── checkpoints/
│   │   └── catboost_final.cbm
│   ├── oof_train.csv
│   ├── test_metrics.csv
│   ├── shap_test.csv
│   └── feature_importance_stability.csv
│
├── cold_start_rf/
│   ├── checkpoints/
│   │   ├── cold_strict.pkl
│   │   └── cold_plus_dev.pkl
│   ├── metadata_strong.json
│   ├── test_metrics_strong.csv
│   └── feature_importance_*.csv
│
├── random_forest/
├── ridge/
└── sequence_rnn/
    ├── metadata.json
    ├── final_history.csv
    └── predictions
```

---

# 9. Ноутбуки

`01_EDA.ipynb`

- структура данных;
- target;
- GPS quality;
- synthetic data;
- пространственная структура.

`02_feature_engineering.ipynb`

- полный feature pipeline;
- сохранение feature matrix.

`03_baselines.ipynb`

- простые baselines;
- Naive Bayes.

`04_ridge_optuna.ipynb`

- Ridge;
- leakage-safe CV;
- Optuna.

`05_random_forest_optuna.ipynb`

- Random Forest;
- Optuna.

`06_catboost_optuna.ipynb`

- CatBoost;
- purged CV;
- test evaluation;
- final checkpoint.

`07_sequence_rnn.ipynb`

- sequence LSTM experiment.

`08_cold_start_rf.ipynb`

- cold-start production models;
- checkpoints.
