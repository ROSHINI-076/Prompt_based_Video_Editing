"""Tests for pure intent-target resolution to source-anchored results."""

from __future__ import annotations

from incubrix.engine.models import (
    AddCaptionOp,
    CutOp,
    Edit,
    KeepOnlyOp,
    RestoreOp,
    Sentence,
    SourceInfo,
    Transcript,
    Word,
)
from incubrix.engine.replay import replay
from incubrix.engine.resolver import (
    CaptionTarget,
    PhraseTarget,
    ReorderDestination,
    Resolver,
    RestoreTarget,
    TimeTarget,
    TopicTarget,
    _choice_index,
    _format_ranges,
)

FPS = 30.0


def _transcript() -> Transcript:
    entries = [
        ("hello", "A"),
        ("there", "A"),
        ("thanks", "A"),
        ("everyone", "A"),
        ("we", "A"),
        ("say", "A"),
        ("thanks", "A"),
        ("again", "A"),
        ("and", "A"),
        ("then", "A"),
        ("thanks", "A"),
        ("for", "A"),
        ("watching", "A"),
        ("thanks", "B"),
        ("all", "B"),
        ("the", "A"),
        ("cost", "A"),
        ("is", "A"),
        ("high", "A"),
        ("today", "A"),
    ]
    words = [
        Word(
            id=f"w{i + 1:04d}",
            text=text,
            start_s=float(i),
            end_s=i + 0.6,
            speaker=speaker,
        )
        for i, (text, speaker) in enumerate(entries)
    ]
    return Transcript(
        words=words,
        sentences=[
            # Each thanks occurrence and the synonym-bearing topic are separate sentences.
            # Deliberately includes words not mentioned in test phrases between occurrences.
            Sentence(first_word_idx=start, last_word_idx=end)
            for start, end in ((0, 3), (4, 7), (8, 12), (13, 14), (15, 19))
        ],
    )


def _source(duration_s: float = 100.0) -> SourceInfo:
    return SourceInfo(
        sha256="fixture",
        duration_frames=round(duration_s * FPS),
        fps=FPS,
    )


def _edit(edit_id: str, *ops) -> Edit:
    return Edit(id=edit_id, instruction=f"edit {edit_id}", ops=list(ops))


def _resolver(
    duration_s: float = 100.0,
    edits: list[Edit] | None = None,
    embedder=None,
) -> Resolver:
    transcript = _transcript()
    stack = edits or []
    timeline = replay(_source(duration_s), stack, transcript)
    return Resolver(timeline, transcript, stack, embedder=embedder)


def test_four_phrase_occurrences_are_ambiguous_with_all_option():
    result = _resolver().resolve_phrase(PhraseTarget(text="thanks"))

    assert result.status == "AMBIGUOUS"
    assert len(result.options) == 5
    assert [option.choice for option in result.options] == [1, 2, 3, 4, "all"]
    assert [option.snippet for option in result.options[:4]] == [
        "thanks", "thanks", "thanks", "thanks"
    ]
    assert all(option.current_start_s is not None for option in result.options[:4])


def test_choice_and_range_format_helpers_cover_valid_and_invalid_inputs():
    assert _format_ranges([], FPS) == "no surviving source interval"
    assert _format_ranges([(0, 30)], FPS) == "0:00-0:01"
    assert _choice_index(None, 2) is None
    assert _choice_index(0, 2) is None
    assert _choice_index(2, 2) == 1
    assert _choice_index("first", 4) == 0
    assert _choice_index("one", 4) == 0
    assert _choice_index("second", 4) == 1
    assert _choice_index("two", 4) == 1
    assert _choice_index("third", 4) == 2
    assert _choice_index("three", 4) == 2
    assert _choice_index("last", 4) == 3
    assert _choice_index("final", 4) == 3
    assert _choice_index("4", 4) == 3
    assert _choice_index("invalid", 4) is None
    assert _choice_index("last", 0) is None


