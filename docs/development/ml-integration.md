# Подключение ML

Из корня проекта после установки `pip install -e .`:

```sh
.venv/bin/python tools/ml/simulator.py
.venv/bin/python tools/ml/preflight.py --url http://127.0.0.1:8090 --history-seconds 1800
```

Симулятор явно маркирует версии TEST-ONLY. Режим задаётся ML_SIMULATOR_MODE:
success, early, zero, insufficient, timeout, unavailable503, partial, wrong-version.
Для проверки сохранённого PredictionBatch добавьте в preflight `--batch file.json`;
инференс запускается только с `--predict`. Ненулевой exit code означает провал проверки.
Публичный ModelInfo и JSON Schema находятся в transport_contracts и contracts/ml.

ML обязан реализовать GET /health/ready, GET /v1/model-info, POST /v1/predict-batch.
Backend использует TRANSPORT_ML_URL и TRANSPORT_HISTORY_SECONDS.
Окно истории короче model-info отклоняется. Пакеты больше max_batch_size
разбиваются только при batch_independent=true; контекст сохраняется,
идентификаторы частей стабильны при повторе с теми же версиями и лимитом.
Общий бюджет запроса — 20 секунд; при его исчерпании весь цикл получает ml_timeout.

## Исторический прогон

```sh
.venv/bin/python tools/ml_offline/offline.py replay \
  --points dataset/validate/points.csv --traffic dataset/validate/traffic.csv \
  --schedule dataset/validate/schedule_plan.csv --timezone Europe/Moscow \
  --output .runtime/offline-run --url http://127.0.0.1:8090 --max-steps 10
```

Часовой пояс необходимо подтвердить по описанию исходных данных; команда выше — пример.
Время каждого запроса равно T точки; event_time и receive_time не позже T.
В модель не передаются time_fact и labels. sample_id сохраняется как prediction_id.
cur_dev_s берётся только из точки, источник dataset_point и время T сохраняются.
CSV индексируются во временной SQLite; один срез ограничен 100000 событий.
Снимки и результаты сохраняются атомарно. Повтор команды продолжает после сохранённых
результатов, включая ошибочные; для повторного эксперимента используйте новую output папку.
Изменение входных файлов/окна/часового пояса запрещено в существующей папке.
Не запускайте два процесса в одну output папку.
Скорость по историческому времени: --speed 60; без параметра — максимально быстро.

```sh
.venv/bin/python tools/ml_offline/offline.py submission \
  --template dataset/sample_submission.csv --output .runtime/offline-run \
  --destination .runtime/submission.csv
.venv/bin/python tools/ml_offline/offline.py evaluate \
  --labels dataset/labels/labels_test.csv --label-column target \
  --split test --output .runtime/test-run
```

Имя label-column задайте по заголовку файла меток. MAE допустим только для train/test.
Экспорт требует точное покрытие ID шаблона, сохраняет порядок, разделитель «;».
Пропущенные/ошибочные результаты не заменяются нулём.
