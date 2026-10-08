import json

import pytest

from scripts.scene_reference_handoff import (
    audit_video_entry,
    register_scene,
    resolve_previous,
)

VIDEO = "/srv/data/n8n-media/store/firefly/videos/fullmotion-173124-scene-1.mp4"
ID1 = "c15841f5-8402-48c5-ab56-eec95f5e5784"
ID2 = "4ed13dcf-8b75-486d-9233-1a43cc769839"


def scene(slot, source=VIDEO, ref=None):
    return {
        "success": True,
        "slot": slot,
        "prompt": f"Gold prism continues motion {slot}",
        "duration": 5,
        "host_path": source,
        "provider": "firefly",
        "model": "firefly-video",
        "firefly_fair_use": True,
        "credit_spending_allowed": False,
        "reference_image_id": ref,
        "reference_requested": bool(ref),
    }


def fake_frame(path, project, slot, root):
    return {
        "sha256": "a" * 64 if slot == 1 else "b" * 64,
        "bytes": 85445,
        "base64": "dummy-reference-data",
    }


def test_requires_previous_reference_for_all_later_video_scenes(tmp_path):
    p = "story-reference-test"
    with pytest.raises(ValueError, match="Previous scene"):
        resolve_previous(p, 2, "Second shot moves", 5, tmp_path)
    first = register_scene(
        p, scene(1), tmp_path, extract=fake_frame, upload=lambda frame: ID1
    )
    assert first["success"] is True
    assert first["publishing_approved"] is False
    assert first["scene_result"]["next_reference_image_id"] == ID1
    next_step = resolve_previous(p, 2, "Second shot moves", 5, tmp_path)
    assert next_step["reference_image_id"] == ID1
    assert next_step["visual_reference_required"] is True
    with pytest.raises(ValueError, match="omitted the exact previous"):
        register_scene(
            p,
            scene(
                2,
                "/srv/data/n8n-media/store/firefly/videos/fullmotion-173218-scene-2.mp4",
            ),
            tmp_path,
            extract=fake_frame,
            upload=lambda frame: ID2,
        )
    second = register_scene(
        p,
        scene(
            2,
            "/srv/data/n8n-media/store/firefly/videos/fullmotion-173218-scene-2.mp4",
            ID1,
        ),
        tmp_path,
        extract=fake_frame,
        upload=lambda frame: ID2,
        audit=lambda frame, video: {"passed": True, "luma_ssim": 0.93, "rgb_mae": 0.04},
    )
    assert second["scene_result"]["next_reference_image_id"] == ID2
    assert (
        second["scene_result"]["reference_custody"]
        == "adobe_storage_previous_scene_frame"
    )
    assert (
        resolve_previous(p, 3, "Third shot moves", 5, tmp_path)["reference_image_id"]
        == ID2
    )
    report = json.loads((tmp_path / p / "scene-002-reference.json").read_text())
    assert report["input_reference_image_id"] == ID1
    assert report["reference_image_id"] == ID2


def test_idempotent_replay_and_conflict(tmp_path):
    p = "duplicate-check"
    row = scene(1)
    first = register_scene(
        p, row, tmp_path, extract=fake_frame, upload=lambda frame: ID1
    )

    def broken_upload(frame):
        raise AssertionError("Prior published Adobe reference should be reused")

    second = register_scene(p, row, tmp_path, extract=fake_frame, upload=broken_upload)
    assert (
        first["scene_result"]["next_reference_image_id"]
        == second["scene_result"]["next_reference_image_id"]
    )
    row["host_path"] = (
        "/srv/data/n8n-media/store/firefly/videos/fullmotion-173999-scene-1.mp4"
    )
    with pytest.raises(ValueError, match="already committed"):
        register_scene(p, row, tmp_path, extract=fake_frame, upload=broken_upload)


def test_rejects_unapproved_credits_or_invalid_identity(tmp_path):
    row = scene(1)
    row["credit_spending_allowed"] = True
    with pytest.raises(ValueError, match="fair-use"):
        register_scene("story-safe", row, tmp_path)
    with pytest.raises(ValueError, match="Invalid"):
        resolve_previous("../bad", 1, "hello", 5, tmp_path)
    with pytest.raises(ValueError, match="5–10 seconds"):
        resolve_previous("safe", 1, "hello", 2, tmp_path)


def test_actual_video_reference_rejects_disconnected_pilot():
    from pathlib import Path

    ref = Path(
        "/home/bobby/Videos/scene-director/reference-frames/shadowcore-pilot/scene-001-out.jpg"
    )
    if not ref.is_file():
        pytest.skip("reference pilot asset absent")
    data = audit_video_entry(
        ref, "/srv/data/n8n-media/store/firefly/videos/fullmotion-173442-scene-3.mp4"
    )
    assert data["passed"] is False
    assert data["luma_ssim"] < 0.35
