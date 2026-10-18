# IncuBrix – Status Tracker

Updated: 2026-10-18 (Phase 4B Job Queue)

## Must-haves

| # | Must-have | Status | Evidence |
|---|-----------|--------|----------|
| 1 | Never damage the original | Done (storage & hash verification) | `incubrix/adapters/storage/s3.py`; `tests/integration/test_data_layer.py` |
| 2 | Edits stick to content (word IDs) | Done (engine scope) | `incubrix/engine/models.py`; `tests/unit/test_phase3_adapters.py` |
| 3 | Three-way answers | Done (engine scope) | `incubrix/engine/answers.py`; `tests/test_scenarios.py`; Phase 3 pipeline tests |
| 4 | Predictable / deterministic | Done (mock instruction path) | `incubrix/engine/hashing.py`; `tests/unit/test_phase3_adapters.py` (three-run hashes) |
| 5 | Render matches timeline | Not done | Scheduled for Phase 5 (renderer & verifier) |
| 6 | Undo/redo/restore | Done (engine scope) | `incubrix/engine/session.py`; `tests/scenarios/worked_example.yaml`; Phase 3 pipeline test |
| 7 | One stated time policy | Done (engine scope) | `incubrix/engine/answers.py`; `tests/scenarios/worked_example.yaml` |
| 8 | Integration Standard + runners + demo | Partial | Phase 0 scaffold, Phase 4A data layer and Phase 4B queue verified; HTTP API incomplete |

## Integration Standard (10 points)

| # | Requirement | Status | Evidence |
|---|-------------|--------|----------|
| 1 | One-command start/test | Partial | `tests/test_smoke.py::test_health_endpoint`; no named test verifies the complete Compose start/test workflow |
| 2 | OpenAPI 3.1 | Not done | `openapi.yaml` exists, but no passing named test verifies OpenAPI 3.1 or detects app/spec drift |
| 3 | Background jobs | Partial | `tests/unit/test_job_queue.py::test_job_lifecycle_persists_claim_progress_and_result`; `tests/integration/test_api_http.py::test_http_upload_intake_and_eight_step_worked_example` covers intake over HTTP; rendering/progress coverage remains incomplete |
| 4 | Safe retries (Idempotency-Key) | Done | `tests/integration/test_api_http.py::test_instruction_idempotency_replays_response_and_applies_once`; `test_idempotency_rejects_reused_key_with_different_instruction_body`; `test_video_intake_idempotency_returns_same_job_without_repeating_work`; `test_concurrent_idempotent_instructions_have_one_effect` |
| 5 | Clear errors | Done | `tests/integration/test_api_http.py::test_http_unauthorized_error_shape`; `test_http_not_found_error_shape`; `test_http_method_not_allowed_error_shape`; `test_http_oversized_upload_error_shape`; `test_http_rate_limit_error_shape`; `test_http_validation_error_includes_field_details`; `test_http_internal_error_hides_traceback`; `test_concurrent_session_instructions_return_conflict_and_preserve_history`; `test_ready_returns_503_when_postgres_is_unreachable`; `test_ready_returns_503_when_minio_is_unreachable` |
| 6 | Configuration | Partial | `tests/test_smoke.py::test_settings_load` checks selected defaults; no named test verifies every setting and environment contract |
| 7 | Files in/out | Partial | `tests/integration/test_data_layer.py::test_source_upload_and_verify_source`; `tests/integration/test_api_http.py::test_http_upload_intake_and_eight_step_worked_example` covers HTTP upload and source verification; rendered download flow remains untested |
| 8 | Logs & health | Partial | `tests/test_smoke.py::test_health_endpoint`, `test_ready_endpoint_up`, `test_ready_endpoint_down`, `test_correlation_id_echoed`, and `test_correlation_id_generated`; structured JSON log fields lack a named contract test |
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
| Eight-step worked example through natural language | Partial (HTTP flow passes; render step is only a marker and no media is rendered) | `tests/unit/test_phase3_adapters.py`; `tests/integration/test_api_http.py::test_http_upload_intake_and_eight_step_worked_example` |
| API-key non-disclosure and engine dependency boundary | Done | `tests/unit/test_phase3_adapters.py` |
| Postgres parse cache, real provider/model verification and render output | Partial (Postgres parse cache complete) | `incubrix/db/parse_cache.py`; `tests/integration/test_data_layer.py` |

## Phase 4A: Data layer (Postgres, SQLAlchemy 2, Alembic, S3 Storage)

| Item | Status | Evidence |
|------|--------|----------|
| Honest cost defaults (mock/local = 0, env prices for hosted) | Done | `incubrix/config.py`; `incubrix/pipeline/cost.py`; `tests/unit/test_cost.py` |
| Database schema & Alembic migrations (videos, transcripts, sessions, edits, redo_edits, pending_questions, parse_cache) | Done | `incubrix/db/models.py`; `incubrix/db/migrations/`; `tests/integration/test_data_layer.py` |
| Safe concurrent migrations via PostgreSQL advisory lock | Done | `incubrix/db/engine.py`; `tests/integration/test_data_layer.py` |
| Pure engine mapping and exact identifier round-trip | Done | `incubrix/db/mapping.py`; `tests/unit/test_db_mapping.py` |
| Session repository with optimistic concurrency control (`sessions.version`) | Done | `incubrix/db/repository.py`; `tests/unit/test_repository.py`; `tests/integration/test_data_layer.py` |
| Session load rebuilds timeline deterministically (`hash_after == hash_before`) | Done | `tests/unit/test_repository.py`; `tests/integration/test_data_layer.py` |
| Postgres parse cache implementing `ParseCache` protocol | Done | `incubrix/db/parse_cache.py`; `tests/integration/test_data_layer.py` |
| S3-compatible storage adapter (streaming upload/download, idempotent bucket, presigned URLs) | Done | `incubrix/adapters/storage/s3.py`; `tests/unit/test_storage.py` |
| Content-addressed source storage under sha256 (never overwritten) & streaming `verify_source` | Done | `incubrix/adapters/storage/s3.py`; `tests/integration/test_data_layer.py` |
| Integration tests against real Postgres and MinIO containers | Done | `tests/integration/test_data_layer.py` (6 integration tests passed) |
| CI workflow updated with Postgres & MinIO service containers; `scripts/test.ps1` and `Makefile` test target | Done | `.github/workflows/ci.yml`; `Makefile`; `scripts/test.ps1` |