def test_ambiguous_notes_handle_options_without_current_display_times():
    resolver = _resolver()
    with_no_ranges = resolver._ambiguous_note(
        "selection", [resolver_option(choice=1)]
    )
    with_original_edit = resolver._ambiguous_note(
        "selection",
        [resolver_option(choice=1, edit_id="e9", source_ranges=((0, 30),))],
        policy="original",
    )
    with_original_time_option = resolver._ambiguous_note(
        "selection",
        [resolver_option(
            choice=1,
            current_start_s=1.0,
            current_end_s=2.0,
            source_ranges=((30, 60),),
        )],
        policy="original",
    )

    assert with_no_ranges == "Times read as current time: selection."
    assert "e9: 0:00-0:01" in with_original_edit
    assert "original time" in with_original_time_option


def resolver_option(
    choice,
    edit_id=None,
    source_ranges=(),
    current_start_s=None,
    current_end_s=None,
):
    from incubrix.engine.resolver import ResolutionOption

    return ResolutionOption(
        choice=choice,
        edit_id=edit_id,
        source_ranges=source_ranges,
        current_start_s=current_start_s,
        current_end_s=current_end_s,
    )


def test_after_phrase_waits_for_choice_then_returns_tail_source_range():
    resolver = _resolver(duration_s=30.0)
    unresolved = resolver.resolve_phrase(
        PhraseTarget(text="thanks", relation="after")
    )
    chosen = resolver.resolve_phrase(
        PhraseTarget(text="thanks", relation="after", choice=2)
    )

    assert unresolved.status == "AMBIGUOUS"
    assert unresolved.source_ranges == []
    assert chosen.status == "RESOLVED"
    # The second "thanks" is word 7, ending at 6.6s; the silence midpoint is 6.8s.
    assert chosen.source_ranges == [(204, round(30.0 * FPS))]
    assert "current (edited) time" in chosen.note
    assert "original" in chosen.note


def test_time_rejects_start_beyond_video_and_clamps_overshooting_end():
    resolver = _resolver(duration_s=10.0)
    beyond = resolver.resolve_time(TimeTarget(start=11.0, end=12.0))
    clamped = resolver.resolve_time(TimeTarget(start=8.0, end=12.0))

    assert beyond.status == "OUT_OF_RANGE"
    assert beyond.error_code == "OUT_OF_RANGE"
    assert clamped.status == "RESOLVED"
    assert clamped.source_ranges == [(240, 300)]
    assert clamped.clamped is True
    assert "clamped" in clamped.note


def test_bad_time_ranges_reject_negative_and_reversed_bounds():
    resolver = _resolver()
    assert resolver.resolve_time(TimeTarget(start=-1, end=2)).error_code == "BAD_RANGE"
    assert resolver.resolve_time(TimeTarget(start=2, end=2)).error_code == "BAD_RANGE"
    assert resolver.resolve_time(TimeTarget(from_end=-2)).error_code == "BAD_RANGE"


def test_time_rejects_unknown_policy_missing_bounds_and_conflicting_forms():
    resolver = _resolver()

    assert resolver.resolve_time(TimeTarget(policy="wallclock")).error_code == "BAD_RANGE"
    assert resolver.resolve_time(TimeTarget()).error_code == "BAD_RANGE"
    assert resolver.resolve_time(TimeTarget(start=1)).error_code == "BAD_RANGE"
    assert resolver.resolve_time(
        TimeTarget(end=2, from_start=1)
    ).error_code == "BAD_RANGE"
    assert resolver.resolve_time(
        TimeTarget(start=1, from_end=2)
    ).error_code == "BAD_RANGE"
    assert resolver.resolve_time(
        TimeTarget(start=100, end=101)
    ).error_code == "BAD_RANGE"


def test_last_duration_longer_than_video_covers_full_source():
    result = _resolver(duration_s=10).resolve_time(TimeTarget(from_end=20))

    assert result.status == "RESOLVED"
    assert result.source_ranges == [(0, round(10 * FPS))]


def test_current_time_with_no_surviving_segments_returns_no_match():
    resolver = _resolver(duration_s=10)

    result = resolver.resolve_time(TimeTarget(start=0, end=0.001))

    assert result.status == "NO_MATCH"


