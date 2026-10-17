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
