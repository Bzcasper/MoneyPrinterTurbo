from unittest.mock import patch
import pytest

from scripts.video_autopilot_queue import (
    candidate,
    release_check,
    record_result,
    record_private_upload,
)


def sample_candidate(**extra):
    return {
        "queue_id": 12,
        "clip_id": "80dd5f5a-b131-49ef-b644-6d528449bcb2",
        "title": "Safe Instrumental",
        "media_kind": "beat",
        "channel": "Strictly Beats",
        "sha256": "a" * 64,
        "modal_path": "80dd5f5a-b131-49ef-b644-6d528449bcb2/master.wav",
        "source_format": "wav",
        "modal_volume": "suno-official-master",
        "audio_file_id": 44,
        "duration_seconds": 132.0,
        "bpm": 140.0,
        "commercial_ready": False,
        **extra,
    }


def test_dry_run_never_claims_and_never_assumes_rights():
    with (
        patch("scripts.video_autopilot_queue.psql") as db,
        patch(
            "scripts.video_autopilot_queue.json_one", return_value=sample_candidate()
        ),
    ):
        result = candidate(dry_run=True)
    assert result["media_kind"] == "beat"
    assert result["apply_producer_tag"] is True
    assert result["publishing_approved"] is False
    assert result["rights_ready"] is False
    assert result["generation_preflight_ready"] is False
    db.assert_called_once()  # schema check only, no job INSERT


def test_song_policy_is_never_tagged():
    with (
        patch("scripts.video_autopilot_queue.psql"),
        patch(
            "scripts.video_autopilot_queue.json_one",
            return_value={
                **sample_candidate(),
                "media_kind": "song",
                "channel": "BC TRAP GOD",
            },
        ),
    ):
        result = candidate(dry_run=True)
    assert result["apply_producer_tag"] is False


def test_private_release_gate_fails_closed_for_unreviewed_media():
    with patch("scripts.video_autopilot_queue.json_one", return_value={}):
        result = release_check("80dd5f5a-b131-49ef-b644-6d528449bcb2")
    assert result["release_ready"] is False
    assert result["posting_status"] == "HOLD"


def test_unapproved_private_upload_cannot_be_registered():
    with patch("scripts.video_autopilot_queue.psql", return_value=[]):
        with pytest.raises(ValueError, match="Duplicate YouTube upload"):
            record_private_upload("80dd5f5a-b131-49ef-b644-6d528449bcb2", "abcdefghijk")


def test_update_stage_requires_valid_uuid_and_allowed_stage():
    with pytest.raises(ValueError):
        record_result("not-a-uuid", stage="GENERATING")
    with pytest.raises(ValueError, match="stage not accepted"):
        record_result("80dd5f5a-b131-49ef-b644-6d528449bcb2", stage="PUBLISHED")


def test_mutating_claim_fails_closed_when_rights_or_source_is_not_ready():
    with (
        patch("scripts.video_autopilot_queue.psql") as db,
        patch(
            "scripts.video_autopilot_queue.json_one", return_value=sample_candidate()
        ),
    ):
        result = candidate(dry_run=False)
    assert result == {"has_work": False, "reason": "commercial_rights_not_cleared"}
    db.assert_called_once()


def test_upload_sweep_is_idle_without_qa_approved_release():
    from scripts.video_autopilot_queue import claim_next_private_upload

    with patch("scripts.video_autopilot_queue.psql", return_value=[]):
        result = claim_next_private_upload()
    assert result["has_work"] is False


def test_private_upload_reservation_is_single_atomic_transition_from_review():
    from scripts.video_autopilot_queue import claim_private_upload

    clip = "80dd5f5a-b131-49ef-b644-6d528449bcb2"
    approved = dict(
        release_ready=True,
        visibility="private",
        claimed_for_upload=False,
        download_url="https://scene-media.aitoolpool.com/v1/media/projects/verified/scenes/movie.mp4?exp=123&sig=abc",
        youtube_title="Beat",
        youtube_description="Verified",
        target_channel_id="UCl9yINCZs9M2qItUXAnJwVg",
    )
    with (
        patch("scripts.video_autopilot_queue.release_check", return_value=approved),
        patch("scripts.video_autopilot_queue.psql", return_value=[clip]) as db,
    ):
        result = claim_private_upload(clip)
    assert result["claimed"] is True
    assert result["visibility"] == "private"
    assert "stage='QA_REVIEW'" in db.call_args.args[0]


def test_private_upload_reservation_rejected_after_claim():
    from scripts.video_autopilot_queue import claim_private_upload

    clip = "80dd5f5a-b131-49ef-b644-6d528449bcb2"
    with (
        patch(
            "scripts.video_autopilot_queue.release_check",
            return_value={
                "release_ready": True,
                "visibility": "private",
                "claimed_for_upload": True,
            },
        ),
        patch("scripts.video_autopilot_queue.psql") as db,
    ):
        result = claim_private_upload(clip)
    assert result["claimed"] is False
    db.assert_not_called()


def test_private_upload_reservation_cannot_race_duplicate_request():
    from scripts.video_autopilot_queue import claim_private_upload

    clip = "80dd5f5a-b131-49ef-b644-6d528449bcb2"
    with (
        patch(
            "scripts.video_autopilot_queue.release_check",
            return_value={
                "release_ready": True,
                "visibility": "private",
                "claimed_for_upload": False,
            },
        ),
        patch("scripts.video_autopilot_queue.psql", return_value=[]),
    ):
        result = claim_private_upload(clip)
    assert result["claimed"] is False
    assert result["reason"] == "already_reserved"