def test_current_first_and_last_time_ranges_map_through_earlier_cut():
    earlier_cut = _edit("e1", CutOp(ranges=[(0, 20 * round(FPS))]))
    resolver = _resolver(edits=[earlier_cut])

    first = resolver.resolve_time(TimeTarget(from_start=30.0))
    last = resolver.resolve_time(TimeTarget(from_end=10.0))

    assert first.status == "RESOLVED"
    assert first.source_ranges == [(20 * round(FPS), 50 * round(FPS))]
    assert last.status == "RESOLVED"
    assert last.source_ranges == [(90 * round(FPS), 100 * round(FPS))]
    assert "current (edited) time" in first.note
    assert "0:30 = 0:20-0:50" in first.note


def test_original_time_policy_ignores_earlier_cut():
    earlier_cut = _edit("e1", CutOp(ranges=[(0, 20 * round(FPS))]))
    resolver = _resolver(edits=[earlier_cut])

    result = resolver.resolve_time(
        TimeTarget(from_start=30.0, policy="original")
    )

    assert result.source_ranges == [(0, 30 * round(FPS))]
    assert "original time" in result.note


def test_topic_pricing_matches_sentence_containing_cost():
    result = _resolver().resolve_topic(TopicTarget(query="pricing"))

    assert result.status == "RESOLVED"
    assert result.source_ranges == [(round(14.8 * FPS), round(19.6 * FPS))]
    assert result.word_ids == [
        "w0016", "w0017", "w0018", "w0019", "w0020"
    ]


def test_topic_ambiguity_supports_all_and_invalid_choice():
    resolver = _resolver()
    all_matches = resolver.resolve_topic(
        TopicTarget(query="thanks", choice="all")
    )
    invalid_choice = resolver.resolve_topic(
        TopicTarget(query="thanks", choice=20)
    )

    assert all_matches.status == "RESOLVED"
    assert len(all_matches.source_ranges) == 4
    assert invalid_choice.status == "AMBIGUOUS"
    assert invalid_choice.options[-1].choice == "all"


def test_topic_multiple_sentence_matches_include_one_all_choice():
    result = _resolver().resolve_topic(TopicTarget(query="thanks"))

    assert result.status == "AMBIGUOUS"
    assert [option.choice for option in result.options] == [1, 2, 3, 4, "all"]


def test_phrase_only_in_cut_region_is_ignored_with_explanation():
    # The first four words contain "thanks" at index 2; cut that occurrence.
    cut = _edit("e1", CutOp(ranges=[(0, 4 * round(FPS))]))
    resolver = _resolver(edits=[cut])

    result = resolver.resolve_phrase(PhraseTarget(text="hello there thanks everyone"))

    assert result.status == "NO_MATCH"
    assert "only in cut regions" in (result.detail or "")


def test_phrase_handles_empty_query_speaker_mismatch_and_all_occurrences():
    resolver = _resolver()
    assert resolver.resolve_phrase(PhraseTarget(text="!!!")).status == "NO_MATCH"
    assert resolver.resolve_phrase(
        PhraseTarget(text="thanks", speaker="nobody")
    ).status == "NO_MATCH"

    result = resolver.resolve_phrase(PhraseTarget(text="thanks", choice="all"))
    assert result.status == "RESOLVED"
    assert len(result.source_ranges) == 4
    assert len(result.word_ids) == 4


def test_phrase_invalid_choice_remains_ambiguous_without_selecting():
    result = _resolver().resolve_phrase(PhraseTarget(text="thanks", choice=10))

    assert result.status == "AMBIGUOUS"
    assert result.source_ranges == []
    assert result.options[-1].choice == "all"


def test_phrase_detects_match_outside_source_without_treating_it_as_cut():
    transcript = Transcript(
        words=[Word(id="w0001", text="late", start_s=20, end_s=21)]
    )
    result = Resolver(replay(_source(10), [], transcript), transcript).resolve_phrase(
        PhraseTarget(text="late")
    )

    assert result.status == "NO_MATCH"
    assert "No alive transcript match" in (result.detail or "")


def test_before_relation_uses_tail_before_selected_phrase():
    result = _resolver().resolve_phrase(
        PhraseTarget(text="thanks", relation="before", choice=2)
    )

    assert result.status == "RESOLVED"
    assert result.source_ranges == [(0, round(5.8 * FPS))]


