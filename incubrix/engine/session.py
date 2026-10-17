"""Session manager: push edits, undo, redo, get timeline.

Rules (section 6.4):
- One instruction = one Edit = one undo step.
- Undo pops last Edit from stack → redo.
- Redo pushes last Edit from redo → stack.
- Any new edit clears the redo stack.
- Edit IDs come from a monotonic counter and are never reused.
- timeline = replay(source, stack) — always derived, never stored.
"""

from __future__ import annotations

from incubrix.engine.hashing import timeline_hash
from incubrix.engine.models import Edit, SessionState, SourceInfo, Transcript
from incubrix.engine.replay import replay
from incubrix.engine.timeline import Layout, timeline_to_output, total_duration_frames


class SessionManager:
    """Manages edit history for a single video session.

    All timeline state is derived by calling replay() on demand.
    The session only stores the source info, transcript, and edit stacks.
    """

    def __init__(self, source: SourceInfo, transcript: Transcript) -> None:
        self._state = SessionState(source=source, transcript=transcript)

    # ── Read-only properties ──────────────────────────────────────

    @property
    def state(self) -> SessionState:
        """Return the raw session state (for serialisation / tests)."""
        return self._state

    @property
    def edit_counter(self) -> int:
        return self._state.edit_counter

    # ── ID generation ─────────────────────────────────────────────

    def _next_edit_id(self) -> str:
        """Monotonic edit ID.  Counter increments before use; ids start at e1."""
        self._state.edit_counter += 1
        return f"e{self._state.edit_counter}"

    # ── Edit operations ───────────────────────────────────────────

    def push_edit(self, instruction: str, ops: list) -> Edit:
        """Apply a new edit.

        Clears the redo stack (any new edit clears redo).
        """
        edit = Edit(id=self._next_edit_id(), instruction=instruction, ops=ops)
        self._state.stack.append(edit)
        self._state.redo.clear()
        return edit

    def undo(self) -> Edit | None:
        """Undo the last edit: pop stack → redo.

        Returns the undone Edit, or None if nothing to undo.
        """
        if not self._state.stack:
            return None
        edit = self._state.stack.pop()
        self._state.redo.append(edit)
        return edit

    def redo_edit(self) -> Edit | None:
        """Redo the last undone edit: pop redo → stack.

        Returns the redone Edit, or None if nothing to redo.
        """
        if not self._state.redo:
            return None
        edit = self._state.redo.pop()
        self._state.stack.append(edit)
        return edit

    # ── Timeline access ───────────────────────────────────────────

    def get_timeline(self):
        """Derive and return the current Timeline by replaying all active edits."""
        return replay(
            self._state.source,
            self._state.stack,
            self._state.transcript,
        )

    def get_timeline_hash(self) -> str:
        """Compute the deterministic SHA-256 hash of the current timeline."""
        return timeline_hash(self.get_timeline())

    def get_timeline_output(self) -> dict:
        """Return the public timeline projection, including derived caption times."""
        timeline = self.get_timeline()
        result = timeline_to_output(timeline, self._state.transcript)
        result["timeline_hash"] = timeline_hash(timeline)
        return result

    def get_layout(self) -> Layout:
        """Return a Layout for the current timeline (for output-time lookups)."""
        tl = self.get_timeline()
        return Layout(tl.segments)

    def get_duration_frames(self) -> int:
        """Return the current output duration in frames."""
        return total_duration_frames(self.get_timeline().segments)

    # ── History ───────────────────────────────────────────────────

    def get_history(self) -> dict:
        """Return the edit stack and redo stack as plain dicts."""
        return {
            "stack": [e.model_dump() for e in self._state.stack],
            "redo": [e.model_dump() for e in self._state.redo],
            "edit_counter": self._state.edit_counter,
        }
