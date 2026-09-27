"""Durable projection worker and independent, bounded prediction scheduler."""

import asyncio
import hashlib
import logging
from datetime import UTC, datetime, timedelta

from processing_worker.ml_client import MLClientError
from transport_contracts import (
    PredictionBatch,
    PredictionResult,
    PredictionTarget,
    ScheduleVisit,
    TelemetryEvent,
)
from transport_storage.store import Backpressure

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, store, ml, settings):
        self.store, self.ml, self.settings = store, ml, settings
        self.stopping = asyncio.Event()
        self.wakeup = asyncio.Event()
        self.healthy = True
        self.last_error = None
        self.cycle_lock = asyncio.Lock()

    def failure(self, error):
        self.healthy = False
        self.last_error = type(error).__name__
        log.exception("worker_failure")

    async def process(self):
        while not self.stopping.is_set():
            try:
                n = await asyncio.to_thread(self.store.process_pending)
                self.healthy = True
                self.last_error = None
                if n:
                    continue
            except Exception as exc:
                self.failure(exc)
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=0.05)
            except TimeoutError:
                pass

    async def prepare(self, as_of=None):
        async with self.cycle_lock:
            if await asyncio.to_thread(self.store.pending_batches):
                raise Backpressure("prediction_cycle_pending")
            request_id = await asyncio.to_thread(
                self._prepare, as_of or datetime.now(UTC)
            )
            self.wakeup.set()
            return request_id

    def _prepare(self, as_of):
        as_of = as_of.astimezone(UTC)
        run = self.settings.run_id
        visits = self.store.targets(self.settings.schedule_version, as_of)
        if not visits:
            return None
        rid = hashlib.sha256(
            f"{run}:{self.settings.schedule_version}:{as_of.isoformat()}".encode()
        ).hexdigest()
        history = self.store.telemetry(
            run,
            limit=self.settings.max_telemetry_per_cycle + 1,
            as_of=as_of,
            since=as_of - timedelta(seconds=self.settings.history_seconds),
        )
        if len(history) > self.settings.max_telemetry_per_cycle:
            raise Backpressure("prediction_history_limit")
        targets = [
            PredictionTarget(
                prediction_id=hashlib.sha256(
                    f"{rid}:{v['stop_visit_id']}".encode()
                ).hexdigest(),
                tr_id=v["tr_id"],
                target_stop_id=v["stop_visit_id"],
                target_time_begin=v["time_begin"],
            )
            for v in visits
        ]
        # Explicit source slice: no label imports, no future facts or mutable DB references.
        batch = PredictionBatch(
            request_id=rid,
            run_id=run,
            as_of=as_of,
            schedule_version=self.settings.schedule_version,
            targets=targets,
            telemetry=[TelemetryEvent.model_validate(e) for e in history],
            schedule_context=[ScheduleVisit.model_validate(v) for v in visits],
        )
        self.store.save_batch(batch.model_dump(mode="json"))
        return rid

    async def predict(self):
        # Sequential execution bounds in-flight ML calls to one; ingestion is independent.
        while not self.stopping.is_set():
            try:
                pending = await asyncio.to_thread(self.store.pending_batches)
                if pending:
                    raw = pending[0]
                    batch = PredictionBatch.model_validate(raw)
                    try:
                        results = await self.ml.predict(batch)
                    except MLClientError:
                        results = [
                            PredictionResult(
                                prediction_id=t.prediction_id,
                                status="error",
                                error_code="ml_contract_error",
                            )
                            for t in batch.targets
                        ]
                    await asyncio.to_thread(
                        self.store.finish_batch,
                        raw,
                        [r.model_dump(mode="json") for r in results],
                    )
                    continue
            except Exception as exc:
                log.exception("prediction_cycle_failure")
                try:
                    await asyncio.to_thread(
                        self.store.issue,
                        self.settings.run_id,
                        None,
                        "prediction_cycle_failure",
                        type(exc).__name__,
                    )
                except Exception:
                    log.exception("prediction_diagnostic_failure")
            self.wakeup.clear()
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=0.1)
            except TimeoutError:
                pass

    async def schedule(self):
        while not self.stopping.is_set():
            try:
                await asyncio.wait_for(
                    self.stopping.wait(), timeout=self.settings.prediction_interval
                )
            except TimeoutError:
                try:
                    await self.prepare()
                except Backpressure:
                    log.warning("prediction_cycle_skipped: backlog or input limit")
                except Exception:
                    log.exception("prediction_schedule_failure")
