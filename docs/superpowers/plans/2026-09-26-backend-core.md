# Backend core implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development; owner performs architecture and reviews as explicitly requested.

**Goal:** Run a persistent NDTP → processing → REST service, with a versioned external ML API integration, without UI or model implementation.
**Architecture:** Local integration profile with FastAPI/asyncio, SQLite WAL durable inbox and transactional projections. Separate packages retain gateway/worker/backend boundaries. This is an explicit E1 subset of the target Kafka/PostgreSQL architecture, not a claim of completing all A01–A28.
**Tech Stack:** Python >=3.12, FastAPI, Pydantic 2, HTTPX, Uvicorn, pytest.
**Spec:** ../specs/2026-09-25-transport-delay-system-design.md, dataset NDTP specification (authoritative).

## Global constraints
- Preserve all existing user documents and datasets, work in the current checkout because the scaffold is untracked.
- No frontend or actual model. No invented predictions when ML is disabled/unavailable.
- UTC wire timestamps; explicit timezone for naive CSV import. Model targets in (T+10min,T+15min].
- Single application process owns TCP listener and worker; bounded TCP connections/body sizes, persistent idempotency, chronological state updates.
- Token protection for business APIs; technical health unauthenticated. Local profile has one shared zone, no claim of multiuser RBAC.
- Startup/readiness, malformed input, restart persistence, duplicate and late input, ML timeout/schema/partial results must be tested.

## Review focus
- Fragmented TCP streams/CRC/length abuse must not crash listener or cause unbounded buffers.
- Invalid position must retain last good coordinate; stale events must not roll back state.
- Durable enqueue failure must not be reported as success; processing errors must remain visible.
- ML reply correlation, finite values, missing/duplicate IDs and future input must be checked.
- Concurrent writes and restarting while inbox contains pending input must not lose data.

## Task 1: Protocol and gateway (lightweight subagent)
Files: services/telemetry-gateway/src/telemetry_gateway/{protocol.py,server.py}, services/telemetry-gateway/tests/test_protocol.py.
Interface: decode_frame(frame: bytes) -> Frame; Frame(unit_id:int, request_id:int, kind:str, payload:bytes, navigation:dict|None). Gateway calls async callback(frame), callback handles binding/normalization/durability. start_server(callback,host,port,...)->asyncio.Server. CRC validated regardless of flag, malformed frames isolated, connection identity tied to handshake.
- [x] Write and run failing tests for real binary layout, all stream splits, CRC, coordinates/flags, non-nav tails, mismatch handshake, reconnect.
- [x] Implement bounded stream parser/server, then run tests.
- [x] Owner reviews and runs integration tests.

## Task 2: Contracts and ML client (lightweight subagent)
Files: packages/contracts/src/transport_contracts/{models.py,__init__.py}, services/processing-worker/src/processing_worker/ml_client.py; contract tests.
Interface models: TelemetryEvent, ScheduleVisit, PredictionTarget, PredictionBatch, PredictionResult, BatchResponse; exact fields from spec. MLClient.predict(batch) returns list[PredictionResult], ML disabled yields explicit unavailable errors. Input/output schema snapshots generated for handoff.
- [x] Write failing tests for UTC awareness, finite coordinates/values, target window, result correlation and retry policy.
- [x] Implement strict schemas and bounded async HTTP client; keep per-target errors isolated.
- [x] Test timeout/503 retry, invalid schema, partial/duplicate/extra output, model metadata validation.
- [x] Owner reviews contract integration.

## Task 3: Persistence, state and API (owner)
Files: packages/storage/src/transport_storage/store.py; services/backend/src/backend/{app.py,config.py}; services/processing-worker/src/processing_worker/worker.py; integration tests.
- [x] Write failing API/storage tests, then implement SQLite schema initialization/version, WAL inbox, deduplication and durable state.
- [x] Add authenticated ingestion, device binding, schedule import, vehicles/history/quality/predictions, snapshots and SSE.
- [x] Build immutable prediction batches from as-of telemetry and target schedule, asynchronous ML orchestration, persisted results and degraded status.
- [x] Check restart, bad input, concurrent ingestion, old events, queue failure and absence of model.

## Task 4: Running service and acceptance (owner + bounded helper)
Files: pyproject.toml, scripts/dev, tools/data-import, tools/csv-replay, Dockerfile, infra/compose, docs/development/backend.md, docs/operations/acceptance-2026-09-26.md.
- [x] Install isolated dependencies and lock tested versions; document startup/env variables, API examples and model handoff.
- [x] Run supplied emulator if runtime available; otherwise report exact blocker separately from protocol tests.
- [x] Verify actual sockets and HTTP with sustained ingestion, reconnect/restart, ML stub contract server and unavailable ML.
- [x] Full suite and owner review; fix findings and record measured outcomes and remaining scope.

## Execution and owner review

User explicitly authorized planning + execution and lightweight implementation agents, with architectural decisions and review retained by owner. Work stayed in the current checkout because the user scaffold was untracked; no user documents/datasets were discarded and no automatic commit mixed them into a changeset.

- Gateway/contract/CLI tasks were implemented by lightweight agents; owner reviewed code and completed integration.
- Owner review fixes: strict JSON datetime compatibility; bounded TCP session shutdown; per-target validation; exact persisted ML request body; total timeout/response limits; event-loop wakeup from correct thread; delayed good GPS fix; malformed quality metadata/NaN returns 422 instead of 500.
- 75 tests plus lint/format checks; real emulator local and container load evidence under docs/operations/results.
- Docker Desktop was installed after the user explicitly requested it. The shipped Compose profile remains local SQLite, not the unimplemented Kafka/PostgreSQL target.
- Remaining full-system scope is explicit in docs/development/backend.md. No actual model/UI was implemented or claimed.
