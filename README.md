# IncuBrix Prompt-Based Video Editing Engine

An offline-first FastAPI service for turning plain-language video editing requests into
deterministic, reversible timeline edits. The engine anchors captions and effects to transcript
word IDs; it does not store overlays at output timestamps. The source is kept unchanged, and
intake produces a SHA-256 and an auditable job manifest.

## Architecture

```mermaid
flowchart LR
  Client["HTTP client / test runner"] --> API["FastAPI API"]
  API --> DB[("Postgres: sessions, edits, jobs")]
  API --> S3[("MinIO: immutable sources, manifests")]
  API --> Parser["LLM adapter: intent parsing only"]
  Worker["Worker"] --> DB
  Worker --> S3
  Worker --> STT["STT adapter"]
  API --> Engine["Pure timeline engine"]
```

Postgres is also the job queue (`FOR UPDATE SKIP LOCKED`); there is no Redis. Mock STT, LLM and
embedder providers are the default and require no API keys or network access. Render execution is
reserved but not implemented yet (Stage 5); a render job currently ends with
`NOT_IMPLEMENTED`. Session and batch runners are **coming in Stage 6**.

## Setup

Windows PowerShell (four commands, then edit `.env` to set a private bearer token):

```powershell
git clone <repo-url> E:\incubrix
cd E:\incubrix
Copy-Item .env.example .env
.\scripts\test.ps1
```

The test script builds and starts Postgres, MinIO, API, worker, and the bucket initializer, waits for
`/ready`, then runs Ruff and pytest. To run the API without tests, use `docker compose up -d --build`
after configuring `.env`; `/health` is unauthenticated and `/api/v1/*` requires `Authorization:
Bearer <API_TOKEN>`.

On platforms with GNU Make installed, `make test` runs the same test script. On Windows, use
`.\scripts\test.ps1`; GNU Make is not included with Windows PowerShell.

## Test and contract commands

```powershell
.\scripts\test.ps1
python -m pytest tests/ -q
python scripts\test_suite.py --coverage-engine
python scripts\generate_openapi.py
```

The suite requires Docker Compose for real Postgres/MinIO integration and the HTTP end-to-end test.
It uses mock providers and needs no API keys. The engine coverage command reports coverage across
the full suite; the Stage 4C run measured **100% statement coverage (2,020/2,020 statements)**.
The YAML OpenAPI file is regenerated from the live FastAPI application; a test fails on drift.

Session runner and batch runner commands are **coming in Stage 6**.

## API routes

All application routes require bearer-token authentication:

| Method | Route | Purpose |
|---|---|---|
| POST | `/api/v1/videos` | Accept multipart upload or URL; return an intake job (`202`) |
| GET | `/api/v1/videos/{video_id}` | Video metadata and source hash |
| GET | `/api/v1/videos/{video_id}/transcript` | Transcript |
| POST | `/api/v1/sessions` | Create an editing session |
| GET | `/api/v1/sessions/{session_id}` | Session metadata |
| POST | `/api/v1/sessions/{session_id}/instructions` | Submit `say` or clarification `answer` |
| GET | `/api/v1/sessions/{session_id}/timeline` | Current derived timeline and hash |
| GET | `/api/v1/sessions/{session_id}/history` | Edit and redo stacks |
| POST | `/api/v1/sessions/{session_id}/render` | Enqueue reserved render job; handler is Stage 5 |
| GET | `/api/v1/jobs/{job_id}` | Job state, progress, result, and manifest link |
| GET | `/health`, `/ready` | Unauthenticated process and dependency checks |

`Idempotency-Key` is required for POST requests. Reusing a key with a different request returns
`IDEMPOTENCY_KEY_REUSED` (409). Responses include an `X-Correlation-Id`. Intake manifests are stored
in MinIO and linked from completed job results; actual rendered media output is not yet available.

## Time policy and current limitations

Time-based instructions default to **current (edited) time**. The resolver converts that time
through the current timeline to source frames and the answer reports the policy/conversion. Use an
explicit “original time” phrase to target the source-time coordinate system. Source-anchored
operations are replayed from history; captions/effects remain attached to word IDs.

Rendering, render verification, OpenTimelineIO export, session/batch runners, and the
40-scenario proof pack are not implemented yet. See [STATUS.md](./STATUS.md) for scoped evidence and
remaining work.

## Configuration

