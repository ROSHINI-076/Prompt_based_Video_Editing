# PART A: MASTER PROMPT (workspace rules)

## 1. Role and goal

You are a senior backend and media engineer. Build the **IncuBrix Prompt-Based Video Editing Engine** end to end. A creator uploads a talking-head video and edits it by typing plain-language instructions ("cut the first 30 seconds", "remove the part about pricing", "add a caption on the intro"). The system turns each instruction into **exact, reversible, deterministic edits** on a timeline and renders the result with ffmpeg. The system is evaluated on correctness across a *conversation* of edits (references, undo, restore, changing minds, vague requests, shifted timing).

This is a hiring assessment. Marks come from: (1) Must-haves (all pass/fail, all must pass), (2) Levels Core/Advanced/Outstanding, (3) a 10-point Integration Standard, (4) 5 hidden tests on a live demo, (5) a live session where the candidate explains and changes the code. **Therefore: code must be simple, explainable, well tested, and honest about limitations.** Never claim something works unless a test or evidence file proves it.

## 2. The single most important rule

**Edits belong to content, not to a clock position.**
A caption added on the words "welcome to the channel" must stay on exactly those words after anything before it is cut, sped up, reordered or restored. Never store an overlay/effect as an output timestamp. Store **word IDs**. Output times are *computed* from the current timeline every time.

## 3. Non-negotiable Must-haves (design everything to satisfy these)

| # | Must-have | How this project guarantees it |
|---|---|---|
| 1 | Never damage the original | Source file stored once, SHA-256 recorded at intake, never written to again. Edits are a history applied on top; rendering is a separate job that writes new files. Provide a verification command that re-hashes the source after a session. |
| 2 | Edits stick to content | Overlays, mutes, bleeps anchor to word IDs. Timeline is derived by replay. |
| 3 | Three-way answers | Every instruction returns exactly one of `applied`, `needs_clarification`, `rejected`, in the formats in section 9. |
| 4 | Predictable / deterministic | Same session gives same `timeline_hash` on 3 runs. The LLM only translates words into structured operations (temperature 0, JSON schema, cached). ALL timeline logic is ordinary deterministic code. |
| 5 | Render matches timeline | All boundaries quantized to the frame grid; rendered length == timeline length within 1 frame; A/V offset within 1 frame; automated verifier. |
| 6 | All operations work incl. undo/redo/restore | One instruction = one undo; a new edit clears redo; restore a specific earlier edit. |
| 7 | One stated time policy | Default policy = **current (edited) time**. Explicit "original time" override supported. Every answer that interprets a time states the policy and the conversion. |
| 8 | Integration Standard + session runner + live demo | See section 11 and 12. |

## 4. Technology decisions (already made, do not re-debate)

- **Language/runtime:** Python 3.11+, FastAPI, pydantic v2 (+ pydantic-settings), SQLAlchemy 2 + Alembic (or plain SQL migrations), uvicorn.
- **Containers (keep it to 4 + 1 init):** `api`, `worker` (same image as api, different command), `postgres`, `minio`, plus one-shot `minio-init` (creates bucket). **No Redis.** Use **Postgres as the job queue** (`jobs` table, workers claim with `SELECT ... FOR UPDATE SKIP LOCKED`).
- **Media:** ffmpeg + ffprobe inside the image.
- **Speech-to-text:** adapter interface `STTAdapter`. Implementations: `mock` (reads a prepared transcript JSON, used in all tests and default in compose), `faster_whisper` (local, MIT, word timestamps; model name from env). Optional diarization adapter (mock + a simple heuristic fallback; any real diarization model must have its commercial licence verified and recorded in SOURCES.md).
- **LLM (instruction parsing only):** adapter interface `LLMAdapter`. Implementations: `mock` (rule-based/regex parser; used in all tests; no API key), `groq` (OpenAI-compatible API, key from `GROQ_API_KEY`, model from `LLM_MODEL`, base URL from `LLM_BASE_URL`), and optionally `openai` using the same OpenAI-compatible client. Selected by `LLM_PROVIDER`. Temperature 0, JSON schema / JSON mode, validate with pydantic, retry + repair once, then fall back to a clear `rejected` (code `INSTRUCTION_NOT_UNDERSTOOD`) rather than guessing.
- **Semantic search:** local `sentence-transformers` small model (Apache-2.0, e.g. a MiniLM/bge-small class model, name from env) behind an `Embedder` adapter, with a keyword + synonym fallback and a `mock` embedder for tests. Matches are shown to the user before cutting.
- **Tests:** pytest. One command `make test` runs unit + integration + at least one end-to-end, with **no API keys** and no internet.
- **Interop:** OpenTimelineIO export (Outstanding level).

