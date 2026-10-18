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

## D004: Caption display times are derived, not stored or hashed directly

**Date:** 2026-10-17
**Context:** The spec says "never store an overlay/effect as an output timestamp" (s2) and "End time
is at least MIN_CAPTION_S=0.7 after start, even when sped up" (s6.5). These two rules must coexist.
**Decision:**
- `Timeline.overlays` stores only word-ID anchors (`Anchor.start_word_id`, `Anchor.end_word_id`)
  and status (`active`/`dormant`/`partial`). No display timestamps are stored there.
- `SessionManager.get_timeline_output()` is the public engine output path. It calls
  `timeline_to_output()`, which calls `compute_overlay_displays()` and derives display times via
  `Layout.caption_out_span()` (including `MIN_CAPTION_S`) for API/renderer consumers.
- `timeline_hash` hashes the stored `Timeline`, not the output projection, and does **not** hash
  display times directly. It covers source metadata, segments (including speed), and overlay anchors,
  but not transcript word timestamps. Therefore the hash guarantees stable display times only for
  the same session/transcript; it is not a cross-session hash of caption timing. Keeping display
  times out of the hash avoids making the stored timeline depend on a derived presentation value.
  A consumer needing a cross-session output identity must also identify the transcript.
- The `min_s` parameter of `compute_overlay_displays` defaults to `MIN_CAPTION_S = 0.7` in the
  engine.
**Rationale:** Storing timestamps would violate the core invariant ("edits belong to content").
Computed output times remain deterministic within a session and are exposed at the output boundary.

## D005: `out_to_src` fallback protects the layout-length invariant

**Date:** 2026-10-17
**Context:** The final `return None` is unreachable for a normally constructed `Layout`, but must
remain safe if the cached total ever becomes inconsistent with the segment lengths.
**Decision:** Keep the fallback without a coverage exclusion.
For a normal `Layout`, the invariant is:
1. The bounds check at the top of `out_to_src` ensures `0 <= out_frame < self._total`.
2. `self._total = sum(segment_output_length(s) for s in self._segments)`.
3. The loop assigns each segment a tile `[out_starts[i], out_starts[i] + seg_len)`.
4. These tiles are contiguous and non-overlapping, and their union is exactly `[0, self._total)`.
5. Therefore any `out_frame` satisfying the precondition falls into exactly one tile, so the
   loop always returns before exhausting segments.
6. The `return None` after the loop is unreachable for a normally constructed layout.

The test `test_out_to_src_fallback_when_cached_total_is_inconsistent` deliberately breaks the
cached-total invariant to prove the defensive `None` behavior is covered.

## D006: Target resolver returns source-anchored resolutions only

**Date:** 2026-10-17
**Context:** The instruction response layer needs a pure, deterministic boundary between parsed
targets and edit operations.
**Decision:** `Resolver` accepts a replayed `Timeline`, its `Transcript`, and optional edit history.
Resolved edit ranges are original source-frame pairs and word/edit/overlay references remain IDs.
Time policies are seconds at the resolver input; phrase matches use the existing `snap_to_silence`
rule; topic embeddings are an optional `(query, sentence) -> score` callable. Ambiguous matches
carry numbered current-time/snippet options plus `all`, and never select a candidate implicitly.
Restore overlap bounds are expressed in original-time seconds. Caption option display times are
read only from `timeline_to_output()`.
**Rationale:** This keeps the resolver independent of API, persistence, and model providers while
making the response layer responsible for deciding how to present a resolution status.

## D007: Clarify repeated current-time destructive edits

**Date:** 2026-10-17
**Context:** Replaying the same current-time cut, keep-only, or speed target after an edit addresses different source content because the edited timeline has shifted.
**Decision:** When a destructive time target in current time exactly matches the target stored with the most recent active edit, ask for explicit confirmation and show the current-to-original conversion. A yes creates a new edit against the current timeline; a no rejects with `NO_CHANGE`. Exact repeats in original time, and phrase repeats whose target was already removed, remain `NO_CHANGE`.
**Rationale:** Current-time operations are interpreted where the creator is now looking, but applying an identical destructive request twice without confirmation would silently affect different content. Original-time and phrase targets remain content-anchored and do not silently shift.

## D008: Keep string enums compatible without `enum.StrEnum`

