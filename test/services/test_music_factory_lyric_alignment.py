"""Faster-Whisper alignment evidence and thirty-scene timeline safeguards."""
from __future__ import annotations

import pytest

from scripts.music_factory_lyric_alignment import align_segments, normalized_words

WORDS = (
    "The winding road bends beyond the mountain tonight",
    "A heavy rain is striking the windshield this morning",
    "Your message arrives when the traffic lights change",
    "The paper letter lies beside my old keyboard",
    "We stand by the doorway watching the river",
    "I reach for the telephone and hear your voice",
    "The street becomes a canyon full of shadows",
    "A single car stops next to the empty house",
    "I open the door and see the old photograph",
    "The sun rises beyond the buildings as I leave",
)


def fixture_words(duration=150.0):
    lines = list(WORDS) * 3
    # Distinct simple recognizable lyric words at paced spoken times.
    words = []
    tokens = [token for line in lines for token in normalized_words(line)]
    for n, tok in enumerate(tokens):
        t = 2.0 + n * (duration - 12.0) / len(tokens)
        words.append({"start": t, "end": t + .12, "word": tok, "probability": .92})
    return lines, [{"start": 2.0, "end": duration - 10.0, "words": words}]


def test_known_lyrics_make_monotone_30_scene_timings():
    lines, segments = fixture_words()
    report = align_segments(lines, segments, 150.0)
    b = report["scene_boundaries_seconds"]
    assert report["status"] == "ASR_ANCHORED_PRIVATE_REVIEW"
    assert len(b) == 31 and b[0] == 0 and b[-1] == 150.0
    assert all(b[i] < b[i + 1] for i in range(30))
    assert report["evidence"]["matching_act_count"] >= 7
    assert not report["word_level_forced_alignment_verified"]
    assert not report["public_release_approved"]


def test_low_match_transcription_cannot_assert_alignment():
    lines, _ = fixture_words()
    bogus = [{"start": 0, "end": 150, "words": [
        {"start": i*1.2, "word": "zzzz", "probability": .99}
        for i in range(90)
    ]}]
    with pytest.raises(ValueError, match="HOLD_LYRIC_TIMING_LOW_ASR_MATCH"):
        align_segments(lines, bogus, 150)


def test_bad_audio_duration_is_not_allowed():
    lines, segments = fixture_words()
    with pytest.raises(ValueError, match="song length"):
        align_segments(lines, segments, 0)
