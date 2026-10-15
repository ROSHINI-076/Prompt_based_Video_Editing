# Architectural Decisions

This file records design decisions made during development.
When a requirement is ambiguous, we pick the simplest option
consistent with the master prompt and document it here.

---

## D001: Postgres as job queue (not Redis)

**Date:** 2026-10-15
**Context:** The master prompt specifies using Postgres as the job queue with `SELECT ... FOR UPDATE SKIP LOCKED`. No Redis.
**Decision:** Use Postgres `jobs` table with advisory locking via `FOR UPDATE SKIP LOCKED` for job claiming.
**Rationale:** Fewer moving parts, single data store, specified by the brief.

## D002: Mock adapters as default

**Date:** 2026-10-15
**Context:** All tests and the default compose config must run without API keys or internet.
**Decision:** Default `STT_PROVIDER`, `LLM_PROVIDER`, and `EMBEDDER_PROVIDER` are all `mock`.
**Rationale:** Required by the brief — `make test` runs with no keys, no internet.

## D003: Integer frames as internal unit

**Date:** 2026-10-15
**Context:** Float timestamps cause drift and make hashing non-deterministic.
**Decision:** All internal positions are integer frames. Seconds are derived as `frames / fps` rounded to 3 decimals for display only.
**Rationale:** Section 6.1 of the master prompt.