## Phase 4B: Job queue and HTTP API

| Item | Status | Evidence |
|------|--------|----------|
| Postgres jobs table and Alembic migration | Done | `incubrix/db/models.py`; `incubrix/db/migrations/versions/48c62ca2a741_add_jobs_table.py` |
| SKIP LOCKED claims, progress, retries, and stale-lease recovery | Done (queue state tests use SQLite) | `incubrix/jobs/queue.py`; `tests/unit/test_job_queue.py::test_job_lifecycle_persists_claim_progress_and_result`; `tests/unit/test_job_queue.py::test_transient_failure_retries_then_exhausts_attempts`; `tests/unit/test_job_queue.py::test_stale_running_job_is_recovered` |
| Worker loop, heartbeats, signed callback delivery, reserved render handler | Partial (render intentionally returns NOT_IMPLEMENTED) | `incubrix/jobs/worker.py`; `tests/unit/test_job_worker.py::test_worker_runs_handler_and_persists_success`; `tests/unit/test_job_worker.py::test_worker_marks_unimplemented_render_as_failed`; `tests/integration/test_api_http.py::test_http_callback_signature_matches_exact_raw_body`; `test_http_callback_failures_retry_log_and_do_not_fail_job` |
| Intake/transcribe handler: content-addressed storage, SHA-256, ffprobe, VFR/no-audio warnings, STT adapter | Partial (normal A/V HTTP intake is verified; VFR/no-audio warning cases remain untested) | `tests/integration/test_api_http.py::test_http_upload_intake_and_eight_step_worked_example`; `tests/unit/test_job_worker.py::test_video_probe_parses_metadata` |
| `callback_url` with HMAC-SHA256 `X-IncuBrix-Signature`, retries, and callback failure not failing job | Done | `tests/integration/test_api_http.py::test_http_callback_signature_matches_exact_raw_body`; `tests/integration/test_api_http.py::test_http_callback_failures_retry_log_and_do_not_fail_job` |
| Idempotency-Key on every POST (replay + 409 reuse + concurrent identical requests) | Done | `tests/integration/test_api_http.py::test_instruction_idempotency_replays_response_and_applies_once`; `test_idempotency_rejects_reused_key_with_different_instruction_body`; `test_video_intake_idempotency_returns_same_job_without_repeating_work`; `test_concurrent_idempotent_instructions_have_one_effect` |
| Error registry and global handler: standard error envelope for validation, 401, 404, 405, 413, 429, 500 and dependency failures; `SESSION_CONFLICT` (409, retryable) | Done | `tests/integration/test_api_http.py::test_http_unauthorized_error_shape`; `test_http_not_found_error_shape`; `test_http_method_not_allowed_error_shape`; `test_http_oversized_upload_error_shape`; `test_http_rate_limit_error_shape`; `test_http_validation_error_includes_field_details`; `test_http_internal_error_hides_traceback`; `test_concurrent_session_instructions_return_conflict_and_preserve_history`; `test_ready_returns_503_when_postgres_is_unreachable`; `test_ready_returns_503_when_minio_is_unreachable` |
| Bearer auth, rate limit, upload size limit, URL intake (https, `file://` in `TEST_MODE`, redirects, timeout, size limit, SSRF) | Partial | `tests/integration/test_api_http.py::test_video_intake_rejects_unsafe_urls_without_outbound_requests` (private IP, loopback, link-local, non-HTTPS); `test_video_intake_rejects_redirect_to_link_local_before_fetching_redirect`; remaining URL and upload guard cases are not fully covered |
| Routes: `POST /videos`, `GET /videos/{id}`, `GET /videos/{id}/transcript`, `POST /sessions`, `GET /sessions/{id}`, `POST /sessions/{id}/instructions`, `GET /sessions/{id}/timeline`, `GET /sessions/{id}/history`, `POST /sessions/{id}/render`, `GET /jobs/{id}`, `GET /health`, `GET /ready` | Partial | `tests/integration/test_api_http.py::test_http_upload_intake_and_eight_step_worked_example` covers upload, intake, video/transcript/session/instruction/timeline/history routes; complete route matrix remains untested |
| JSON logs with `timestamp`, `level`, `job_id`, and `correlation_id` (generated when missing, echoed in response) | Partial | `incubrix/api/app.py`; `tests/test_smoke.py` exercises correlation IDs; no full integration log contract test |
| Every setting in `.env.example` and config, no hard-coded values | Done | `.env.example`; `incubrix/config.py`; `tests/test_smoke.py::test_settings_load` |

### 4B status summary

- The queue and worker primitives are implemented and unit-tested.
- The API layer and route protection are present but not yet fully proven against every HTTP path and failure mode.
- The remaining Phase 4B items are intentionally deferred beyond this status audit and must be validated with real Postgres + MinIO integration tests before 4C or Phase 5 begins.
