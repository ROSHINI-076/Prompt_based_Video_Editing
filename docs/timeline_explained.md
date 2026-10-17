# Timeline Explained: How Edits Stay Attached to Content

*One page. For anyone who needs to understand — or explain — why a caption
placed on the words "welcome to the channel" still appears over exactly
those words after cuts, speed changes, reorders and restores.*

---

## The core idea: anchor to words, not to the clock

Every caption and audio effect stores **word IDs**, not timestamps.

```
Overlay {
  anchor: { start_word_id: "w0084", end_word_id: "w0087" }  ← permanent
  status: active | dormant | partial
}
```

`w0084` is the word "welcome". It was assigned at transcript-import time and
never changes for the lifetime of the session. The four words *welcome to the
channel* are always `w0084`–`w0087`, no matter what has been cut, sped up, or
reordered.

**Display time is computed on demand**, by asking: "where do those words land
in the current output?" It is never stored.

---

## The timeline: segments in output order

The current state of the video is a list of **segments**:

```
Segment { src_in_f: int, src_out_f: int, speed: float }
```

Each segment is a slice of the original source (`src_in_f` … `src_out_f`,
both in integer frames) played at the given speed multiplier. The output is
exactly the concatenation of those slices.

**Output length of one segment** = `max(1, round((src_out_f − src_in_f) / speed))` frames.

Initially there is one segment `[0, duration_frames)` at `speed=1.0`. Every
edit reshapes this list.

---

## How output time is computed for any source frame

```
out_time(frame f):
  for each segment s (in order):
      if s.src_in_f ≤ f < s.src_out_f:
          return out_start[s] + round((f − s.src_in_f) / s.speed)
  return None  ← frame was cut
```

`out_start[s]` is the cumulative output length of all segments before `s`.
The `Layout` class precomputes these for O(1) lookups.

---

## A word is "alive" if its midpoint lands in a segment

```
midpoint_frame(word) = round((start_s + end_s) / 2 × fps)

is_alive(word) = any segment contains midpoint_frame(word)
```

If a word is alive, its on-screen time can be derived. If it isn't, it was cut.
When a time-based cut clips a word but leaves its midpoint alive, the caption
span uses the word's surviving portion of the segment.

---

## What happens to a caption when words are cut?

| Alive anchor words | Caption status | What the user sees |
|---|---|---|
| **All alive** | `active` | Caption displays normally over the words |
| **Some alive** | `partial` | Caption shrinks to the surviving words; user warned "now covers k of n words" |
| **None alive** | `dormant` | Caption is hidden; user warned it will revive if the cut is restored |

**Dormant captions are never deleted.** They remain in the overlay list so
that a `restore` of the cut brings them back automatically.

Caption times are computed at the output boundary, not stored on the overlay.
`SessionManager.get_timeline_output()` exposes `overlay_displays` derived from
the word anchors and current segments. This projection applies the 0.7-second
minimum (capped at the timeline end), including after a speed change.

---

## The edit stack: the single source of truth

```
Session.stack = [Edit, Edit, ...]
timeline       = replay(source, stack)   ← always derived
```

Each `Edit` contains **resolved, source-anchored ops** — cut ranges are source
frame ranges, caption anchors are word IDs. They never depend on which other
edits exist, so they survive restores, undo/redo, and session reloads.

**Replay algorithm:**

```
disabled = { op.target_edit_id
             for every RestoreOp in every *active* edit }   ← fixpoint

start with: segments = [(0, duration_frames, speed=1.0)]
            overlays = []
            effects  = []

for each edit in stack:
    if edit.id in disabled: skip
    apply each op in the edit
```

The fixpoint iteration means: if you *restore a restore* (undo the "put it
back" instruction), the original cut becomes active again.

---

## Undo and redo

```
push_edit(ops):  stack.append(edit);  redo.clear()
undo():          redo.append(stack.pop())
redo():          stack.append(redo.pop())
```

One instruction = one `Edit` = one undo step. Edit IDs (`e1`, `e2`, …) are
assigned by a monotonic counter and **never reused**, even after undo, so
history references always point to the right edit.

---

## Restore is just another edit

```
restore("e3")  → push Edit { op: RestoreOp(target="e3") }
```

This disables `e3` during replay. Because it is itself an `Edit`, undoing it
re-enables `e3` — which is exactly what "undo the restore" should do.

---

## Determinism guarantee

```
timeline_hash = sha256(canonical_json(timeline, exclude=["warnings"]))
```

`canonical_json` uses sorted keys and compact separators. All positions are
**integer frames** (no floating-point accumulation). The LLM is used only to
translate plain language → structured ops at temperature 0; all timeline
arithmetic is ordinary deterministic code. The same source + the same stack
produces the **identical `timeline_hash` on every run**.
The hash is over the stored timeline and does not include derived caption
display times or transcript timestamps; stable display-time comparisons assume
the same session transcript.

---

## Example: caption survives a cut before it

```
Initial:   [─────────────────────] 5 min
           w0001…w0600

Step 1: Add caption "Welcome!" anchored to w0084–w0087 (t ≈ 42 s)
Step 2: Cut the first 30 seconds (frames 0–900)

Timeline:  [──────────────────]  4:30
           w0001–w0059 cut, w0060–w0600 alive
           w0084–w0087 still alive → caption ACTIVE
           Display time: NOW computed from new layout ≈ 12 s in the output
```

The caption anchor (`w0084`–`w0087`) never changed. The *display time*
shifted from ~42 s to ~12 s automatically, because it is re-derived from the
segment list on every query.
