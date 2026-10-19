# Development progress

## Stage 4C — manifest, OpenAPI, HTTP integration evidence

- **Commit:** `feat: add Stage 4C audit manifests and HTTP contracts`
- **Checks:** `ruff check incubrix/ tests/` passed; `ruff format --check incubrix/ tests/` passed.
- **Cold start:** After `docker compose down`, `.\scripts\test.ps1` rebuilt/started the stack and passed **523 tests in 34.80s**.
- **Make target:** GNU Make 4.4.1 ran `make test`; Ruff and format checks passed and **523 tests passed in 35.24s**.
- **Coverage:** Full suite with engine coverage passed **523 tests**; `incubrix/engine` statement coverage was **100% (2,020/2,020 statements)**.

### Done

- Intake job manifests include provider/model and version labels, safe settings, seed, stage timings,
  cost estimates, warnings, rights, and AI-generation flags. Manifests are stored in MinIO and
  linked from job results; tests verify required fields and no credentials.
- OpenAPI 3.1 YAML is generated from the application; exact parsed-schema drift and all API
  operation/security/request/error contracts have named tests.
- The HTTP route matrix, Postgres job lifecycle (queued/running/progress/succeeded/failed), JSON
  logs, correlation IDs, rate limits, upload limits, URL intake, and configuration scan are
  covered by integration tests against the real Compose Postgres and MinIO.
- Mandatory API POST idempotency keys are enforced and documented in OpenAPI.
- README, status evidence, architectural decisions, and this progress record are updated.

### Partial / Not done

- Rendering was not started (Stage 5). The render job handler remains `NOT_IMPLEMENTED`; therefore
  rendered-media output verification and render-job manifests remain unproven.
- The session/batch runners and 40-scenario proof pack remain Stage 6.
- Consequently, the full Integration Standard is not yet complete: render-dependent portions of
  background jobs and output files remain Partial in `STATUS.md`.

### Next step

Stage 5: implement rendering and its output manifest/verifier. Do not begin it without the user's
“next stage” instruction.
