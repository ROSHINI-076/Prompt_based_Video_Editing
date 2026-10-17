# IncuBrix – Status Tracker

Updated: 2026-10-16 (Phase 0 Complete)

## Must-haves

| # | Must-have | Status | Evidence |
|---|-----------|--------|----------|
| 1 | Never damage the original | Not done | Scheduled for Phase 4 (intake SHA-256) |
| 2 | Edits stick to content (word IDs) | Not done | Scheduled for Phase 1 (pure engine core) |
| 3 | Three-way answers | Not done | Scheduled for Phase 2 (resolver / session) |
| 4 | Predictable / deterministic | Not done | Scheduled for Phase 1 & 2 (replay / hash) |
| 5 | Render matches timeline | Not done | Scheduled for Phase 5 (renderer & verifier) |
| 6 | Undo/redo/restore | Not done | Scheduled for Phase 1 (pure engine core) |
| 7 | One stated time policy | Not done | Scheduled for Phase 1 (timeparse / policy) |
| 8 | Integration Standard + runners + demo | Partial | Phase 0 scaffold & tooling verified |

## Integration Standard (10 points)

| # | Requirement | Status | Evidence |
|---|-------------|--------|----------|
| 1 | One-command start/test | Done | `docker-compose.yml` (all 4+1 containers healthy), `Makefile`, `tests/test_smoke.py` |
| 2 | OpenAPI 3.1 | Done | `openapi.yaml` generated from FastAPI app (OpenAPI 3.1.0) |
| 3 | Background jobs | Not done | Scheduled for Phase 4 |
| 4 | Safe retries (Idempotency-Key) | Not done | Scheduled for Phase 4 |
| 5 | Clear errors | Partial | Standard error JSON in global exception handler |
| 6 | Configuration | Done | `incubrix/config.py` (pydantic-settings), `.env.example` |
| 7 | Files in/out | Not done | Scheduled for Phase 4 |
| 8 | Logs & health | Done | `/health` (200), `/ready` (200), structlog with `X-Correlation-Id` |
| 9 | Output manifest | Not done | Scheduled for Phase 4 / 5 |
| 10 | Batch runner | Not done | Scheduled for Phase 6 |

## Levels

### Core
| Item | Status | Evidence |
|------|--------|----------|
| All must-haves | Not done | — |
| 8-step worked example | Not done | — |
| Single-speaker video | Not done | — |
| Multi-speaker video | Not done | — |

### Advanced
| Item | Status | Evidence |
|------|--------|----------|
| Edits that shift each other | Not done | — |
| Ambiguity/references | Not done | — |
| Conflicts | Not done | — |
| Two speakers | Not done | — |

### Outstanding
| Item | Status | Evidence |
|------|--------|----------|
| Imperfect data handling | Not done | — |
| Scale (60-min, <3s) | Not done | — |
| OpenTimelineIO export | Not done | — |
| ≥40 scripted scenarios | Not done | — |

## Phase 2C: Remaining pure engine operations

| Operation | Status | Evidence |
|-----------|--------|----------|
| Filler removal with guarded “like” rule, snap-to-silence, effect skips | Done | `tests/unit/test_phase2c_ops.py` |
| Silence removal with source-anchored protected pauses | Done | `tests/unit/test_phase2c_ops.py` |
| Sentence-based auto-caption in one undoable edit | Done | `tests/unit/test_phase2c_ops.py` |
| Allow-listed music intent, conflict, replacement and restore | Done | `tests/unit/test_phase2c_ops.py` |
| Three-run determinism for each new operation | Done | `tests/unit/test_phase2c_ops.py` |
| Engine statement coverage | Done (100%) | `tests/unit/test_phase2c_ops.py`; full coverage run reports 100% for `incubrix.engine` |