def test_phrase_after_relation_uses_surviving_tail_when_silence_boundary_was_cut():
    cut_boundary = round(2.8 * FPS)
    edit = _edit("e1", CutOp(ranges=[(cut_boundary, cut_boundary + 6)]))
    result = _resolver(edits=[edit]).resolve_phrase(
        PhraseTarget(text="thanks", relation="after", choice=1)
    )

    assert result.status == "RESOLVED"
    assert result.source_ranges[0][0] == cut_boundary + 6


def test_phrase_before_relation_maps_to_surviving_word_when_snap_boundary_was_cut():
    # Cut through the start word's opening frames: the word midpoint survives,
    # while its silence-gap snap boundary and exact word start do not.
    cut = _edit("e1", CutOp(ranges=[(0, round(2.2 * FPS))]))
    result = _resolver(edits=[cut]).resolve_phrase(
        PhraseTarget(text="thanks", relation="before", choice=1)
    )

    assert result.status == "RESOLVED"
    assert result.source_ranges == [(round(2.2 * FPS), round(2.3 * FPS))]



def test_topic_match_in_cut_region_is_ignored():
    cut = _edit("e1", CutOp(ranges=[(15 * round(FPS), 20 * round(FPS))]))
    result = _resolver(edits=[cut]).resolve_topic(TopicTarget(query="pricing"))

    assert result.status == "NO_MATCH"
    assert "only in cut regions" in (result.detail or "")


def test_topic_keyword_inside_cut_sentence_is_not_matched_from_surviving_neighbors():
    # "cost" is cut but the rest of its sentence remains alive.
    cut = _edit("e1", CutOp(ranges=[(16 * round(FPS), round(16.7 * FPS))]))
    result = _resolver(edits=[cut]).resolve_topic(TopicTarget(query="pricing"))

    assert result.status == "NO_MATCH"
    assert "only in cut regions" in (result.detail or "")


def test_restore_overlap_filters_active_cut_edits_and_reports_ambiguity():
    first = _edit("e1", CutOp(ranges=[(0, 30 * FPS)]))
    second = _edit("e2", CutOp(ranges=[(30 * FPS, 60 * FPS)]))
    resolver = _resolver(edits=[first, second])

    ambiguous = resolver.resolve_restore(
        RestoreTarget(overlap=(0, 60), any=False)
    )
    selected = resolver.resolve_restore(
        RestoreTarget(overlap=(0, 30), any=False)
    )
    selected_all = resolver.resolve_restore(
        RestoreTarget(overlap=(0, 60), choice="all")
    )
    by_id = resolver.resolve_restore(RestoreTarget(edit_id="e1"))

    assert ambiguous.status == "AMBIGUOUS"
    assert [option.edit_id for option in ambiguous.options[:2]] == ["e1", "e2"]
    assert ambiguous.options[-1].choice == "all"
    assert selected.status == "RESOLVED"
    assert selected.edit_ids == ["e1"]
    assert selected_all.edit_ids == ["e1", "e2"]
    assert by_id.edit_ids == ["e1"]


def test_restore_ignores_edits_already_disabled_by_restore():
    cut = _edit("e1", CutOp(ranges=[(0, 30 * FPS)]))
    restore = _edit("e2", RestoreOp(target_edit_id="e1"))

    result = _resolver(edits=[cut, restore]).resolve_restore(
        RestoreTarget(any=True)
    )

    assert result.status == "NO_MATCH"


