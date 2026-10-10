"""Song footage must be grounded in real, verified lyrics, not a mood slideshow."""
from unittest.mock import patch
import json
import pytest

from scripts.music_factory_lyrics_story import (
    lyric_lines, qualified_catalog_lyrics, build_lyric_story, BIBLE,
)
from scripts.music_factory_worker import generated_prompt

CLIP = "192229ce-90c5-44ca-8e0e-d0a77d86b2b7"
TITLE = "A Story"


def source():
    return {
        "clip_id": CLIP, "title": TITLE, "media_kind": "song",
        "channel": "BC TRAP GOD", "duration_seconds": 160, "genre": "rap",
    }


def lyrics():
    places = (
        "Driving alone on the highway while the city lights pass me",
        "My phone rings and I answer it with shaking hands",
        "I cross the bridge and turn toward our old house",
        "Rain hits the windshield as I pull over beside the door",
        "I remember the last words she said when the room went quiet",
        "My reflection catches the candle burning on the table",
        "I open the letter and read the message twice",
        "I step outside and search the street for my friend",
        "I leave the car behind as daylight fills the road",
        "I take the first step toward home and the morning sun",
    )
    return "\n".join(f"[Verse {i}]\n{line}\n{line} again\n" for i, line in enumerate(places, 1))


def test_exactly_thirty_lyric_grounded_shots_and_handoffs():
    raw = lyrics()
    s = build_lyric_story(source(), raw)
    assert s["planning_source"] == "canonical_suno_lyrics_grounded_v2"
    assert len({scene["location"] for scene in s["scenes"]}) == 1
    assert all("ONE plain-hooded protagonist" in scene["action"] for scene in s["scenes"])
    assert s["media_kind"] == "song"
    assert len(s["scenes"]) == 30
    assert {scene["master_beat"] for scene in s["scenes"]} == set(range(1, 11))
    assert [x["lyric_line_index"] for x in s["scenes"]] == sorted(x["lyric_line_index"] for x in s["scenes"])
    assert all(s["scenes"][i]["opening_state"] == s["scenes"][i-1]["end_state"] for i in range(1, 30))
    assert all(s["scenes"][i]["lyric_excerpt"] in raw for i in range(30))
    for i, scene in enumerate(s["scenes"], 1):
        prompt = generated_prompt(scene, s, i, "song")
        assert BIBLE in prompt
        assert scene["lyric_excerpt"][:80] in prompt
        assert "no duplicates" in prompt
        assert "NO lettering" in prompt
        assert len(prompt) <= 1485
        assert "no face drift" in prompt
        assert "STRICTLY BEATS" not in prompt
    assert s["lyrics_alignment_verified"] is False
    assert s["publishing_approved"] is False


def test_missing_placeholder_and_mismatched_lyrics_are_rejected():
    assert lyric_lines("[Instrumental]") == []
    assert lyric_lines("[Chorus]\n(yeah, yeah, yeah)\n(bow, bow)") == []
    assert lyric_lines("[Intro]\n(walkin')\n(divine, divine)") == []
    assert qualified_catalog_lyrics({}, CLIP, TITLE) is None
    assert qualified_catalog_lyrics({CLIP: {"title": "Different", "lyrics": lyrics()}}, CLIP, TITLE) is None
    assert qualified_catalog_lyrics({CLIP: {"title": TITLE, "lyrics": lyrics()}}, CLIP, TITLE) == lyrics()
    with pytest.raises(ValueError, match="lyrics"):
        build_lyric_story(source(), "[Instrumental]")
    with pytest.raises(ValueError):
        build_lyric_story({**source(), "media_kind":"beat"}, lyrics())


def test_legacy_abstract_song_prompt_fails_closed():
    from scripts.music_factory_story import build_story
    s = build_story(source())
    with pytest.raises(ValueError, match="lyric"):
        generated_prompt(s["scenes"][0], s, 1, "song")


def test_repeated_chorus_indices_respect_song_order():
    repeated = "\n".join(["I walk home when the sky turns dark"] * 32)
    s = build_lyric_story(source(), repeated)
    indices = [scene["lyric_line_index"] for scene in s["scenes"]]
    assert indices == sorted(indices)
    assert indices[-1] >= 30
