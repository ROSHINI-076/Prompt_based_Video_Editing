"""Tests for engine/session.py — undo/redo semantics and session management.

Required by spec (Phase 1):
- undo/redo semantics: one instruction = one undo
- new edit clears redo stack
- restoring an early edit leaves later edits intact
"""

from __future__ import annotations

import pytest

from incubrix.engine.models import (
    AddCaptionOp,
    CutOp,
    RestoreOp,
    SourceInfo,
    SpeedOp,
    Transcript,
    Word,
    make_word_id,
    segment_output_length,
)
from incubrix.engine.session import SessionManager
from incubrix.engine.timeline import total_duration_frames

FPS = 30.0


def _src(duration_s: float = 10.0) -> SourceInfo:
    return SourceInfo(sha256="test", duration_frames=int(duration_s * FPS), fps=FPS)


def _transcript(n: int = 20) -> Transcript:
    words = []
    t = 0.0
    for i in range(n):
        words.append(Word(id=make_word_id(i), text=f"w{i}", start_s=t, end_s=t + 0.4))
        t += 0.5
    return Transcript(words=words)


class TestSessionBasics:
    def test_empty_session_full_duration(self):
        mgr = SessionManager(_src(10.0), _transcript())
        assert mgr.get_duration_frames() == int(10.0 * FPS)

    def test_push_edit_changes_duration(self):
        mgr = SessionManager(_src(10.0), _transcript())
        mgr.push_edit("cut first 3s", [CutOp(ranges=[(0, 90)])])
        assert mgr.get_duration_frames() == int(10.0 * FPS) - 90

    def test_monotonic_edit_ids(self):
        mgr = SessionManager(_src(10.0), _transcript())
        e1 = mgr.push_edit("cut 1", [CutOp(ranges=[(0, 30)])])
        e2 = mgr.push_edit("cut 2", [CutOp(ranges=[(60, 90)])])
        assert e1.id == "e1"
        assert e2.id == "e2"

    def test_edit_ids_never_reused_after_undo(self):
        """After undo, the next new edit gets a new ID, not the undone one."""
        mgr = SessionManager(_src(10.0), _transcript())
        e1 = mgr.push_edit("cut", [CutOp(ranges=[(0, 30)])])
        mgr.undo()
        e2 = mgr.push_edit("cut again", [CutOp(ranges=[(0, 30)])])
        assert e2.id != e1.id
        assert e2.id == "e2"


class TestUndoRedo:
    def test_undo_restores_previous_state(self):
        mgr = SessionManager(_src(10.0), _transcript())
        original_duration = mgr.get_duration_frames()
        mgr.push_edit("cut first 3s", [CutOp(ranges=[(0, 90)])])
        mgr.undo()
        assert mgr.get_duration_frames() == original_duration

    def test_undo_returns_the_undone_edit(self):
        mgr = SessionManager(_src(10.0), _transcript())
        e = mgr.push_edit("cut", [CutOp(ranges=[(0, 90)])])
        undone = mgr.undo()
        assert undone is not None
        assert undone.id == e.id

    def test_undo_on_empty_returns_none(self):
        mgr = SessionManager(_src(10.0), _transcript())
        assert mgr.undo() is None

    def test_redo_reapplies_edit(self):
        mgr = SessionManager(_src(10.0), _transcript())
        mgr.push_edit("cut first 3s", [CutOp(ranges=[(0, 90)])])
        duration_after_cut = mgr.get_duration_frames()
        mgr.undo()
        mgr.redo_edit()
        assert mgr.get_duration_frames() == duration_after_cut

    def test_redo_on_empty_returns_none(self):
        mgr = SessionManager(_src(10.0), _transcript())
        assert mgr.redo_edit() is None

    def test_new_edit_clears_redo(self):
        """After undo, a new edit must clear the redo stack."""
        mgr = SessionManager(_src(10.0), _transcript())
        mgr.push_edit("cut 1", [CutOp(ranges=[(0, 30)])])
        mgr.push_edit("cut 2", [CutOp(ranges=[(60, 90)])])
        mgr.undo()
        # At this point redo has one item
        assert len(mgr.state.redo) == 1
        # Push a new edit
        mgr.push_edit("cut 3 — new edit", [CutOp(ranges=[(120, 150)])])
        # Redo stack must now be empty
        assert len(mgr.state.redo) == 0

    def test_undo_multiple_steps(self):
        mgr = SessionManager(_src(10.0), _transcript())
        mgr.push_edit("cut 1", [CutOp(ranges=[(0, 30)])])
        mgr.push_edit("cut 2", [CutOp(ranges=[(60, 90)])])
        mgr.push_edit("cut 3", [CutOp(ranges=[(120, 150)])])
        duration = mgr.get_duration_frames()
        mgr.undo()  # undo cut 3
        assert len(mgr.state.stack) == 2
        mgr.undo()  # undo cut 2
        assert len(mgr.state.stack) == 1
        mgr.undo()  # undo cut 1
        assert mgr.get_duration_frames() == int(10.0 * FPS)

    def test_undo_after_restore_re_cuts(self):
        """Undo after 'put the intro back' (restore) should re-cut the intro.

        Step 1: cut 0-3s (e1)
        Step 2: restore e1 (e2) → intro comes back
        Step 3: undo e2 → restore is undone → cut is active again
        """
        mgr = SessionManager(_src(10.0), _transcript())
        original = mgr.get_duration_frames()
        mgr.push_edit("cut intro", [CutOp(ranges=[(0, 90)])])
        duration_after_cut = mgr.get_duration_frames()
        mgr.push_edit("put intro back", [RestoreOp(target_edit_id="e1")])
        assert mgr.get_duration_frames() == original  # intro restored
        mgr.undo()  # undo the restore
        assert mgr.get_duration_frames() == duration_after_cut  # cut is back


class TestSessionHash:
    def test_hash_stable_without_changes(self):
        mgr = SessionManager(_src(10.0), _transcript())
        h1 = mgr.get_timeline_hash()
        h2 = mgr.get_timeline_hash()
        assert h1 == h2

    def test_hash_changes_after_edit(self):
        mgr = SessionManager(_src(10.0), _transcript())
        h_before = mgr.get_timeline_hash()
        mgr.push_edit("cut", [CutOp(ranges=[(0, 90)])])
        h_after = mgr.get_timeline_hash()
        assert h_before != h_after

    def test_hash_restored_after_undo(self):
        mgr = SessionManager(_src(10.0), _transcript())
        h_before = mgr.get_timeline_hash()
        mgr.push_edit("cut", [CutOp(ranges=[(0, 90)])])
        mgr.undo()
        assert mgr.get_timeline_hash() == h_before


class TestGetHistory:
    def test_history_reflects_stack(self):
        mgr = SessionManager(_src(10.0), _transcript())
        mgr.push_edit("cut", [CutOp(ranges=[(0, 90)])])
        hist = mgr.get_history()
        assert len(hist["stack"]) == 1
        assert len(hist["redo"]) == 0

    def test_history_after_undo(self):
        mgr = SessionManager(_src(10.0), _transcript())
        mgr.push_edit("cut", [CutOp(ranges=[(0, 90)])])
        mgr.undo()
        hist = mgr.get_history()
        assert len(hist["stack"]) == 0
        assert len(hist["redo"]) == 1
