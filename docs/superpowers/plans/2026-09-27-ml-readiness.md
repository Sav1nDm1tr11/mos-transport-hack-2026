# ML readiness implementation plan

Goal: replace the ML simulator with a real service without changing the transport data path or UI.
Spec: /Users/savin/Documents/Codex/2026-09-27/x20/outputs/ml-readiness-audit.md; user authorized execution in priority order.
Architecture: preserve FastAPI/SQLite local profile and schema-v1 API. Simulator and offline utilities stay explicitly separate from production. Additive endpoints expose integration readiness and prediction freshness. No fabricated production predictions.

- [x] P0 contract: publish ModelInfo and JSON Schema, public check method, metadata/history compatibility tests. Files packages/contracts, ml_client.py, export-contracts.py.
- [x] P0 simulator/preflight: tools/ml/{simulator,preflight,demo}.py, isolated local demo startup; successful/early/zero/insufficient/503/timeout/partial/wrong-version cases. Contract and HTTP tests.
- [x] P0 historical pipeline: tools/ml_offline prediction points, deterministic immutable snapshots, shared historical time, pause/resume via bounded steps/checkpoint, explicit timezone, cur_dev source; CSV output and strict submission/evaluation utilities. Tests prevent future/label leakage and duplicate IDs.
- [x] P1 backend diagnostics: bounded cycle list, model state/check API, explicit no-target/history-limit/contract reasons, non-blocking health. Isolated integration tests.
- [x] P1 capacity: refuse dependent oversized batch clearly; split independent batches deterministically under common time budget; history mismatch rejected before inference. Tests retry and preservation.
- [x] P1 UI: server-side coverage on exact current targets, expiration, target/time/versions/reasons, optional intervals/probability only when supplied. Node and browser tests.
- [x] P1 acceptance: full pytest/JS suites, simulator -> worker -> storage -> API -> browser, existing restart/backpressure boundaries and reproducible reports. No unverified production-scale claim.

Review focus: run/version isolation; replay time versus wall clock; null/zero/negative values; compatibility after retries/restart; oversized/dependent batches.
Each implementation task starts with a regression test, then implementation and focused verification; final full-suite integration and independent review. Changes remain uncommitted with existing user changes preserved.

Validation: local Python suite, Node unit tests, two Playwright scenarios, isolated simulator HTTP smoke, and 3 real validate CSV points. Review findings fixed: UTC normalization, metadata-only compatibility, offline checkpoint/model correlation, UI expiration during outages. Dedicated Compose profile deferred: local demo provides process cleanup and isolated temporary storage. Production load and actual model quality remain outside this verification.
