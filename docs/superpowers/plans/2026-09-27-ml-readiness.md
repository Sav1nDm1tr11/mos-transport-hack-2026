# ML readiness implementation plan

Goal: replace the ML simulator with a real service without changing the transport data path or UI.
Spec: /Users/savin/Documents/Codex/2026-09-27/x20/outputs/ml-readiness-audit.md; user authorized execution in priority order.
Architecture: preserve FastAPI/SQLite local profile and schema-v1 API. Simulator and offline utilities stay explicitly separate from production. Additive endpoints expose integration readiness and prediction freshness. No fabricated production predictions.

- [ ] P0 contract: publish ModelInfo and JSON Schema, public check method, metadata/history compatibility tests. Files packages/contracts, ml_client.py, export-contracts.py.
- [ ] P0 simulator/preflight: tools/ml/{simulator,preflight,scenario}.py, isolated Compose/demo startup; successful/early/zero/insufficient/503/timeout/partial/wrong-version cases. Contract and HTTP tests.
- [ ] P0 historical pipeline: tools/ml_offline prediction points, deterministic immutable snapshots, shared historical time, pause/resume via bounded steps/checkpoint, explicit timezone, cur_dev source; CSV output and strict submission/evaluation utilities. Tests prevent future/label leakage and duplicate IDs.
- [ ] P1 backend diagnostics: bounded cycle list, model state/check API, explicit no-target/history-limit/contract reasons, non-blocking health. Isolated integration tests.
- [ ] P1 capacity: refuse dependent oversized batch clearly; split independent batches deterministically under common time budget; history mismatch rejected before inference. Tests retry and preservation.
- [ ] P1 UI: server-side coverage on exact current targets, expiration, target/time/versions/reasons, optional intervals/probability only when supplied. Node and browser tests.
- [ ] P1 acceptance: full pytest/JS suites, simulator -> worker -> storage -> API -> browser, restart/load boundaries and reproducible reports. No unverified production-scale claim.

Review focus: run/version isolation; replay time versus wall clock; null/zero/negative values; compatibility after retries/restart; oversized/dependent batches.
Each implementation task starts with a regression test, then implementation and focused verification; final full-suite integration and independent review. Changes remain uncommitted with existing user changes preserved.