## 5. Architecture

```
Session Runner CLI / Batch Runner CLI
          │ HTTP /api/v1
          ▼
   FastAPI service (api) ── Postgres (sessions, edit history, transcripts, jobs, idempotency keys)
          │                  MinIO (source videos, renders, manifests)  ◄── worker (render, STT, embeddings jobs)
          ▼
 Instruction pipeline (inside api, ordinary code except step 1):
  1. Parser adapter (LLM or mock)  → structured "intent ops"  [ONLY AI STEP]
  2. Validator (pydantic)
  3. Resolver (words / time / semantic / references → word IDs and source frames)
  4. Decision (applied | needs_clarification | rejected)
  5. Timeline engine (pure functions, replay) → new timeline + timeline_hash
  Rendering is a SEPARATE job that consumes a timeline.
```

Repo layout:

```
incubrix/
  api/            routes, errors, middleware (correlation id, idempotency), deps
  adapters/       stt/{mock,faster_whisper}.py  llm/{mock,groq,openai}.py  embed/{mock,local}.py  storage/s3.py
  engine/         models.py timeline.py replay.py session.py resolver.py timeparse.py ops.py hashing.py
  render/         graph.py (ffmpeg filter graph) captions_ass.py verify.py
  jobs/           queue.py worker.py callbacks.py
  runner/         session_runner.py batch_runner.py
  config.py  logging.py  manifest.py  otio_export.py
schemas/          timeline.schema.json  transcript.schema.json  session.schema.json
tests/            unit/  integration/  e2e/  scenarios/*.yaml  fixtures/
sessions/         >=15 scripted sessions (+ results/)
docs/             timeline_explained.md (1 page) architecture.png/mmd
openapi.yaml README.md REPORT.md SOURCES.md AI_USE.md Makefile docker-compose.yml Dockerfile .env.example
```

## 6. Core engine design (implement exactly this; keep it simple and explainable)

### 6.1 Units
- Use **integer frames** internally for all source and timeline positions (`fps` from the source). Seconds are derived (`frames / fps`) and rounded to 3 decimals for output. This removes float drift and makes hashing deterministic.
- For variable-frame-rate sources: normalise to a constant fps (a `normalized fps` stored in the intake record) at intake/render time and state it in warnings.

### 6.2 Transcript
```
Word { id: "w0001" (deterministic, index-based), text, start_s, end_s, speaker, confidence, flags[] }
Transcript { words[], sentences[(first_word_idx, last_word_idx)], speakers[], warnings[] }
```
Sentences derived from punctuation/pauses if the STT does not provide them. Handle STT mistakes: flag words with confidence < `LOW_CONFIDENCE_THRESHOLD` (env, default 0.6).

### 6.3 Timeline = derived state
```
Timeline {
  source { sha256, duration_frames, fps, has_audio }
  segments[]   // OUTPUT ORDER. Each: { src_in_f, src_out_f, speed }
  overlays[]   // captions/text: { id, type, text, style, anchor{start_word_id,end_word_id} }
  effects[]    // mute / bleep: { id, type, anchor{start_word_id,end_word_id} }
  music        // { track, duck_db, ... } (see 6.7)
  warnings[]
}
```
- Initially: one segment `[0, duration_frames)` at speed 1.
- **Output time of a source frame** = sum of output lengths of earlier segments + `(f - seg.src_in) / seg.speed`. Segment output length = `max(1, round((src_out - src_in) / speed))` frames. Provide `Layout` helpers: `src_to_out(frame)`, `out_to_src(frame)`, `out_range_to_src_ranges(a, b)` (a current-time range may map to several source ranges), `word_out_span(word)`.
- A word is "alive" if its midpoint frame lies inside a segment. Overlay/effect display time is computed from its alive words only.

