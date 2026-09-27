# ML: модели, качество и production-routing

Состояние на 27.09.2026.

Мы решаем регрессию `target_delay_s` -- отклонение фактического времени прибытия от расписания для первого планового прибытия в окне `(T + 10 мин, T + 15 мин]`.

Основной offline-пайплайн находится в `notebooks/01_...08_`. Для оценки используется MAE в секундах. Основной CV -- purged blocked split по времени внутри реальных `tr_id`; вокруг validation вычищается 45 минут, чтобы история телеметрии и prediction horizon не пересекались. Synthetic наблюдения могут участвовать только в train. `test` используется как отдельный real holdout после выбора модели.

## Текущие результаты

| Модель | Purged OOF MAE | Real test MAE | Роль |
|---|---:|---:|---|
| Ridge | 63.84 | 65.91 | линейный baseline |
| Random Forest | 41.35 | 43.06 | нелинейный tabular baseline |
| CatBoost | **32.84** | **35.23** | основная full-context модель |
| Sequence RNN | 32.44 | -- | экспериментальная последовательная модель |
| Cold RF strict | **43.40** | 47.55 | cold start без `cur_dev_s` |
| Cold RF + `cur_dev_s` | 43.92 | **47.32** | cold start при известном текущем отклонении |

Leaderboard score 1.0 у cold-start submission не заменяет локальный MAE -- для архитектурного выбора ориентируемся на purged OOF и real test.

## Почему две cold-start модели

В online `cur_dev_s` может отсутствовать. Значение `0` в этом случае использовать нельзя -- оно означало бы известное нулевое отклонение. Поэтому сервис различает два режима:

- `COLD_STRICT` -- snapshot GPS + расписание + геометрия, `cur_dev_s` отсутствует;
- `COLD_PLUS_DEV` -- те же признаки + известное `cur_dev_s`.

Когда накопилась достаточная история и `cur_dev_s` известен, сервис может перейти на `FULL_CATBOOST`.

## Production-routing

> Текущий live worker пока не оценивает `cur_dev_s`, поэтому без внешнего источника реальные online-запросы будут идти через `COLD_STRICT`. `COLD_PLUS_DEV` и `FULL_CATBOOST` включатся автоматически после появления корректного `cur_dev_s` в `PredictionTarget`.


```text
PredictionBatch
      |
      v
ml-service
      |
      +-- full CatBoost загружен,
      |   cur_dev_s известен,
      |   истории достаточно --------> FULL_CATBOOST
      |
      +-- cur_dev_s известен ---------> COLD_PLUS_DEV
      |
      +-------------------------------> COLD_STRICT
      |
      v
PredictionResult -> processing-worker -> storage -> backend -> UI
```

По умолчанию full-режим требует не менее 6 пригодных GPS-наблюдений и не менее 15 минут фактического span в 30-минутном окне. Пороги задаются `FULL_MIN_OBSERVATIONS`, `FULL_MIN_SPAN_MINUTES`, `FULL_HISTORY_MINUTES`.

`processing-worker` всегда вызывает один API. Выбор модели происходит внутри `ml-service`; backend и frontend от конкретной модели не зависят.

## Артефакты

```text
artifacts/models/
├── cold_start_rf/
│   └── checkpoints/
│       ├── cold_strict.pkl
│       └── cold_plus_dev.pkl
├── catboost/
│   └── checkpoints/
│       ├── catboost_final.cbm
│       └── catboost_final_metadata.json   # optional
└── sequence_rnn/
    └── checkpoints/                       # experimental
```

RF checkpoint содержит fitted preprocessing и список признаков. CatBoost хранит feature names в `.cbm`; optional metadata задает mode и clipping. Если metadata отсутствует, сервис использует direct prediction без дополнительного clipping.

## Train-serving consistency

ML-сервис строит признаки только из immutable `PredictionBatch`:

- учитывает только телеметрию, доступную на `as_of`;
- `available_time = max(event_time, receive_time)`;
- фактические будущие времена и labels в API не передаются;
- full feature builder восстанавливает online-safe rolling, spatial и schedule признаки;
- признаки, которым нужны отсутствующие online поля (`alt`, `gps_time`, `is_hist_data`, `manual_fill`) или offline-fitted PCA, остаются missing.

Из-за последнего пункта текущий `.cbm` в online режиме является best-effort переносом offline CatBoost. Для финальной production-версии правильный следующий шаг -- переобучить CatBoost на полностью online-reconstructable feature set или сохранить все fitted preprocessing artifacts.

## Ноутбуки

- `01_EDA.ipynb` -- анализ данных;
- `02_feature_engineering.ipynb` -- общий feature engineering;
- `03_baselines.ipynb` -- простые baselines;
- `04_ridge_optuna.ipynb` -- Ridge;
- `05_random_forest_optuna.ipynb` -- RF;
- `06_catboost_optuna.ipynb` -- основная full-context модель;
- `07_sequence_rnn.ipynb` -- GRU/LSTM experiment;
- `08_cold_start_rf.ipynb` -- cold-start RF.

## Что показывается в UI

Каждый `PredictionResult` содержит `model_version`, `feature_version` и `reason_codes`. Интерфейс показывает фактический режим конкретного прогноза:

- `FULL_CATBOOST`;
- `COLD_PLUS_DEV`;
- `COLD_STRICT`.

Так можно видеть не только прогноз, но и уровень доступного контекста, на котором он получен.