Every `Settings` field is listed below and in `.env.example`. Secrets must be supplied through the
environment or ignored `.env`; never commit or log them. Compose requires `S3_ACCESS_KEY` and
`S3_SECRET_KEY`. `AUTH_DISABLED` is not accepted in `docker-compose.yml` and must not be enabled
outside the explicit test-only app bypass.

| Setting | Default | Meaning |
|---|---:|---|
| `HOST` | `0.0.0.0` | API bind address |
| `PORT` | `8000` | API port |
| `DEBUG` | `false` | Debug mode |
| `API_TOKEN` | empty; required | Bearer token for the API |
| `AUTH_DISABLED` | `false` | Test-only auth bypass when `TEST_MODE=true` |
| `RATE_LIMIT_PER_MINUTE` | `60` | Per-client request limit |
| `MAX_UPLOAD_BYTES` | `524288000` | Multipart body byte limit |
| `DATABASE_URL` | `postgresql+psycopg2://incubrix:incubrix@postgres:5432/incubrix` | Sync Postgres URL |
| `DATABASE_URL_ASYNC` | same as `DATABASE_URL` | Async Postgres URL |
| `DB_POOL_SIZE` | `5` | SQLAlchemy pool size |
| `DB_MAX_OVERFLOW` | `10` | SQLAlchemy pool overflow |
| `S3_ENDPOINT` | `http://minio:9000` | S3-compatible endpoint |
| `S3_ACCESS_KEY` | empty | MinIO access key |
| `S3_SECRET_KEY` | empty | MinIO secret key |
| `S3_BUCKET` | `incubrix` | Storage bucket |
| `S3_REGION` | `us-east-1` | S3 signing region |
| `S3_PRESIGNED_EXPIRY` | `3600` | Presigned-link lifetime, seconds |
| `STT_PROVIDER` | `mock` | `mock` or `faster_whisper` |
| `STT_MODEL` | empty | Configured speech model |
| `STT_DEVICE` | `cpu` | Speech model device |
| `LLM_PROVIDER` | `mock` | `mock`, `groq`, or `openai` |
| `LLM_MODEL` | empty | Selected instruction parser model |
| `LLM_BASE_URL` | empty | OpenAI-compatible endpoint |
| `LLM_API_KEY` | empty | OpenAI-compatible API key |
| `GROQ_API_KEY` | empty | Groq API key |
| `OPENAI_API_KEY` | empty | OpenAI API key |
| `LLM_TIMEOUT_S` | `30` | LLM request timeout |
| `LLM_MAX_RETRIES` | `2` | Bounded LLM retries |
| `EMBEDDER_PROVIDER` | `mock` | `mock` or `local` |
| `EMBEDDER_MODEL` | empty | Configured embedding model |
| `EMBEDDER_DIMENSIONS` | `128` | Mock vector dimensions |
| `MIN_SPEED` | `0.25` | Minimum accepted speed factor |
| `MAX_SPEED` | `4.0` | Maximum accepted speed factor |
| `MIN_CAPTION_S` | `0.7` | Minimum computed caption duration |
| `LOW_CONFIDENCE_THRESHOLD` | `0.6` | Transcript confidence warning threshold |
| `MAX_UPLOAD_SIZE_MB` | `500` | Upload size setting in MB |
| `URL_FETCH_TIMEOUT_S` | `120` | URL intake timeout |
| `URL_FETCH_MAX_SIZE_MB` | `500` | URL intake size limit in MB |
| `MAX_DOWNLOAD_BYTES` | `524288000` | URL intake byte limit |
| `URL_MAX_REDIRECTS` | `3` | Allowed URL redirects |
| `WORKER_POLL_INTERVAL_S` | `1.0` | Job poll interval |
| `WORKER_CONCURRENCY` | `2` | Worker threads |
| `HMAC_SECRET` | empty | Internal HMAC secret |
| `CALLBACK_HMAC_SECRET` | empty | Callback signature secret |
| `CALLBACK_MAX_RETRIES` | `3` | Callback retry count |
| `CALLBACK_BACKOFF_S` | `2.0` | Callback exponential backoff base |
| `JOB_MAX_ATTEMPTS` | `3` | Job attempts before terminal failure |
| `JOB_HEARTBEAT_STALE_S` | `120` | Worker lease timeout |
| `IDEMPOTENCY_TTL_S` | `86400` | Idempotency response lifetime |
| `STT_MOCK_TRANSCRIPT_PATH` | empty | Optional prepared transcript JSON for mock STT |
| `LOG_LEVEL` | `INFO` | Log threshold |
| `LOG_FORMAT` | `json` | `json` or `console` |
| `STT_COST_PER_MINUTE` | `0.0` | Configured STT cost |
| `LLM_COST_PER_1K_TOKENS` | `0.0` | Legacy flat LLM token price |
| `LLM_INPUT_COST_PER_1K_TOKENS` | `0.0` | LLM input token price |
| `LLM_OUTPUT_COST_PER_1K_TOKENS` | `0.0` | LLM output token price |
| `TEST_MODE` | `false` | Enables file URL fixtures for tests |

