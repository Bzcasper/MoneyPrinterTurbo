"""Regression checks for the scheduled Suno beat/song factory."""

import json
from unittest.mock import patch
import pytest

from scripts.music_factory_catalog import (
    _classify,
    _mp3_song_candidate,
    claim_draft,
)
from scripts.music_factory_story import build_story
from scripts.music_factory_worker import generated_prompt
from scripts.video_autopilot_queue import record_result

CLIP = "192229ce-90c5-44ca-8e0e-d0a77d86b2b7"


def candidate(kind="song"):
    is_beat = kind == "beat"
    return {
        "clip_id": CLIP,
        "queue_id": 32,
        "media_kind": kind,
        "channel": "Strictly Beats" if is_beat else "BC TRAP GOD",
        "project_type": "type_beat" if is_beat else "song",
        "make_instrumental": is_beat,
        "title": "Different Flavor Girls",
        "genre": "original trap",
        "duration_seconds": 151,
        "source_sha256": "a" * 64,
        "source_modal_path": f"clips/{CLIP}/audio/{CLIP}.wav",
        "source_volume": "suno-playback-library",
        "source_format": "wav",
        "project_id": "autovideo_" + CLIP.replace("-", ""),
        "apply_producer_tag": is_beat,
        "publishing_approved": False,
    }


@pytest.mark.parametrize("kind", ["beat", "song"])
def test_distinct_lane_storyboard_has_real_30_shot_handoffs(kind):
    source = candidate(kind)
    story = build_story(source)
    assert len(story["scenes"]) == 30
    assert story["media_kind"] == kind
    assert story["channel"] == source["channel"]
    assert story["strict_image_conditioning_verified"] is False
    assert len(set(s["master_beat"] for s in story["scenes"])) == 10
    assert [s["master_beat"] for s in story["scenes"]] == [
        n for n in range(1, 11) for _ in range(3)
    ]
    assert all(
        story["scenes"][n]["opening_state"] == story["scenes"][n - 1]["end_state"]
        for n in range(1, 30)
    )
    assert all(
        len(generated_prompt(scene, story, n, kind)) <= 1485
        for n, scene in enumerate(story["scenes"], 1)
    )
    assert all(
        "Negative constraints: no face drift" in generated_prompt(scene, story, n, kind)
        for n, scene in enumerate(story["scenes"], 1)
    )
    assert story == build_story(source)
    if kind == "song":
        assert "STRICTLY BEATS" not in json.dumps(story).upper()
    else:
        assert "BC TRAP GOD" not in json.dumps(story).upper()


def test_reject_wrong_lane_or_invalid_canon():
    with pytest.raises(ValueError):
        build_story(candidate("unknown"))
    item = candidate("beat")
    item["clip_id"] = "not-a-uuid"
    with pytest.raises(ValueError):
        build_story(item)


def test_catalog_beat_identity_is_not_inferred_from_a_vocal():
    row = candidate("beat")
    row["project_type"] = "type_beat"
    row["make_instrumental"] = False
    row["duration_seconds"] = 151.0
    item = {
        "id": CLIP,
        "title": row["title"],
        "duration": 151.0,
        "_wav_sha256": "a" * 64,
        "wavBytes": 1_000_000,
    }
    assert _classify(row, item) is None
    row["make_instrumental"] = True
    row["media_kind"] = "beat"
    row["permitted_download"] = False
    row["rights_status"] = ""
    expected = _classify(row, item)
    assert expected["media_kind"] == "beat"
    assert expected["apply_producer_tag"] is True
    assert expected["source_rights_verified"] is False


def test_mp3_song_selector_checks_expected_modal_identity():
    row = dict(
        clip_id=CLIP,
        queue_id=32,
        project_type="song",
        channel="BC TRAP GOD",
        make_instrumental=False,
        title="Different Flavor Girls",
        duration_seconds=151,
        source_modal_path=f"{CLIP}/{CLIP}.mp3",
        source_format="mp3",
        source_volume="suno-songs-v2",
        source_sha256="b" * 64,
        source_bytes=5_000_000,
        style_tags="heavy 808",
        permitted_download=False,
        rights_status="",
    )
    with patch("scripts.music_factory_catalog.psql", return_value=[json.dumps(row)]):
        result = _mp3_song_candidate()
    assert result["apply_producer_tag"] is False
    assert result["source_format"] == "mp3"
    assert result["source_volume"] == "suno-songs-v2"
    assert result["publishing_approved"] is False
    row["source_modal_path"] = "evil/relative.mp3"
    with patch("scripts.music_factory_catalog.psql", return_value=[json.dumps(row)]):
        assert _mp3_song_candidate() is None


def test_atomic_retry_claim_does_not_duplicate_other_source():
    item = candidate("beat")
    item.update(queue_id=32, source_bytes=1_000_000)
    with (
        patch("scripts.music_factory_catalog.next_candidate", return_value=item),
        patch(
            "scripts.modal_wav_preflight.verify_song",
            return_value={
                "ok": True,
                "catalog_sha256": item["source_sha256"],
                "modal_path": item["source_modal_path"],
            },
        ),
        patch(
            "scripts.music_factory_catalog.psql", return_value=["BEGIN", "32", "COMMIT"]
        ) as db,
    ):
        res = claim_draft(CLIP)
    assert res["claimed"] is True
    sql = db.call_args.args[0]
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "ON CONFLICT (clip_id) DO UPDATE" in sql
    assert "media_video_autopilot.stage='FAILED_RETRYABLE'" in sql
    assert "media_video_autopilot.source_queue_id=EXCLUDED.source_queue_id" in sql
    assert "FROM one WHERE TRUE" in sql


def test_failed_worker_releases_claim_with_bounded_retry_backoff():
    with patch("scripts.video_autopilot_queue.psql", side_effect=[[CLIP], []]) as db:
        out = record_result(
            CLIP, stage="FAILED_RETRYABLE", details={"reason": "model_unavailable"}
        )
    assert out["updated"] is True
    assert db.call_count == 2
    retry_statement = db.call_args_list[1].args[0]
    assert "q.attempts>=q.max_attempts" in retry_statement
    assert "interval '15 minutes'" in retry_statement
    assert "a.stage='FAILED_RETRYABLE'" in retry_statement


def test_never_treat_non_approved_story_as_release_ready():
    story = build_story(candidate("song"))
    assert story["publishing_approved"] is False
    assert story["strict_image_conditioning_verified"] is False
