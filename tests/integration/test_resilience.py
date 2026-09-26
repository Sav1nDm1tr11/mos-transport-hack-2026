import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from backend.config import Settings
from processing_worker.ml_client import MLClientError
from processing_worker.worker import Worker
from test_store import NOW, event
from transport_storage.store import Conflict, Store


def test_concurrent_duplicate_ingest_has_one_effect(tmp_path):
    store = Store(str(tmp_path / "concurrency.db"))
    with ThreadPoolExecutor(max_workers=16) as pool:
        accepted = list(pool.map(lambda _: store.enqueue(event()), range(100)))
    assert sum(accepted) == 1
    assert store.process_pending() == 1
    assert store.vehicle("live", "bus")["entity_version"] == 1
    store.close()


def test_no_future_telemetry_in_snapshot_and_schedule_immutable(tmp_path):
    store = Store(str(tmp_path / "future.db"))
    first = event()
    store.enqueue(first)
    future = event("future")
    future["event_time"] = (NOW + timedelta(seconds=1)).isoformat()
    store.enqueue(future)
    late_received = event("received_later")
    late_received["receive_time"] = (NOW + timedelta(seconds=1)).isoformat()
    store.enqueue(late_received)
    assert [e["event_id"] for e in store.telemetry("live", as_of=NOW)] == ["e1"]
    v = dict(
        schedule_version="v", stop_visit_id="s", tr_id="bus", time_begin=NOW.isoformat()
    )
    store.put_schedule([v])
    with pytest.raises(Conflict):
        store.put_schedule([{**v, "tr_id": "other"}])
    assert store.schedule("v")[0]["tr_id"] == "bus"
    store.close()


async def test_malformed_ml_response_is_terminal_and_does_not_block_next_cycle(
    tmp_path,
):
    class BadML:
        async def predict(self, batch):
            raise MLClientError("schema mismatch", reject_batch=True)

    store = Store(str(tmp_path / "badml.db"))
    settings = Settings(api_token="testing-secret-123", db_path=":memory:")
    store.enqueue(event())
    store.put_schedule(
        [
            dict(
                schedule_version="default",
                stop_visit_id="stop",
                tr_id="bus",
                time_begin=(NOW + timedelta(minutes=12)).isoformat(),
            )
        ]
    )
    worker = Worker(store, BadML(), settings)
    rid = await worker.prepare(NOW)
    task = asyncio.create_task(worker.predict())
    try:
        for _ in range(100):
            if store.predictions("live"):
                break
            await asyncio.sleep(0.01)
        result = store.predictions("live")[0]
        assert result["error_code"] == "ml_contract_error"
        assert not store.pending_batches()
        assert await worker.prepare(NOW + timedelta(seconds=1)) != rid
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        store.close()


def test_failed_transaction_does_not_publish_partial_projection(tmp_path):
    store = Store(str(tmp_path / "atomic.db"))
    store.enqueue(event())
    store.db.execute(
        "CREATE TRIGGER simulate_disk_failure BEFORE INSERT ON changes BEGIN SELECT RAISE(ABORT, 'disk failure'); END;"
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.process_pending()
    assert store.vehicle("live", "bus") is None
    assert store.snapshot("live")["counts"]["pending"] == 1
    store.db.execute("DROP TRIGGER simulate_disk_failure")
    assert store.process_pending() == 1
    assert store.snapshot("live")["event_cursor"] == 1
    store.close()


async def test_scheduler_wakes_prediction_loop_from_event_loop_thread(tmp_path):
    from processing_worker.ml_client import MLClient

    store = Store(str(tmp_path / "wake.db"))
    store.put_schedule(
        [
            dict(
                schedule_version="default",
                stop_visit_id="s",
                tr_id="bus",
                time_begin=(NOW + timedelta(minutes=12)).isoformat(),
            )
        ]
    )
    worker = Worker(store, MLClient(None), Settings(api_token="testing-secret-123"))
    loop = asyncio.get_running_loop()
    old_debug = loop.get_debug()
    loop.set_debug(True)
    task = asyncio.create_task(worker.predict())
    try:
        await asyncio.sleep(0.01)
        await worker.prepare(NOW)
        for _ in range(100):
            if store.predictions("live"):
                break
            await asyncio.sleep(0.01)
        assert store.predictions("live")[0]["error_code"] == "ml_disabled"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        loop.set_debug(old_debug)
        store.close()