**Date:** 2026-10-17
**Context:** Ruff's UP042 recommends replacing classes that inherit from both `str` and
`enum.Enum` with `enum.StrEnum`. That requires Python 3.11 and changes how `str()` behaves.
**Decision:** Keep `OverlayStatus` and `EffectType` as `str, enum.Enum` classes and ignore UP042.
The source remains compatible with both Python 3.10 and 3.11; no `enum.StrEnum` conversion is used.
**Rationale:** Preserving the existing string conversion behavior is preferable to applying an
automatic enum migration solely to satisfy this lint rule.

## D009: Conservative filler classification

**Date:** 2026-10-17
**Context:** Filler cleanup must remove common hesitation words without deleting meaningful uses
of "like".
**Decision:** `ALWAYS_FILLER_WORDS` is `{um, uh, erm, er, ah, hmm}`. A normalized "like" is a filler
only if it ends with a comma or a neighboring silence gap is at least `LIKE_PAUSE_THRESHOLD_S`
(0.25 seconds), and the preceding normalized word is not in
`LIKE_PREVIOUS_WORD_EXCLUSIONS`:
`{i, you, we, they, would, looks, look, feels, feel, sounds, sound, don't, didn't, doesn't, not,
just, and, or}`. Filler cuts use `snap_to_silence`; mute/bleeped filler words are preserved and
reported.
**Rationale:** The explicit preceding-word guard protects grammatical "like" phrases, while the
comma/pause signal permits conservative cleanup of discourse fillers.

## D011: Durable job leases and retry scheduling

**Date:** 2026-10-18
**Context:** Phase 4B needs Postgres workers to claim work safely, recover crashes, and retry
transient failures without introducing Redis.
**Decision:** Jobs are claimed in a transaction using `SELECT ... FOR UPDATE SKIP LOCKED`. A
heartbeat lease identifies the current worker; expired leases are requeued until the attempt limit.
The queue records `available_at` so retries use exponential backoff without sleeping while holding a
database connection.
**Rationale:** Postgres remains the sole queue and job state survives process restarts. `available_at`
is an additional scheduling field required to represent backoff durably.

## D012: HTTP API uses a standard error envelope and middleware protections

**Date:** 2026-10-18
**Context:** The API layer must give predictable error payloads and protect the demo from abuse.
**Decision:** All API responses use a common `{"error": {"code", "message", "retryable", "details"}}`
shape, with route-specific 200/202 success responses remaining untouched. HTTP auth is enforced by a
Bearer-token middleware on every route except `/health` and `/ready`, and a per-token rate limiter is
kept in memory. Idempotency-Key replay is applied at the ASGI boundary so repeated POSTs return the
stored response or a `IDEMPOTENCY_KEY_REUSED` 409 for a reused key with a different body.
**Rationale:** This matches the brief’s policy without changing engine semantics: the worker, queue, and
engine remain detached from the HTTP layer while the API improves safety and consistency.

## D010: Source-anchored silence protection and music intent

**Date:** 2026-10-17
**Context:** Silence trimming and music intent must remain deterministic, reversible timeline edits.
**Decision:** Silence is measured between adjacent alive words in current output order, then the
trimmed ranges are resolved back to source frames with `MIN_SILENCE_PAD_S = 0.15`. A `keep_pause`
edit stores the preceding word ID; replay carries those IDs into the timeline and later silence
passes preserve the associated output gap. Auto-captions use sentence word IDs and a maximum chunk
size of `AUTO_CAPTION_MAX_WORDS = 8`. Music operations store `track_id`, `duck_db`, and
`duck_when="speech"` as timeline intent only; the default allow-list is
`ALLOWED_MUSIC_TRACK_IDS = {"bundled_default"}`.
**Rationale:** Anchors and integer-frame ranges survive replay, undo, redo, restores and reorders;
the engine records music ducking intent without performing audio processing.

## D011: Explicit restore ordinals and guest-question confirmation

**Date:** 2026-10-17
**Context:** Two conversation cases need deterministic selectors without timestamp guesses: restoring
the second-to-last active cut, and deciding whether a guest-topic cut should also remove the host's
immediately preceding question.
**Decision:** `restore` accepts `{"kind":"cut_ordinal","from_end":N}` and counts only active edits
that contain cut ranges; an unavailable ordinal is rejected with `NO_TARGET` and names the number
of active cut edits. A speaker-constrained topic cut that has an immediately preceding question
from another speaker asks whether to remove or keep that host question before applying anything.
The answer is stored in the pending structured intent; the guest match remains speaker-constrained.
**Rationale:** The cut selector is based on replayed history, and the lead-in decision is explicit,
so neither operation guesses based on shifted clock positions or silently removes another speaker.