def test_restore_validates_selector_range_and_candidate_id():
    active_cut = _edit("e1", CutOp(ranges=[(-10, round(3 * FPS)), (5000, 6000)]))
    keep_partial = _edit("e2", KeepOnlyOp(range=(round(10 * FPS), round(20 * FPS))))
    keep_empty = _edit("e3", KeepOnlyOp(range=(-30, 4000)))
    resolver = _resolver(edits=[active_cut, keep_partial, keep_empty])

    invalid_selector = resolver.resolve_restore(RestoreTarget())
    invalid_overlap = resolver.resolve_restore(RestoreTarget(overlap=(-1, 2)))
    beyond_overlap = resolver.resolve_restore(RestoreTarget(overlap=(101, 102)))
    no_overlap = resolver.resolve_restore(RestoreTarget(overlap=(12, 15)))
    unknown_edit = resolver.resolve_restore(RestoreTarget(edit_id="e999"))

    assert invalid_selector.error_code == "BAD_RANGE"
    assert invalid_overlap.error_code == "BAD_RANGE"
    assert beyond_overlap.error_code == "OUT_OF_RANGE"
    assert no_overlap.error_code == "NO_MATCH"
    assert unknown_edit.error_code == "NO_MATCH"
    assert resolver._cut_ranges_for_edit(keep_partial) == [
        (0, round(10 * FPS)),
        (round(20 * FPS), round(100 * FPS)),
    ]
    assert resolver._cut_ranges_for_edit(keep_empty) == []
    assert resolver._cut_ranges_for_edit(active_cut) == [(0, round(3 * FPS))]


def test_restore_invalid_ambiguous_choice_returns_options():
    edits = [
        _edit("e1", CutOp(ranges=[(0, 30)])),
        _edit("e2", CutOp(ranges=[(60, 90)])),
    ]
    result = _resolver(edits=edits).resolve_restore(
        RestoreTarget(any=True, choice="invalid")
    )

    assert result.status == "AMBIGUOUS"
    assert result.options[-1].choice == "all"

    selected = _resolver(edits=[
        _edit("e1", CutOp(ranges=[(0, 30)])),
        _edit("e2", CutOp(ranges=[(60, 90)])),
    ]).resolve_restore(RestoreTarget(any=True, choice=2))
    assert selected.status == "RESOLVED"
    assert selected.edit_ids == ["e2"]


def test_phrase_ranges_snap_to_surrounding_silence_midpoints():
    result = _resolver().resolve_phrase(
        PhraseTarget(text="thanks", choice=2)
    )

    # Second thanks is [6.0, 6.6); adjacent words end/start at 5.6/7.0,
    # so the chosen cut interval is snapped to 5.8s and 6.8s.
    assert result.status == "RESOLVED"
    assert result.source_ranges == [(round(5.8 * FPS), round(6.8 * FPS))]
    assert result.word_ids == ["w0007"]


def test_caption_resolution_uses_canonical_display_projection():
    transcript = _transcript()
    add_caption = _edit(
        "e1",
        AddCaptionOp(
            overlay_id="caption-1",
            text="Welcome",
            start_word_id="w0001",
            end_word_id="w0002",
        ),
    )
    timeline = replay(_source(), [add_caption], transcript)
    result = Resolver(timeline, transcript, [add_caption]).resolve_caption(
        CaptionTarget(text="Wel")
    )

    assert result.status == "RESOLVED"
    assert result.overlay_ids == ["caption-1"]
    assert result.options[0].current_start_s is not None
    assert "original" in result.note


def test_ambiguous_caption_selection_exposes_projected_times_and_choice():
    transcript = _transcript()
    caption_edits = [
        _edit(
            "e1",
            AddCaptionOp(
                overlay_id="caption-1",
                text="Thanks one",
                start_word_id="w0003",
                end_word_id="w0003",
            ),
        ),
        _edit(
            "e2",
            AddCaptionOp(
                overlay_id="caption-2",
                text="Thanks two",
                start_word_id="w0007",
                end_word_id="w0007",
            ),
        ),
    ]
    timeline = replay(_source(), caption_edits, transcript)
    resolver = Resolver(timeline, transcript, caption_edits)

    ambiguous = resolver.resolve_caption(CaptionTarget(text="Thanks"))
    selected = resolver.resolve_caption(CaptionTarget(text="Thanks", choice=2))
    selected_all = resolver.resolve_caption(CaptionTarget(text="Thanks", choice="all"))

    assert ambiguous.status == "AMBIGUOUS"
    assert len(ambiguous.options) == 3
    assert ambiguous.options[0].current_start_s is not None
    assert ambiguous.options[1].current_start_s is not None
    assert selected.overlay_ids == ["caption-2"]
    assert selected_all.overlay_ids == ["caption-1", "caption-2"]


