# IncuBrix – Status Tracker

Updated: 2026-10-18 (Phase 3 Adapters and Instruction Pipeline)

## Must-haves

| # | Must-have | Status | Evidence |
|---|-----------|--------|----------|
| 1 | Never damage the original | Not done | Scheduled for Phase 4 (intake SHA-256) |
| 2 | Edits stick to content (word IDs) | Done (engine scope) | `incubrix/engine/models.py`; `tests/unit/test_phase3_adapters.py` |
| 3 | Three-way answers | Done (engine scope) | `incubrix/engine/answers.py`; `tests/test_scenarios.py`; Phase 3 pipeline tests |
| 4 | Predictable / deterministic | Done (mock instruction path) | `incubrix/engine/hashing.py`; `tests/unit/test_phase3_adapters.py` (three-run hashes) |
| 5 | Render matches timeline | Not done | Scheduled for Phase 5 (renderer & verifier) |
| 6 | Undo/redo/restore | Done (engine scope) | `incubrix/engine/session.py`; `tests/scenarios/worked_example.yaml`; Phase 3 pipeline test |
| 7 | One stated time policy | Done (engine scope) | `incubrix/engine/answers.py`; `tests/scenarios/worked_example.yaml` |
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
| 8-step worked example (engine-level) | Done | `tests/scenarios/worked_example.yaml`; render step is a timeline-output marker |
| Single-speaker video | Not done | — |
| Multi-speaker video | Not done | — |

### Advanced
| Item | Status | Evidence |
|------|--------|----------|
| Edits that shift each other | Not done | — |
| Ambiguity/references | Not done | — |
| Conflicts | Not done | — |
| Two speakers (engine-level) | Done | `tests/scenarios/guest_topic_host_leadin.yaml`; `tests/scenarios/guest_topic_remove_host_leadin.yaml` |

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

## Phase 2D: Worked example and engine hard cases

| Item | Status | Evidence |
|------|--------|----------|
| YAML scenario harness; all scenarios run three times with per-step hash comparison | Done | `tests/test_scenarios.py`; `tests/scenarios/README.md` |
| Eight-step worked example with word-anchored caption and reversible intro restore | Done (engine-level) | `tests/scenarios/worked_example.yaml`; rendering itself remains deferred |
| Caption dormancy and revival; reorder with mute/bleep and filler protection | Done | `tests/scenarios/dormant_caption_restore.yaml`; `tests/scenarios/reorder_mute_bleep_filler.yaml` |
| Current/original repeat confirmation, range parsing, out-of-range and destructive-cut rejection | Done | `tests/scenarios/repeat_current_time_cut.yaml`; `tests/scenarios/repeat_original_time_cut.yaml`; `tests/scenarios/time_formats.yaml`; `tests/scenarios/caption_out_of_range.yaml`; `tests/scenarios/speed_invalid_cut_all_partial.yaml` |
| Restore by active cut ordinal while retaining music | Done | `tests/scenarios/restore_second_to_last_cut.yaml`; `tests/scenarios/restore_second_to_last_cut_insufficient.yaml` |
| Guest-topic filtering and explicit host lead-in choice | Done | `tests/scenarios/guest_topic_host_leadin.yaml`; `tests/scenarios/guest_topic_remove_host_leadin.yaml`; `tests/scenarios/guest_topic_without_leadin.yaml` |
| Speed overlap, protected pause, one-instruction undo, redo clearing | Done | `tests/scenarios/speed_overlap_and_deliberate_pause.yaml`; `tests/scenarios/one_instruction_one_undo.yaml`; `tests/scenarios/new_edit_clears_redo.yaml` |
| Scenario coverage and engine coverage | Done | 18 YAML scenarios; `python -m pytest tests/ --cov=incubrix.engine --cov-report=term-missing -q` |

## Phase 3: Adapters and natural-language instruction pipeline

| Item | Status | Evidence |
|------|--------|----------|
| STT interface, deterministic mock JSON adapter, stable word IDs and confidence flags | Done | `incubrix/adapters/stt/`; `tests/unit/test_phase3_adapters.py` |
| faster-whisper adapter with lazy import and optional pinned extra | Partial (provider not downloaded or exercised against a real model) | `incubrix/adapters/stt/faster_whisper.py`; mocked contract test |
| Deterministic mock embedder and lazy local sentence-transformers adapter | Partial (real model not downloaded or exercised) | `incubrix/adapters/embed/`; `tests/unit/test_phase3_adapters.py` |
| Mock parser for required instruction forms, strict pydantic validation and exported schema | Done | `incubrix/adapters/llm/mock.py`; `incubrix/pipeline/intents.py`; `schemas/intent_ops.schema.json`; adapter tests |
| OpenAI-compatible/Groq client, JSON repair, bounded transient retries and usage | Done (HTTP contract mocked; live provider not tested) | `incubrix/adapters/llm/openai_compatible.py`; `tests/unit/test_phase3_adapters.py` |
| Compact-context parse cache and pipeline; pending clarification bypasses parser | Done (memory cache only) | `incubrix/adapters/llm/cache.py`; `incubrix/pipeline/instructions.py`; worked-example test |
| Eight-step worked example through natural language | Partial (render step recognized, no media is rendered) | `tests/unit/test_phase3_adapters.py`; warning is asserted |
| API-key non-disclosure and engine dependency boundary | Done | `tests/unit/test_phase3_adapters.py` |
| Postgres parse cache, real provider/model verification and render output | Not done | Deferred to Phases 4–5 |
