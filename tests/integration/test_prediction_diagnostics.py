from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from processing_worker.worker import Worker
from transport_storage.store import Store

NOW = datetime(2026, 9, 27, 10, tzinfo=UTC)


async def test_no_target_is_durable_and_run_scoped(tmp_path):
    path = str(tmp_path / "store.db")
    store = Store(path)
    settings = SimpleNamespace(run_id="a", schedule_version="s")
    worker = Worker(store, None, settings)
    assert await worker.prepare(NOW) is None
    assert store.cycles("a")[0]["status"] == "no_target"
    assert store.cycles("other") == []
    store.close()
    store = Store(path)
    assert store.cycles("a")[0]["as_of"] == NOW.isoformat()
    store.close()


def test_current_coverage_excludes_stale_wrong_schedule_and_error():
    store = Store(":memory:")
    visit = dict(
        schedule_version="s",
        stop_visit_id="stop",
        tr_id="bus",
        time_begin=(NOW + timedelta(minutes=12)).isoformat(),
    )
    store.put_schedule([visit])

    def insert(key, as_of, schedule="s", status="ok"):
        batch = dict(
            request_id=key,
            run_id="a",
            as_of=as_of.isoformat(),
            schedule_version=schedule,
            targets=[
                dict(
                    prediction_id=key,
                    tr_id="bus",
                    target_stop_id="stop",
                    target_time_begin=visit["time_begin"].replace("+00:00", "Z"),
                )
            ],
        )
        store.save_batch(batch)
        store.finish_batch(
            batch,
            [
                dict(
                    prediction_id=key,
                    status=status,
                    delay_s=0.0 if status == "ok" else None,
                )
            ],
        )

    insert("old", NOW - timedelta(minutes=3))
    assert store.coverage("a", "s", NOW)["predicted_vehicles"] == 0
    insert("wrong", NOW, "different")
    assert store.coverage("a", "s", NOW)["predicted_vehicles"] == 0
    insert("good", NOW - timedelta(seconds=2))
    assert store.coverage("a", "s", NOW)["predicted_vehicles"] == 1
    insert("error", NOW - timedelta(seconds=1), status="error")
    assert store.coverage("a", "s", NOW)["predicted_vehicles"] == 0
    assert store.coverage("other", "s", NOW)["predicted_vehicles"] == 0
    store.close()