### 6.4 History = ordered edit groups (the source of truth)
```
Edit { id: "e1","e2",... (monotonic counter, never reused), instruction, ops[] }   // one instruction = one Edit
Session { stack[], redo[] , pending_question?, counters }
timeline = replay(source, stack)
```
- **Stored ops are RESOLVED, source-anchored ops** (word IDs or source-frame ranges), never output times and never dependent on the time policy. That way replay is deterministic and later edits survive restores of earlier ones.
- Op types stored: `cut{ranges}`, `keep_only{range}`, `speed{range,factor}`, `reorder{range,before}`, `add_caption{id,text,start_word,end_word,style}`, `style{id,style}`, `mute{id,..}`, `bleep{id,..}`, `music{...}`, `restore{target_edit_id}`.
- **Replay algorithm:** `disabled = {op.target for every restore op in stack}`; iterate `stack` in order, skip edits in `disabled`, apply each op to (segments, overlays, effects).
- **Undo:** pop last edit from `stack` to `redo`. Reverses exactly one instruction (including a restore edit, which is why "undo" after "put the intro back" re-cuts the intro). **Redo:** push back. **Any new edit clears `redo`.** Edit ids come from a monotonic counter so ids stay stable and deterministic.
- **Restore a specific earlier edit** = append a new Edit containing `restore{target}`. The target edit is skipped in replay, everything after it stays. Overlays whose words had been cut automatically reappear.

### 6.5 Operation rules (document each in `docs/timeline_explained.md`)

| Concern | Rule |
|---|---|
| Cut | Remove source ranges from segments (split segments at range edges). Overlays whose alive-word count becomes 0 become **dormant** (not deleted) and the answer warns the user. Restoring revives them. |
| Keep only | Equivalent to cutting everything outside the range. Reject if it leaves nothing. |
| Reorder | Move a contiguous section before a destination point; anchors follow words automatically. Reject if destination is inside the section. |
| Speed | Split segments at range edges, set `speed`. Supported range from env: `MIN_SPEED=0.25`, `MAX_SPEED=4.0`; outside range is **rejected** with the allowed range. **Overlap rule: last instruction wins (replace) on the overlapped region**; document it and state it in the answer when overlap occurs. |
| Caption timing | End time is at least `MIN_CAPTION_S=0.7` after start, even when sped up. |
| Partial cut of a captioned phrase | Defined rule: **caption shrinks to the surviving words** (status `partial`) and the answer warns "now covers k of n words". |
| Cut boundary placement | Cuts defined by words are **snapped to the midpoint of the silence gap** before the first word and after the last word so no word is clipped. |
| Frame grid | All segment boundaries are integer frames. |
| Mute / bleep | Anchored to word IDs. Bleep = silence + sine tone mix. If any anchored word has confidence < threshold, the answer includes a low-confidence warning (Outstanding: flagged, not bleeped blindly). Filler removal that would cut into a bleeped/muted word skips that word and reports it. |
| Filler removal | Always remove `um`, `uh`, `erm`, `ah` style tokens. Remove `like` **only** when a rule says it is a filler (for example: not followed by a noun/adjective phrase, set off by pauses > X ms, or between commas). Rule must be explicit and unit-tested with positive and negative sentences. |
| Silence removal | Remove gaps longer than N seconds (N from the instruction), leaving a small configurable pad. **Deliberate pauses** marked by the user ("keep that pause") are protected and survive later silence removal. |
| Auto-caption | Create caption overlays over the whole transcript (sentence or fixed-length chunks), each anchored to word IDs. |
| Background music | Add a music track (bundled royalty-free/own-generated file, licence recorded) that automatically ducks under speech using sidechain compression. |
| Repeated instruction | Safe: if applying changes nothing, answer `rejected` with code `NO_CHANGE` and the reason ("0:30-0:40 was already removed"). Never double-apply. |
| Impossible / whole-video cut | `rejected` (`CUT_ALL`, `OUT_OF_RANGE`, `INVALID_SPEED`, etc.). |
| Multi-part instruction | Each part resolved independently; parts that are valid are applied; invalid parts are reported **by name and reason**; if a part needs clarification, ask before applying anything. Document this "apply valid parts, report the rest" rule. If every part fails, `rejected`. |
| Conflict with earlier edit | `rejected` naming the earlier edit id and instruction. |

