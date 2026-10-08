from unittest.mock import patch

import pytest

from scripts.assemble_full_motion_project import validate_payload, assemble

CLIP = "d512fc9b-5246-4488-a0fd-e5110247f1e9"
TITLE = "VAMPMIN-VAR-003-R04-06954"


def sample(n=30):
    return {
        "project_id": "fullmotion-test",
        "clip_id": CLIP,
        "title": TITLE,
        "bpm": 150,
        "scene_count": n,
        "provider_policy": "adobe_unlimited_video_with_previous_frame_reference",
        "scene_video_assets": [
            {
                "ordinal": i,
                "host_path": f"/srv/data/n8n-media/store/firefly/videos/fullmotion-{i}.mp4",
                "provider": "firefly",
                "model": "firefly-video",
                "firefly_fair_use": True,
                "prompt": f"Scene {i}: physically continuous cinematic motion",
                "reference_image_id": f"canon-{i - 1:03d}" if i > 1 else None,
                "next_reference_image_id": f"canon-{i:03d}",
                "reference_frame_sha256": "a" * 64,
                "reference_custody": "adobe_storage_previous_scene_frame",
                "reference_similarity": None
                if i == 1
                else {"passed": True, "luma_ssim": 0.94, "rgb_mae": 0.04},
            }
            for i in range(1, n + 1)
        ],
        "director_treatment": {
            "thesis": "A suspended sun pulse crosses an abandoned orbital city",
            "motif": "golden rotating signal",
            "display_title": "Solar Current",
            "title_motion": "swoop",
            "scenes": [
                {
                    "master_beat": min(10, (i - 1) * 10 // n + 1),
                    "act": "MOVEMENT",
                    "location": f"Glass station {i}",
                    "camera": "35mm advancing tracking",
                    "action": f"Gold signal advances through station {i}",
                    "opening_state": "Light arrives"
                    if i == 1
                    else f"Signal exits station {i - 1}",
                    "end_state": f"Signal exits station {i}",
                    "transition": "Follow the gold signal",
                }
                for i in range(1, n + 1)
            ],
        },
    }


@pytest.mark.parametrize("n", [30, 33, 50])
def test_accepts_all_unique_video_scenes(n):
    pid, clip, count, bpm, assets = validate_payload(sample(n))
    assert (pid, clip, count, bpm) == ("fullmotion-test", CLIP, n, 150)
    assert len(assets) == n


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda a: a["scene_video_assets"].pop(),
        lambda a: a["scene_video_assets"][5].update({"ordinal": 1}),
        lambda a: a["scene_video_assets"][0].update(
            {"provider": "moneyprinter-image-motion"}
        ),
        lambda a: a["scene_video_assets"][0].update({"firefly_fair_use": False}),
        lambda a: a["scene_video_assets"][0].update({"host_path": "/tmp/scene.mp4"}),
        lambda a: a["scene_video_assets"][0].update(
            {"host_path": "/srv/data/n8n-media/store/firefly/videos/fullmotion-2.mp4"}
        ),
    ],
)
def test_invalid_or_non_free_assets_fail_closed(corrupt):
    v = sample()
    corrupt(v)
    with pytest.raises(ValueError):
        validate_payload(v)


def test_rejects_non_wav_library_beat_before_writing():
    with patch(
        "scripts.assemble_full_motion_project.psql_rows",
        side_effect=[[], [[TITLE, "t", "91.88", "/music/audio.mp3", "source_mp3"]]],
    ):
        with pytest.raises(ValueError, match="original instrumental WAV"):
            assemble(sample())


def test_rejects_incorrect_track_identity_before_writing():
    with patch(
        "scripts.assemble_full_motion_project.psql_rows",
        side_effect=[
            [],
            [["Other Beat", "t", "91.88", "/music/audio.wav", "wav_only"]],
        ],
    ):
        with pytest.raises(ValueError, match="title must match"):
            assemble(sample())


def test_rejects_corrupt_or_short_generated_mp4():
    import json
    import subprocess
    from scripts.assemble_full_motion_project import probe_generated_motion

    sample = {
        "streams": [{"codec_name": "h264", "width": 1280, "height": 720}],
        "format": {"duration": "0.8"},
    }
    with patch(
        "scripts.assemble_full_motion_project.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, json.dumps(sample), ""),
    ):
        with pytest.raises(ValueError, match="codec/resolution/duration"):
            probe_generated_motion("/srv/data/n8n-media/store/firefly/videos/short.mp4")


def test_accepts_valid_generated_video_probe():
    import json
    import subprocess
    from scripts.assemble_full_motion_project import probe_generated_motion

    sample = {
        "streams": [{"codec_name": "h264", "width": 1280, "height": 720}],
        "format": {"duration": "5.041667"},
    }
    with patch(
        "scripts.assemble_full_motion_project.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, json.dumps(sample), ""),
    ):
        assert (
            probe_generated_motion("/srv/data/n8n-media/store/firefly/videos/good.mp4")[
                "codec"
            ]
            == "h264"
        )


def test_custom_story_missing_or_broken_handoff_rejected():
    invalid = sample()
    invalid.pop("director_treatment")
    with pytest.raises(ValueError, match="unique director_treatment"):
        validate_payload(invalid)
    invalid = sample()
    invalid["director_treatment"]["scenes"][1]["opening_state"] = (
        "Reset to unrelated place"
    )
    with pytest.raises(ValueError, match="handoff mismatch"):
        validate_payload(invalid)


def test_assembled_sql_preserves_ten_master_beat_references():
    from unittest.mock import patch

    data = sample(30)
    library = [[TITLE, "t", "91.8935", "/media/songs/source.wav", "wav_only"]]
    with (
        patch(
            "scripts.assemble_full_motion_project.psql_rows", side_effect=[[], library]
        ),
        patch(
            "scripts.assemble_full_motion_project.remote_file_exists", return_value=True
        ),
        patch(
            "scripts.assemble_full_motion_project.probe_generated_motion",
            return_value={"duration": 5.0},
        ),
        patch("scripts.assemble_full_motion_project.psql_exec") as save,
    ):
        outcome = assemble(data)
    written = save.call_args.args[0]
    assert outcome["video_scene_count"] == 30
    assert "handoff_from,handoff_to,continuity_state" in written
    assert "story_contract_version" in written
    assert "hero_handoff" in written
    assert "Solar Current" in written
    assert "final_frame_sha256" in written
    assert "duration_seconds,width,height" in written
    assert "source_duration_sec" in written
    assert "canon-001" in written
    assert "adobe_storage_previous_scene_frame" in written


def test_unchained_video_reference_fails_at_database_boundary():
    incorrect = sample()
    incorrect["scene_video_assets"][8]["reference_image_id"] = "unrelated-adobe-asset"
    with pytest.raises(ValueError, match="not chained"):
        validate_payload(incorrect)
    missing = sample()
    del missing["scene_video_assets"][8]["next_reference_image_id"]
    with pytest.raises(ValueError, match="durable Adobe frame custody"):
        validate_payload(missing)
    old = sample()
    old.pop("provider_policy")
    with pytest.raises(ValueError, match="requires previous-scene"):
        validate_payload(old)
