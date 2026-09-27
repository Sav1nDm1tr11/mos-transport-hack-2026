from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import json
import logging
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from processing_worker.ml_client import MLClient
from processing_worker.worker import Worker
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from telemetry_gateway.server import close_server, start_server
from transport_contracts import ScheduleVisit, TelemetryEvent
from transport_storage.store import Backpressure, Conflict, Store

from backend.config import Settings

log = logging.getLogger(__name__)


class BodyLimit:
    """Bound both fixed-length and chunked requests before JSON decoding."""

    def __init__(self, app, maximum):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        size = 0
        messages = []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > self.maximum:
                rid = scope.get("state", {}).get("request_id", uuid.uuid4().hex)
                response = JSONResponse(
                    {"error_code": "body_too_large", "request_id": rid},
                    status_code=413,
                    headers={"X-Request-ID": rid},
                )
                return await response(scope, receive, send)
            messages.append(message)
            if not message.get("more_body", False):
                break
        index = 0

        async def replay():
            nonlocal index
            if index < len(messages):
                result = messages[index]
                index += 1
                return result
            return await receive()

        return await self.app(scope, replay, send)


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tr_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")


class Cycle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    as_of: AwareDatetime | None = None


Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0, le=1000000)]


def create_app(settings: Settings | None = None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        store = Store(settings.db_path)
        process_lock = None
        try:
            if settings.db_path != ":memory:":
                process_lock = open(settings.db_path + ".lock", "a")
                fcntl.flock(process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            ml = MLClient(
                settings.ml_url,
                timeout=settings.ml_timeout,
                history_seconds=settings.history_seconds,
            )
            worker = Worker(store, ml, settings)
            app.state.store = store
            app.state.worker = worker
            app.state.started = datetime.now(UTC)

            async def receive_frame(frame):
                if frame.kind != "realtime":
                    return
                tr_id = await asyncio.to_thread(
                    store.resolve_device, str(frame.unit_id)
                )
                if tr_id is None:
                    await asyncio.to_thread(
                        store.issue,
                        settings.run_id,
                        str(frame.unit_id),
                        "unknown_device",
                    )
                    return
                received = datetime.now(UTC)
                # NPH request counter is intentionally excluded: it resets on reconnect.
                event_id = hashlib.sha256(
                    str(frame.unit_id).encode() + frame.payload
                ).hexdigest()
                navigation = dict(frame.navigation)
                source_flags = navigation.pop("source_flags", None)
                event = TelemetryEvent(
                    run_id=settings.run_id,
                    event_id=event_id,
                    source="ndtp",
                    tr_id=tr_id,
                    unit_id=str(frame.unit_id),
                    receive_time=received,
                    ingested_at=received,
                    source_identity={
                        "request_id": frame.request_id,
                        "navigation_flags": source_flags,
                    },
                    **navigation,
                )
                if event.event_time > received + timedelta(seconds=60):
                    await asyncio.to_thread(
                        store.issue, settings.run_id, str(frame.unit_id), "future_event"
                    )
                    return
                await asyncio.to_thread(
                    store.enqueue, event.model_dump(mode="json"), settings.max_pending
                )

            async def protocol_error(reason, unit_id):
                log.warning("ndtp_rejected: %s", reason)
                await asyncio.to_thread(
                    store.issue,
                    settings.run_id,
                    str(unit_id) if unit_id is not None else None,
                    "ndtp_protocol_error",
                    reason,
                )

            server = await start_server(
                receive_frame,
                on_error=protocol_error,
                host=settings.ndtp_host,
                port=settings.ndtp_port,
                max_connections=settings.max_connections,
            )
            app.state.tcp_server = server
            tasks = [
                asyncio.create_task(worker.process(), name="projection"),
                asyncio.create_task(worker.predict(), name="predictions"),
                asyncio.create_task(worker.schedule(), name="scheduler"),
            ]
            app.state.tasks = tasks
            try:
                yield
            finally:
                await close_server(server)
                worker.stopping.set()
                worker.wakeup.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await ml.close()
        finally:
            if process_lock:
                process_lock.close()
            store.close()

    app = FastAPI(title="Moscow Transport Core", version="0.1.0", lifespan=lifespan)
    app.add_middleware(BodyLimit, maximum=settings.max_http_bytes)

    @app.middleware("http")
    async def request_context(request, call_next):
        request.state.request_id = uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    def error_response(request, code, status):
        return JSONResponse(
            {
                "error_code": code,
                "request_id": getattr(request.state, "request_id", uuid.uuid4().hex),
            },
            status_code=status,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error_response(request, str(exc.detail), exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        response = error_response(request, "validation_error", 422)
        # Raw invalid input can contain secrets or NaNs. Never echo it.
        return response

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return error_response(request, str(exc), 409)

    @app.exception_handler(Backpressure)
    async def backpressure(request, exc):
        return error_response(request, str(exc), 503)

    @app.exception_handler(sqlite3.Error)
    async def storage_error(request, exc):
        log.error("storage_unavailable: %s", type(exc).__name__)
        return error_response(request, "storage_unavailable", 503)

    async def auth(authorization: Annotated[str | None, Header()] = None):
        expected = "Bearer " + settings.api_token
        if not authorization or not hmac.compare_digest(
            authorization.encode(), expected.encode()
        ):
            raise HTTPException(401, "unauthorized")

    router = APIRouter(prefix="/api/v1", dependencies=[Depends(auth)])

    @app.get("/api/v1/health/live")
    async def live():
        return {"status": "live"}

    @app.get("/api/v1/health/ready")
    async def ready(request: Request):
        worker = request.app.state.worker
        if not worker.healthy or any(t.done() for t in request.app.state.tasks):
            raise HTTPException(503, "worker_unavailable")
        await asyncio.to_thread(request.app.state.store.ready)
        return {
            "status": "ready",
            "profile": "local",
            "ml": "configured" if settings.ml_url else "disabled",
            "ndtp_port": request.app.state.tcp_server.sockets[0].getsockname()[1],
        }

    @router.put("/devices/{unit_id}")
    async def bind(unit_id: str, body: Binding, request: Request):
        if (
            not unit_id.isascii()
            or not unit_id.isdigit()
            or not 0 <= int(unit_id) <= 2147483647
        ):
            raise HTTPException(422, "invalid_unit_id")
        await asyncio.to_thread(
            request.app.state.store.bind_device, str(int(unit_id)), body.tr_id
        )
        return {"unit_id": str(int(unit_id)), "tr_id": body.tr_id}

    @router.post("/telemetry", status_code=202)
    async def ingest(event: TelemetryEvent, request: Request):
        store = request.app.state.store
        bound = await asyncio.to_thread(store.resolve_device, event.unit_id)
        if bound != event.tr_id:
            raise HTTPException(422, "unknown_or_mismatched_device")
        if event.event_time > datetime.now(UTC) + timedelta(seconds=60):
            raise HTTPException(422, "future_event")
        # Ingested-at is a server fact, not controlled by callers.
        data = event.model_dump(mode="json")
        data["ingested_at"] = datetime.now(UTC).isoformat()
        accepted = await asyncio.to_thread(store.enqueue, data, settings.max_pending)
        return {
            "accepted": accepted,
            "event_id": event.event_id,
            "run_id": event.run_id,
        }

    @router.post("/schedules", status_code=201)
    async def schedule_import(
        visits: Annotated[list[ScheduleVisit], Field(min_length=1, max_length=10000)],
        request: Request,
    ):
        await asyncio.to_thread(
            request.app.state.store.put_schedule,
            [v.model_dump(mode="json") for v in visits],
        )
        return {"count": len(visits)}

    @router.get("/schedules")
    async def schedules(request: Request, tr_id: str | None = None, limit: Limit = 50):
        return {
            "items": await asyncio.to_thread(
                request.app.state.store.schedule,
                settings.schedule_version,
                tr_id,
                limit,
            )
        }

    @router.get("/vehicles")
    async def vehicles(
        request: Request,
        run_id: str = settings.run_id,
        limit: Limit = 50,
        offset: Offset = 0,
    ):
        return {
            "items": await asyncio.to_thread(
                request.app.state.store.vehicles, run_id, limit, offset
            ),
            "limit": limit,
            "offset": offset,
        }

    @router.get("/vehicles/{tr_id}")
    async def vehicle(tr_id: str, request: Request, run_id: str = settings.run_id):
        store = request.app.state.store
        state = await asyncio.to_thread(store.vehicle, run_id, tr_id)
        if state is None:
            raise HTTPException(404, "vehicle_not_found")
        return {
            "vehicle": state,
            "schedule": await asyncio.to_thread(
                store.schedule, settings.schedule_version, tr_id, 200
            ),
            "predictions": await asyncio.to_thread(
                store.predictions, run_id, tr_id, 10
            ),
        }

    @router.get("/telemetry")
    async def telemetry(
        request: Request,
        run_id: str = settings.run_id,
        tr_id: str | None = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ):
        return {
            "items": await asyncio.to_thread(
                request.app.state.store.telemetry,
                run_id,
                tr_id,
                limit,
                None,
                None,
                offset,
            )
        }

    @router.get("/dashboard")
    async def snapshot(
        request: Request, run_id: str = settings.run_id, limit: Limit = 50
    ):
        data = await asyncio.to_thread(
            request.app.state.store.dashboard,
            run_id,
            limit,
            settings.schedule_version,
            datetime.now(UTC),
        )
        return {
            **data,
            "server_time": datetime.now(UTC),
            "profile": "local",
            "ml": "configured" if settings.ml_url else "disabled",
        }

    @router.get("/prediction-cycles")
    async def cycle_history(
        request: Request, run_id: str = settings.run_id, limit: Limit = 50
    ):
        return {
            "items": await asyncio.to_thread(
                request.app.state.store.cycles, run_id, limit
            )
        }

    @router.post("/ml/check")
    async def check_ml():
        client = MLClient(
            settings.ml_url,
            timeout=settings.ml_timeout,
            history_seconds=settings.history_seconds,
        )
        try:
            info = await client.check()
            return {"ready": True, "model_info": info.model_dump()}
        except Exception as exc:
            return JSONResponse(
                status_code=503, content={"ready": False, "error": type(exc).__name__}
            )
        finally:
            await client.close()

    @router.get("/data-issues")
    async def issues(
        request: Request, run_id: str = settings.run_id, limit: Limit = 50
    ):
        return {
            "items": await asyncio.to_thread(
                request.app.state.store.issues, run_id, limit
            )
        }

    @router.get("/predictions")
    async def predictions(
        request: Request,
        run_id: str = settings.run_id,
        tr_id: str | None = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ):
        return {
            "items": await asyncio.to_thread(
                request.app.state.store.predictions, run_id, tr_id, limit, offset
            )
        }

    @router.post("/prediction-cycles", status_code=202)
    async def cycle(body: Cycle, request: Request):
        if body.as_of and body.as_of > datetime.now(UTC) + timedelta(seconds=5):
            raise HTTPException(422, "future_cycle")
        rid = await request.app.state.worker.prepare(body.as_of)
        return {"request_id": rid, "status": "queued" if rid else "no_target"}

    @router.get("/events")
    async def events(
        request: Request,
        run_id: str = settings.run_id,
        after: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[str | None, Header()] = None,
    ):
        if last_event_id is not None:
            try:
                after = int(last_event_id)
            except ValueError:
                raise HTTPException(422, "invalid_event_cursor") from None
            if after < 0:
                raise HTTPException(422, "invalid_event_cursor")
        store = request.app.state.store
        current = (await asyncio.to_thread(store.snapshot, run_id, 1))["event_cursor"]

        async def stream():
            cursor = after
            if cursor > current:
                yield "event: resync_required\ndata: {}\n\n"
                return
            heartbeat = asyncio.get_running_loop().time()
            while not await request.is_disconnected():
                rows = await asyncio.to_thread(store.changes, run_id, cursor)
                for row in rows:
                    cursor = row["id"]
                    data = {
                        "id": cursor,
                        "run_id": run_id,
                        "type": row["type"],
                        "entity_id": row["entity_id"],
                        "created_at": row["created_at"],
                        "data": row["body"],
                    }
                    yield f"id: {cursor}\nevent: {row['type']}\ndata: {json.dumps(data)}\n\n"
                clock = asyncio.get_running_loop().time()
                if clock - heartbeat >= 15:
                    yield ": heartbeat\n\n"
                    heartbeat = clock
                await asyncio.sleep(0.2)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    app.include_router(router)
    # Local checkout and Docker both serve the same dependency-free UI.
    frontend = Path(__file__).resolve().parents[4] / "apps" / "frontend" / "src"
    if not frontend.is_dir():
        frontend = Path.cwd() / "apps" / "frontend" / "src"
    if frontend.is_dir():
        app.mount("/ui", StaticFiles(directory=frontend), name="dashboard-ui")

        @app.get("/", include_in_schema=False)
        async def dashboard_page():
            return FileResponse(
                frontend / "index.html", headers={"Cache-Control": "no-store"}
            )

    return app