## D012: Keep provider integration outside the engine

**Date:** 2026-10-18
**Context:** Phase 3 adds natural-language parsing, speech-to-text and semantic embeddings without
weakening the pure-engine boundary.
**Decision:** Adapter protocols and implementations live under `incubrix/adapters/`, while parsing,
cache selection and response orchestration live under `incubrix/pipeline/`. The engine receives an
optional `(query, sentence) -> similarity` callable through its existing resolver hook; it does not
import any adapter or pipeline module. The in-memory parser cache keys provider, model, prompt
version, normalized instruction and a digest of compact context. Database persistence is deferred
to Phase 4.
**Rationale:** Offline mock defaults stay deterministic, and the same resolver logic can work with
keyword fallback or an explicitly selected semantic adapter.

## D013: Support Python 3.10 and keep heavyweight providers optional

**Date:** 2026-10-18
**Context:** The Phase 3 implementation must run on Python 3.10 and 3.11 while keeping tests and
default installs lightweight and offline.
**Decision:** Set the declared minimum and lint/type-check targets to Python 3.10. Pin all runtime
dependencies exactly. `faster-whisper` and `sentence-transformers` are exact-pinned optional extras
and are imported only when their adapters are invoked.
**Rationale:** Mock providers require no keys, network, model downloads or heavyweight dependencies.

## D014: Report renderer requests honestly until the rendering phase

**Date:** 2026-10-18
**Context:** The Phase 3 natural-language acceptance path includes a final “render” step, but the
current repository has not implemented the renderer or render job.
**Decision:** The parser recognizes the command and returns the ordinary response envelope with an
explicit warning that no video was rendered. It does not claim a render succeeded.
**Rationale:** This permits the complete parser-to-engine conversation to be exercised without
misrepresenting deferred Phase 5 behavior.

## D015: Database persistence, pure mapping, and optimistic concurrency

**Date:** 2026-10-18
**Context:** Phase 4A adds persistent database storage for videos, transcripts, sessions, edits,
and parser cache without compromising engine purity.
**Decision:**
- SQLAlchemy 2 declarative models reside in `incubrix/db/models.py`.
- `incubrix/db/mapping.py` handles bidirectional translation between database rows and engine models.
  The engine itself has zero imports from `incubrix/db`.
- All identifiers (edit IDs, overlay IDs, word IDs, effect IDs) round-trip exactly.
- `SessionRepository.save_session` executes in ONE transaction with atomic version checking
  (`sessions.version`). Stale version updates raise `ConcurrencyError`, preventing interleaving.
- Replayed session reconstruction guarantees `loaded.manager.get_timeline_hash() == hash_before`.
- `run_migrations()` uses PostgreSQL advisory locks (`pg_advisory_lock`) to coordinate concurrent
  process startups (API and worker), and uses `Base.metadata.create_all` for in-memory SQLite.

## D017: Phase 4B audit is a checkpoint, not a launch to 4C

**Date:** 2026-10-18
**Context:** The project has reached the Phase 4B queue/API checkpoint, but the remaining cross-cutting HTTP integration checks are intentionally still deferred.
**Decision:** Keep the scope bounded to a verified Phase 4B audit. Items still marked Partial or Not done remain in the status tracker and are not treated as complete evidence until they have an HTTP test against real Postgres and MinIO. The project does not advance into 4C or Phase 5 until those checks exist and pass.
**Rationale:** This preserves a clean “evidence before completion” workflow and prevents claiming route-level correctness without integration proof.

## D016: Content-addressed video storage and streaming source verification

**Date:** 2026-10-18
**Context:** Must-have 1 requires that the original source video is never damaged or overwritten,
with a verification mechanism to re-hash the source after a session.
**Decision:**
- Source videos are stored under a content-addressed S3 key: `sources/{sha256}.mp4`.
- If a source object already exists under its SHA-256 key, it is never overwritten (`exists()` check).
- `compute_file_sha256` and `verify_source` stream data in 64KB chunks; full video files are never
  loaded into memory.
- `verify_source` re-downloads/streams the stored object, computes its SHA-256, and compares it to
  the recorded intake SHA-256. Any tampering or corruption returns an invalid result and raises
  `SourceIntegrityError`.