def test_caption_no_match_dormant_caption_and_invalid_choice():
    transcript = _transcript()
    add_caption = _edit(
        "e1",
        AddCaptionOp(
            overlay_id="caption-1",
            text="Hidden caption",
            start_word_id="w0001",
            end_word_id="w0001",
        ),
    )
    cut = _edit("e2", CutOp(ranges=[(0, round(1.0 * FPS))]))
    timeline = replay(_source(), [add_caption, cut], transcript)
    resolver = Resolver(timeline, transcript, [add_caption, cut])

    assert resolver.resolve_caption(CaptionTarget(text="none")).status == "NO_MATCH"
    dormant = resolver.resolve_caption(CaptionTarget())
    assert dormant.status == "RESOLVED"
    assert dormant.options[0].current_start_s is None

    multiple = [
        add_caption,
        _edit(
            "e3",
            AddCaptionOp(
                overlay_id="caption-2",
                text="Second caption",
                start_word_id="w0002",
                end_word_id="w0002",
            ),
        ),
    ]
    ambiguous_timeline = replay(_source(), multiple, transcript)
    invalid = Resolver(
        ambiguous_timeline, transcript, multiple
    ).resolve_caption(CaptionTarget(choice="unknown"))
    assert invalid.status == "AMBIGUOUS"
    assert invalid.options[-1].choice == "all"



def test_reorder_destination_supports_start_end_time_and_phrase():
    resolver = _resolver(duration_s=30.0)

    start = resolver.resolve_reorder_destination(ReorderDestination(kind="start"))
    end = resolver.resolve_reorder_destination(ReorderDestination(kind="end"))
    by_time = resolver.resolve_reorder_destination(
        ReorderDestination(kind="time", time_s=2.0)
    )
    ambiguous_phrase = resolver.resolve_reorder_destination(
        ReorderDestination(kind="phrase", phrase="thanks")
    )
    chosen_phrase = resolver.resolve_reorder_destination(
        ReorderDestination(kind="phrase", phrase="thanks", choice=2)
    )

    assert start.destination_frame == 0
    assert end.destination_frame == 30 * FPS
    assert by_time.destination_frame == 2 * FPS
    assert ambiguous_phrase.status == "AMBIGUOUS"
    assert chosen_phrase.destination_frame == round(5.8 * FPS)


def test_reorder_destination_rejects_empty_timeline_missing_or_invalid_times():
    transcript = _transcript()
    empty = replay(_source(10), [], transcript).model_copy(update={"segments": []})
    resolver = Resolver(empty, transcript)

    assert resolver.resolve_reorder_destination(
        ReorderDestination(kind="start")
    ).status == "NO_MATCH"
    assert resolver.resolve_reorder_destination(
        ReorderDestination(kind="end")
    ).status == "NO_MATCH"
    assert resolver.resolve_reorder_destination(
        ReorderDestination(kind="time")
    ).status == "BAD_RANGE"
    assert resolver.resolve_reorder_destination(
        ReorderDestination(kind="time", time_s=-1)
    ).status == "BAD_RANGE"
    assert resolver.resolve_reorder_destination(
        ReorderDestination(kind="time", time_s=0)
    ).status == "OUT_OF_RANGE"


def test_reorder_time_reports_no_source_for_inconsistent_layout_cache():
    resolver = _resolver(duration_s=10)
    resolver.layout._total += 1

    result = resolver.resolve_reorder_destination(
        ReorderDestination(kind="time", time_s=10)
    )

    assert result.status == "NO_MATCH"


