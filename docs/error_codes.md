# Engine error codes

| Code | Meaning |
|---|---|
| `BAD_RANGE` | A time range or reorder destination has invalid bounds or shape. |
| `CUT_ALL` | The operation would remove every frame from the video. |
| `EDIT_CONFLICT` | The target conflicts with content removed by an earlier edit, or a second music track conflicts with the active music edit. |
| `INVALID_ARGUMENT` | An operation has a missing, malformed, or incorrectly typed argument. |
| `INSTRUCTION_NOT_UNDERSTOOD` | The configured instruction parser could not produce a valid supported intent. |
| `INVALID_SPEED` | The speed factor is not a finite number in the supported range. |
| `NO_CHANGE` | The operation is redundant, was already applied (including repeated filler/silence removal or auto-captioning), or was declined without changing the timeline. |
| `NO_MATCH` | No matching content, filler word, unprotected silence gap, edit, caption, or pending question was found. |
| `NO_TARGET` | The requested target does not exist, such as styling when there is no caption, selecting a music track outside the configured allow-list, or requesting a cut ordinal beyond the active cut history. |
| `NOTHING_TO_REDO` | There is no edit available to redo. |
| `NOTHING_TO_UNDO` | There is no edit available to undo. |
| `OUT_OF_RANGE` | A requested start or destination is beyond the video. |
| `UNSUPPORTED_OPERATION` | The instruction names an operation the engine does not support. |

# API error codes

| Code | Meaning |
|---|---|
| `VALIDATION_ERROR` | Request body or params failed validation. |
| `NOT_FOUND` | A requested resource, video, transcript, job or session does not exist. |
| `UNAUTHORIZED` | A valid bearer token was not supplied. |
| `PAYLOAD_TOO_LARGE` | The upload or request body exceeded the configured limit. |
| `RATE_LIMITED` | The request was rejected by the per-token rate limiter. |
| `IDEMPOTENCY_KEY_REUSED` | The same Idempotency-Key was used with a different request body. |
| `SESSION_CONFLICT` | A session update conflicted with a stale optimistic version. |
| `DATABASE_UNAVAILABLE` | Postgres or another required DB dependency was unavailable. |
| `STORAGE_UNAVAILABLE` | MinIO/S3 or source storage was unavailable. |
| `LLM_UNAVAILABLE` | The configured LLM adapter was unavailable or failed to respond. |
| `INTERNAL_ERROR` | Unhandled server-side failure or dependency error before a specific code was available. |
| `NOT_IMPLEMENTED` | A job or route is intentionally deferred for a later phase. |
| `INVALID_URL` | URL intake was not allowed or could not be resolved safely. |
| `INVALID_MEDIA` | The provided media file was missing or malformed. |
| `VIDEO_NOT_FOUND` | A referenced file or media item was missing. |