### 6.6 Time policy
- Default: **current (edited) time**: what the creator sees in the player after earlier edits.
- Overrides: "in the original", "original time 1:00".
- Every answer that interpreted a time includes a note such as: `Times read as current (edited) time: 1:00-1:30 = 1:30-2:00 in the original.`
- Same policy is used by cut, keep, speed, reorder, mute, caption-at-time, "first N seconds", "last N seconds".
- `timeparse` supports: `1:20`, `1:02:03`, `80 seconds`, `two minutes`, `a minute twenty`, `a minute and twenty seconds`, `twenty five seconds`, `the first 30 seconds`, `the last 10 seconds`, `at 5:00`, `from 1:00 to 1:30`, `between 0:30 and 0:40`. Out-of-range times are rejected with `OUT_OF_RANGE` (clamp only when the start is valid and the end overshoots; then say so).

### 6.7 Determinism rules
- No random IDs, wall-clock times or unordered iteration anywhere in the timeline or hash.
- `timeline_hash = sha256(canonical_json(timeline_without_warnings_timestamps))` with sorted keys, compact separators, numbers derived from integer frames.
- LLM translation: temperature 0, seed if supported, strict JSON schema, and **cache parsed ops per (provider, model, normalized instruction, compact context digest)** in Postgres so replays and reruns are identical. Tests use the mock parser.
- A test runs every session 3 times and asserts identical `timeline_hash` per step.

## 7. Instruction pipeline and the three answers

### 7.1 LLM output schema ("intent ops")
The LLM (or mock) may output ONLY a list of intent ops in this JSON shape (validate with pydantic; reject anything else):
```json
{"ops":[
 {"op":"cut","target":{"kind":"time","from_start":30}},
 {"op":"cut","target":{"kind":"topic","query":"pricing"}},
 {"op":"cut","target":{"kind":"phrase","text":"thanks","relation":"after"}},
 {"op":"speed","target":{"kind":"time","start":60,"end":90,"policy":null},"factor":2.0},
 {"op":"add_caption","text":"Welcome!","target":{"kind":"phrase","text":"welcome to the channel"}},
 {"op":"style","target":{"kind":"caption"},"style":{"color":"red"}},
 {"op":"restore","target":{"kind":"overlap","start":0,"end":30,"policy":"original"}},
 {"op":"undo"},{"op":"redo"},
 {"op":"render"}
]}
```
Target kinds: `time` (start/end/from_start/from_end/policy), `phrase` (text, relation at|before|after, optional speaker), `topic` (query, optional speaker), `caption` (optional text), `edit`/`overlap`/`any` for restore, `end`/`start` for reorder destinations. Add the rest of the operations (mute, bleep, reorder, keep_only, filler removal, silence removal, auto_caption, music) in the same style. The LLM prompt must include the schema, a few-shot set of examples, and **must forbid** inventing timestamps from the transcript (the resolver does that).

### 7.2 Answers (exact response shape per step; also what `results.json` stores)
```json
{
 "status": "applied | needs_clarification | rejected",
 "operations": [ ... resolved ops in seconds for readability ... ],
 "summary": "Video length 5:00 -> 4:30 (-0:30). Caption 'Welcome!' now plays at 0:12-0:14, still on the same words. Times read as current time ...",
 "question": null,
 "options": null,
 "warnings": [],
 "timeline_hash": "…", "duration_s": 270.0,
 "time_policy": "current", "edit_id": "e2",
 "error": null
}
```
- **needs_clarification:** a specific question with options. Example: `"pricing" is mentioned 3 times at 1:05, 2:40 and 3:15. Which one, or all?` Options: each match (time in current time + snippet) plus `all`. **Never guess on anything destructive.** Nothing is applied until the answer arrives; the pending question is stored in the session; the next `answer` resolves it ("the second one", "2", "all", "last", "first").
- **rejected:** `error: {code, message, details}` (UPPER_SNAKE_CASE): impossible, out of range, contradicts earlier edit (name it), no match, `NO_CHANGE`, `CUT_ALL`, `INVALID_SPEED`, `NO_TARGET` (e.g. "make it bigger" when no caption exists), `INSTRUCTION_NOT_UNDERSTOOD`.
- Reference resolution: "that caption" / "make it shorter" / "put back what I removed" / "the boring part" **ask with options or state an assumption with options** and never silently guess.
- Meaning-based search ("pricing" finds "cost", "fee"): show the matches (time + snippet) in the clarification before cutting.
- Two speakers: "remove everything the guest says about their old job" removes only the guest's matching lines and asks about the host's lead-in question.