def test_reorder_destination_handles_end_boundary_and_invalid_time_or_phrase_choices():
    resolver = _resolver(duration_s=30)
    at_end = resolver.resolve_reorder_destination(
        ReorderDestination(kind="time", time_s=30)
    )
    beyond = resolver.resolve_reorder_destination(
        ReorderDestination(kind="time", time_s=31)
    )
    invalid_kind = resolver.resolve_reorder_destination(
        ReorderDestination(kind="other")
    )
    missing_phrase = resolver.resolve_reorder_destination(
        ReorderDestination(kind="phrase")
    )
    invalid_choice = resolver.resolve_reorder_destination(
        ReorderDestination(kind="phrase", phrase="thanks", choice=50)
    )
    all_choice = resolver.resolve_reorder_destination(
        ReorderDestination(kind="phrase", phrase="thanks", choice="all")
    )

    assert at_end.destination_frame == round(30 * FPS)
    assert beyond.status == "OUT_OF_RANGE"
    assert invalid_kind.status == "BAD_RANGE"
    assert missing_phrase.status == "BAD_RANGE"
    assert invalid_choice.status == "BAD_RANGE"
    assert all_choice.status == "BAD_RANGE"


def test_reorder_phrase_reports_no_match_and_handles_snapped_boundary_cut():
    transcript = _transcript()
    cut = _edit("e1", CutOp(ranges=[(0, round(1.9 * FPS))]))
    timeline = replay(_source(30), [cut], transcript)
    resolver = Resolver(timeline, transcript, [cut])

    assert resolver.resolve_reorder_destination(
        ReorderDestination(kind="phrase", phrase="not present")
    ).status == "NO_MATCH"
    destination = resolver.resolve_reorder_destination(
        ReorderDestination(kind="phrase", phrase="thanks", choice=1)
    )
    assert destination.status == "RESOLVED"
    assert destination.destination_frame == round(1.8 * FPS)


def test_embedder_hook_can_add_a_sentence_match():
    sentence_embedder = lambda query, sentence: 0.9 if "hello there" in sentence else 0.0
    result = _resolver(embedder=sentence_embedder).resolve_topic(
        TopicTarget(query="salutations")
    )

    assert result.status == "RESOLVED"
    assert result.word_ids == ["w0001", "w0002", "w0003", "w0004"]


def test_topic_speaker_filter_empty_sentence_list_and_fallback_sentence_boundaries():
    transcript = Transcript(
        words=[
            Word(id="w0001", text="cost", start_s=0, end_s=0.4, speaker="A"),
            Word(id="w0002", text="rises.", start_s=0.5, end_s=0.9, speaker="A"),
            Word(id="w0003", text="fee", start_s=2.0, end_s=2.4, speaker="B"),
            Word(id="w0004", text="increases", start_s=2.5, end_s=2.9, speaker="B"),
        ]
    )
    resolver = Resolver(replay(_source(10), [], transcript), transcript)

    assert resolver._sentences() == [(0, 1), (2, 3)]
    assert resolver.resolve_topic(
        TopicTarget(query="pricing", speaker="B")
    ).status == "RESOLVED"
    assert resolver.resolve_topic(
        TopicTarget(query="pricing", speaker="unknown")
    ).status == "NO_MATCH"
    assert Resolver(
        replay(_source(10), [], Transcript(words=[])), Transcript(words=[])
    )._sentences() == []


def test_topic_sentence_fallback_splits_on_punctuation_and_pause_and_keeps_tail():
    transcript = Transcript(
        words=[
            Word(id="w0001", text="cost.", start_s=0, end_s=0.3),
            Word(id="w0002", text="other", start_s=0.5, end_s=0.8),
            Word(id="w0003", text="fee", start_s=2.5, end_s=2.8),
        ]
    )
    resolver = Resolver(replay(_source(10), [], transcript), transcript)

    assert resolver._sentences() == [(0, 0), (1, 1), (2, 2)]
    assert resolver.resolve_topic(TopicTarget(query="pricing")).status == "AMBIGUOUS"


def test_topic_empty_query_and_sentence_with_no_matching_keyword():
    resolver = _resolver()
    assert resolver.resolve_topic(TopicTarget(query="!!!")).status == "NO_MATCH"
    assert resolver.resolve_topic(TopicTarget(query="unrelated")).status == "NO_MATCH"




def test_time_rejects_empty_current_timeline():
    timeline = replay(
        _source(10.0),
        [_edit("e1", KeepOnlyOp(range=(0, 0)))],
        _transcript(),
    )
    resolver = Resolver(timeline, _transcript())

    result = resolver.resolve_time(TimeTarget(start=0, end=1))

    assert result.status in {"NO_MATCH", "BAD_RANGE"}