## Error codes

HTTP errors use `{"error":{"code","message","retryable","details"}}`. Instruction-level rejections
are valid responses with HTTP 200 and an engine `error` object.

| Code | HTTP/status | Meaning |
|---|---:|---|
| `VALIDATION_ERROR` | 422 | Invalid request shape or field |
| `UNAUTHORIZED` | 401 | Missing or invalid bearer token |
| `NOT_FOUND` | 404 | Requested resource does not exist |
| `VIDEO_NOT_FOUND` | 404 | Video lookup failed |
| `METHOD_NOT_ALLOWED` | 405 | Unsupported HTTP method |
| `PAYLOAD_TOO_LARGE` | 413 | Upload exceeds configured limit |
| `RATE_LIMITED` | 429 | Client exceeded request limit |
| `IDEMPOTENCY_KEY_REUSED` | 409 | Same key used for a different body |
| `SESSION_CONFLICT` | 409 | Concurrent session edit; retryable |
| `DATABASE_UNAVAILABLE` | 503 | Database unavailable; retryable |
| `STORAGE_UNAVAILABLE` | 503 | Object storage unavailable; retryable |
| `LLM_UNAVAILABLE` | 503 | Configured parser unavailable; retryable |
| `INTERNAL_ERROR` | 500 | Unexpected server/job failure; retryable |
| `NOT_IMPLEMENTED` | 501 | Reserved feature not implemented (render) |
| `INVALID_URL` | 400 | URL fails scheme, network, or fetch validation |
| `INVALID_MEDIA` | 400 | Invalid or unsupported source media |
| `NO_TARGET` | 400 / engine rejection | No matching target for the requested operation |
| `OUT_OF_RANGE` | 400 / engine rejection | Requested source/current time is out of range |
| `INVALID_SPEED` | 400 / engine rejection | Speed is not within configured limits |
| `INSTRUCTION_NOT_UNDERSTOOD` | engine rejection | Parser could not map the request to valid intent |
| `CUT_ALL` | engine rejection | Operation would remove the whole video |
| `BAD_RANGE` | engine rejection | Malformed or invalid time range |
| `INVALID_ARGUMENT` | engine rejection | Invalid structured operation argument |
| `NO_MATCH` | engine rejection | No phrase/topic/caption/edit matched |
| `NO_CHANGE` | engine rejection | Repeated request would make no change |
| `EDIT_CONFLICT` | engine rejection | Operation conflicts with an earlier edit |
| `NOTHING_TO_UNDO` | engine rejection | No edit is available to undo |
| `NOTHING_TO_REDO` | engine rejection | No edit is available to redo |
| `NO_PENDING_QUESTION` | engine rejection | There is no clarification waiting for an answer |

## Troubleshooting (Windows)

- If Docker Buildx reports `additional privileges requested`, run
  `$env:BUILDX_BAKE_ENTITLEMENTS_FS = "0"` before Compose. `scripts/test.ps1` sets it for its
  process automatically.
- Check `docker compose ps` and `docker compose logs api worker postgres minio` if `/ready` does not
  become healthy. The test runner waits up to 120 seconds; the first run also builds the image.
- Keep host-side Postgres/MinIO URLs pointed at `localhost`; Compose containers use `postgres` and
  `minio` service DNS names. `scripts/test_suite.py` configures these separately.
- Local Windows may block host `ffmpeg.exe`; generated test media falls back to the image's ffmpeg.
- If GNU Make is unavailable, use `.\scripts\test.ps1` (the Windows equivalent of `make test`).

## Demo security and cold start

Set a random `API_TOKEN` in `.env` and keep it private. The API token is required on every
`/api/v1/*` route; `/health` and `/ready` are intentionally unauthenticated. The configurable
`RATE_LIMIT_PER_MINUTE` guards the demo. Never enable auth bypass in Compose. Cold start includes
image build and dependency health checks; mock providers avoid downloading models, and the test
runner allows up to 120 seconds for `/ready`.