## 8. Rendering (separate job)

- Build one ffmpeg `filter_complex` from `segments`: per segment `trim`/`atrim` + `setpts`/`asetpts`, speed via `setpts` and chained `atempo` (each between 0.5 and 2.0), then `concat`.
- Normalise to constant fps and 48k audio first (`fps=`, `aresample=async=1`) to survive VFR phone video.
- Captions: generate an ASS file from the timeline (times computed from anchors), burn in with the `subtitles`/`ass` filter. Style options (color, size) map to ASS styles.
- Mute: volume 0 over alive-word ranges. Bleep: silence + sine tone.
- Music ducking: `sidechaincompress` with the speech track as the sidechain; music faded and trimmed to the output length.
- No-audio video: either works (video only) or fails with a clear `NO_AUDIO_TRACK` rejection per documented rule.
- **Verifier (`render/verify.py`)**: ffprobe the output; assert `abs(rendered_frames - timeline_frames) <= 1` and `abs(audio_dur - video_dur) <= 1 frame`; emit the numbers to a JSON used for the Metrics tab (max A/V offset, max length difference across all renders). Also provide a caption-position check (extract a frame at a caption's computed time and assert text pixels exist, or OCR if available).

## 9. HTTP API and Integration Standard (10 points, all required)

All routes under `/api/v1/`.

```
POST /api/v1/videos                      upload (multipart) or {"url": "..."}  → 202 {job_id}  (intake + transcription job)
GET  /api/v1/videos/{id}                 metadata (duration, fps, audio, sha256, status)
GET  /api/v1/videos/{id}/transcript
POST /api/v1/sessions                    {"video_id"} → session
POST /api/v1/sessions/{id}/instructions  {"say": "..."} or {"answer": "..."} → the 3-way answer (fast, synchronous 200)
GET  /api/v1/sessions/{id}/timeline      current timeline + timeline_hash
GET  /api/v1/sessions/{id}/history       edit stack, redo stack
GET  /api/v1/sessions/{id}/timeline.otio OpenTimelineIO export
POST /api/v1/sessions/{id}/render        → 202 {job_id}
GET  /api/v1/jobs/{job_id}               {status: queued|running|succeeded|failed, progress 0-100, result links, error}
GET  /health     GET /ready
```

| # | Requirement | Implementation and acceptance test |
|---|---|---|
| 1 | One-command start/test | `docker compose up` starts api, worker, postgres, minio, minio-init (healthchecks + `depends_on: service_healthy`, named volumes, migrations on startup, connect-retry). `make test` runs unit+integration+e2e with mock adapters, no keys, no internet. `.env.example` lists EVERY setting with comments. |
| 2 | OpenAPI 3.1 | `openapi.yaml` in repo root. A CI test fetches `/openapi.json` from the running app and asserts it matches `openapi.yaml` exactly (generate the file from the app with a script and commit it; test fails on drift). |
| 3 | Background jobs | Anything slower than 2 s (intake/transcription, embeddings build, render) is a job. POST returns **202** with `job_id`. Optional `callback_url` in the request; callback body signed **HMAC-SHA256** in header `X-IncuBrix-Signature` (secret from env). Progress 0-100 updates during render (parse ffmpeg `-progress`). Retries with backoff for callbacks. |
| 4 | Safe retries | `Idempotency-Key` header on **every POST**. Table stores (key, route, request-hash, response/job_id). Same key + same body returns the same stored response/job and never repeats the work (including instruction POSTs, so the same instruction is never applied twice). Same key + different body returns `IDEMPOTENCY_KEY_REUSED` (409). |
| 5 | Clear errors | Every error is `{"error":{"code","message","retryable","details"}}` with UPPER_SNAKE_CASE codes listed in the README table. Global exception handler. Transient problems (DB/S3/LLM timeout) → `retryable:true`; bad input/rejected edit → false. |
| 6 | Configuration | pydantic-settings reads everything from env: DB URL, S3 endpoint/keys/bucket, model names, providers, limits (max upload size, MIN/MAX speed, thresholds, rate limits), paths, HMAC secret, API token. No hard-coded paths, keys or model names. |
| 7 | Files in/out | Input as upload or URL (URL fetch with size/time limits and scheme allowlist; support `file://` fixtures only in test mode). All outputs to S3-compatible storage (MinIO) with presigned download links in job results. |
| 8 | Logs & health | Structured JSON logs: `timestamp, level, message, job_id, correlation_id` (from `X-Correlation-Id`, generated if missing and echoed in the response header). `/health` = process alive. `/ready` = checks Postgres and MinIO and returns 503 when either is down. |
| 9 | Output manifest | Every job result includes `manifest.json`: providers and model names+versions used (STT, LLM, embedder), settings, seed, timings per stage, cost estimate (STT + LLM tokens × price from env), warnings, licence/rights info (every model and media asset such as the music track), `ai_generated` flags. Stored in MinIO and linked. |
| 10 | Batch runner | `python -m incubrix.runner.batch <folder-or-file> --api http://localhost:8000 --out results/` runs every session YAML against the API and writes all `results.json`, final timelines and videos into one folder plus a `summary.json`. |

Also: protect the demo with a bearer token (env) and a **rate limit** (env-configurable) so it cannot be abused; document cold-start time.

## 10. Session runner (the grader's tool)

`python -m incubrix.runner.session sessions/example.yaml --api URL --out out/`
Accepts exactly this file format:
```yaml
video: https://.../interview.mp4
steps:
 - say: "Add a caption 'Welcome!' when I say welcome to the channel"
 - say: "Cut the first 30 seconds"
 - say: "Remove the part about pricing"
   answer: "the second one"   # sent automatically only if the system asks a question
 - say: "Undo"
 - say: "render"
```
Writes `results.json` with, per step: `status, operations, summary, question, options, timeline_hash, duration_s` (+ warnings, edit_id, time_policy), then the final timeline JSON and the rendered video path/link. `render` steps trigger the render job, poll it, download the file. Exit code non-zero if the API is unreachable; never crash on a `rejected` step (it is a valid answer). Supports `--repeat 3` that checks identical `timeline_hash` across runs (determinism metric).

## 11. Levels checklist (build in this order; each item needs a test)

**Core (required):** all must-haves; the 8-step worked example reproduces exactly on your own test video; one single-speaker and one multi-speaker video.

**Advanced:**
- Edits that shift each other: captions follow words; caption whose words are fully cut is removed (dormant) and the user is told; partial-cut rule; sped-up captions keep the 0.7 s minimum.
- Ambiguity/references: "that caption", "the boring part", "make it shorter", "put back what I removed" ask or state an assumption with options; semantic search shows matches before cutting.
- Conflicts: repeated instructions safe; deliberate pauses survive silence removal; impossible/whole-video cuts rejected; overlapping speed rule documented; multi-part all/partial reporting.
- Two speakers: guest-only removal and clarification about the host's lead-in.

**Outstanding:**
- Imperfect data: snap cuts to silence; low-confidence flags; VFR and no-audio handled or failed clearly; overlapping speech and possible speaker-label swap produce warnings.
- Scale: 60-minute video, each instruction responds in **< 3 s** (excluding first transcription and render). Requires: indexes built once at intake (word index by id, sentence embeddings, silence gaps), O(log n) or O(segments) layout lookups, incremental caching of the replayed timeline (cache keyed by history hash), no re-reading the transcript from disk per request. Provide a benchmark script that generates a synthetic 60-min transcript and reports median/slowest latency per instruction type.
- Export: OpenTimelineIO export endpoint + test that re-imports the file.
- Proof: **>= 40 scripted scenarios** with expected results (`tests/scenarios/*.yaml`) running in CI (GitHub Actions workflow).

## 12. Hard cases to implement AND test (the hidden tests are of these kinds)

- Caption added, then its exact sentence cut, then that cut restored (caption must come back).
- Section reordered, then a word inside it muted; filler removal that would cut a bleeped word.
- "Remove 0:30-0:40" sent twice; "add a caption at 5:00" on a shorter video; time formats "80 seconds", "a minute twenty", "the last 10 seconds".
- "Undo the second-to-last cut but keep the music"; "make it bigger" when no caption exists.
- "Remove everything after I say thanks" where thanks occurs 4 times (must ask).
- "Speed up 10x", "cut everything", and a three-part instruction where one part is impossible.
- Two-speaker video with speaker-label swap, overlapping speech, background music, and a phone video with variable frame rate.
Each must end in a sensible, explained answer. **Never fail silently**: use warnings/errors.

## 13. Required worked example (must be an automated end-to-end test)

1. "Add a caption 'Welcome!' when I say welcome to the channel" → Applied, anchored to those 4 words (original ~0:42-0:44).
2. "Cut the first 30 seconds" → Applied, video 30 s shorter, caption now at ~0:12-0:14 on the same words.
3. "Remove the part about pricing" → needs_clarification (3 mentions). Answer "the second one" → Applied.
4. "Speed up from 1:00 to 1:30 by 2x" → Applied, states time policy (current).
5. "Actually, put the intro back" → Applied (restore edit of step 2). First 30 s return, caption back at ~0:42, pricing cut and speed change stay.
6. "Undo" → reverses step 5 only; intro cut again.
7. "Make that caption red" → Applied to the only caption.
8. "Render" → A/V sync, length matches timeline, caption on the right words.
Test fixture: a synthetic transcript (5 minutes, 0.5 s per word, "welcome to the channel" at 42.0 s, "pricing" sentences at 65 s, 160 s, 195 s, two speakers after 200 s) plus a real short test video generated locally with ffmpeg (`testsrc`/tone, or your own recorded talking-head clip); all media must be your own, generated or public-domain.

## 14. Documentation and evidence deliverables

- `README.md`: what it does, architecture diagram, setup in **< 10 commands**, how to run tests and batch runner, ALL settings, and a table of every error code.
- `openapi.yaml`, `schemas/*.json`, `docs/timeline_explained.md` (one page: how edits stay attached to content).
- `REPORT.md` (2-4 pages): approaches considered and why, what works, what does not and why, measured numbers, cost per minute of video, what next.
- `SOURCES.md`: every model, API, library, dataset, media source with licence and commercial-use note (no non-commercial or AGPL in the default path).
- `AI_USE.md`: every AI coding tool used and for what.
- `tests/` + test pack index linking each claim to a file; `sessions/` with **>= 15** scripted sessions, their `results.json`, final timelines, rendered videos, **including failures**.
- Metrics script producing: instruction latency (median/slowest on 5 and 60 min), render accuracy (max A/V offset and length difference in frames), determinism (% identical hashes across 3 runs, must be 100%), clarification quality (asked/assumed vs guessed), word clipping count on 20 cuts (script exports 20 cut boundaries and checks each boundary lies in a silence gap; also provide an audio snippet export for the manual listening check), cost per minute (STT + LLM).
- Submission form helper: generate `evidence_index.md` mapping every Must-have, level item and Integration point to its proof file/test. Mark anything unproven as **Partial/No**. A false claim disqualifies the candidate.

## 15. Working agreement for the agent

- Work in small steps. After each step: run the tests, show output, commit with a descriptive message. Never one giant commit.
- Write the test first or alongside every behaviour.
- Pure functions for timeline logic; no I/O inside the engine; engine has zero dependency on FastAPI, DB, LLM or ffmpeg.
- No secrets in code, logs or screenshots. Keys only in env. `.env` in `.gitignore`.
- All external AI behind adapters with a `mock`. Default config must run fully offline.
- Prefer clarity over cleverness: the candidate must explain every file in a live session. Add short docstrings stating the rule each function implements.
- If a requirement is ambiguous, pick the simplest option consistent with this document, write the decision in `docs/decisions.md`, and continue. Do not stop to ask unless blocked.
- Keep a running `STATUS.md` listing each Must-have/level item as Done / Partial / Not done with evidence links.

