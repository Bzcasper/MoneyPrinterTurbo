import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.controllers.v1 import video as video_controller


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "render_typebeat_project.py"
SPEC = importlib.util.spec_from_file_location("render_typebeat_project", SCRIPT)
renderer = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(renderer)


def _touch(path: Path, data: bytes = b"fixture") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


def test_load_project_preserves_one_mixed_asset_per_scene(tmp_path):
    audio = _touch(tmp_path / "beat.wav")
    image = _touch(tmp_path / "scene-1.png")
    motion = _touch(tmp_path / "scene-2.mp4")
    rows = [
        [["clip-1", "Beat", audio, "decoded_wav"]],
        [
            ["1", "0", "2", "", "", "image", image, "", ""],
            ["2", "2", "5", "video", motion, "image", image, "", ""],
        ],
    ]
    with patch.object(renderer, "psql_rows", side_effect=rows):
        project = renderer.load_project("project-1")
    assert project["cuts"] == [0.0, 2.0, 5.0]
    assert [scene["asset_type"] for scene in project["scenes"]] == ["image", "video"]
    assert [scene["asset_role"] for scene in project["scenes"]] == ["anchor", "locked_video"]
    assert project["assets"] == [image, motion]


def test_load_project_rejects_scene_without_local_asset(tmp_path):
    audio = _touch(tmp_path / "beat.wav")
    rows = [
        [["clip-1", "Beat", audio, "decoded_wav"]],
        [["1", "0", "2", "video", str(tmp_path / "missing.mp4"), "", "", "", ""]],
    ]
    with patch.object(renderer, "psql_rows", side_effect=rows), pytest.raises(
        SystemExit, match="no local canonical asset"
    ):
        renderer.load_project("project-1")


def test_manifest_rejects_non_contiguous_timeline(tmp_path):
    audio = _touch(tmp_path / "beat.wav")
    image = _touch(tmp_path / "scene.png")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        """{
          "project_id": "fixture",
          "audio_path": "%s",
          "scenes": [
            {"ordinal": 1, "start_seconds": 0, "end_seconds": 2, "asset_type": "image", "asset_path": "%s"},
            {"ordinal": 2, "start_seconds": 3, "end_seconds": 5, "asset_type": "image", "asset_path": "%s"}
          ]
        }""" % (audio, image, image),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="not contiguous"):
        renderer.load_manifest(str(manifest))


def test_materialize_scene_sources_converts_only_images(tmp_path):
    image = _touch(tmp_path / "scene.png")
    motion = _touch(tmp_path / "motion.mp4")
    project = {
        "scenes": [
            {"ordinal": 1, "start_seconds": 0.0, "end_seconds": 2.0, "asset_type": "image", "asset_path": image},
            {"ordinal": 2, "start_seconds": 2.0, "end_seconds": 5.0, "asset_type": "video", "asset_path": motion},
        ]
    }
    rendered = str(tmp_path / "scene-001-image-motion.mp4")
    with patch.object(renderer, "_render_image_motion", return_value=rendered) as image_motion:
        sources = renderer.materialize_scene_sources(project, tmp_path)
    assert sources == [rendered, motion]
    image_motion.assert_called_once_with(
        image, tmp_path / "scene-001-image-motion.mp4", 2.0, 1, threads=4
    )


def test_render_project_enforces_scene_order_and_preserves_full_beat_level(tmp_path):
    audio = _touch(tmp_path / "beat.wav")
    image = _touch(tmp_path / "scene.png")
    motion = _touch(tmp_path / "motion.mp4")
    output = tmp_path / "final.mp4"
    project = {
        "project_id": "fixture",
        "source_clip_id": "clip",
        "title": "Fixture",
        "audio_path": audio,
        "source_kind": "decoded_wav",
        "cuts": [0.0, 2.0, 5.0],
        "assets": [image, motion],
        "scenes": [
            {"ordinal": 1, "start_seconds": 0.0, "end_seconds": 2.0, "asset_type": "image", "asset_path": image},
            {"ordinal": 2, "start_seconds": 2.0, "end_seconds": 5.0, "asset_type": "video", "asset_path": motion},
        ],
    }

    def fake_generate(_combined, _audio, _subtitle, filename, params, **_kwargs):
        Path(filename).write_bytes(b"video")
        assert params.bgm_volume == 1.0
        assert params.voice_volume == 0
        return True

    with (
        patch.object(renderer, "normalize_audio", return_value=audio),
        patch.object(renderer, "materialize_scene_sources", return_value=["image-motion.mp4", motion]),
        patch.object(renderer.video, "validate_beat_cut_times", return_value=[0.0, 2.0, 5.0]),
        patch.object(renderer.video, "combine_videos") as combine,
        patch.object(renderer.video, "generate_video", side_effect=fake_generate),
    ):
        result = renderer.render_project(project, output, threads=2, render_mode="legacy")

    assert result["render_mode"] == "legacy"
    assert result["image_scene_count"] == 1
    assert result["video_scene_count"] == 1
    assert result["scene_count"] == 2
    assert combine.call_args.kwargs["video_paths"] == ["image-motion.mp4", motion]
    assert combine.call_args.kwargs["cut_times"] == [0.0, 2.0, 5.0]
    assert combine.call_args.kwargs["strict_cut_order"] is True


def test_fast_render_normalizes_once_then_stream_copies_concat_and_mux(tmp_path):
    audio = _touch(tmp_path / "beat.wav")
    image = _touch(tmp_path / "scene.png")
    motion = _touch(tmp_path / "motion.mp4")
    output = tmp_path / "final.mp4"
    project = {
        "project_id": "fixture-fast",
        "source_clip_id": "clip",
        "title": "Fixture Fast",
        "audio_path": audio,
        "source_kind": "decoded_wav",
        "cuts": [0.0, 2.0, 5.0],
        "assets": [image, motion],
        "scenes": [
            {"ordinal": 1, "start_seconds": 0.0, "end_seconds": 2.0, "asset_type": "image", "asset_path": image},
            {"ordinal": 2, "start_seconds": 2.0, "end_seconds": 5.0, "asset_type": "video", "asset_path": motion},
        ],
    }

    def fake_mux(_combined, _audio, filename, _duration):
        Path(filename).write_bytes(b"fast-video")

    with (
        patch.object(renderer, "normalize_audio", return_value=audio),
        patch.object(
            renderer,
            "materialize_scene_sources",
            return_value=["scene-image.mp4", "scene-video.mp4"],
        ) as materialize,
        patch.object(renderer.video, "validate_beat_cut_times", return_value=[0.0, 2.0, 5.0]),
        patch.object(renderer, "_concat_normalized_scenes") as concat,
        patch.object(renderer, "_mux_master_audio", side_effect=fake_mux) as mux,
        patch.object(renderer.video, "combine_videos") as generic_combine,
        patch.object(renderer.video, "generate_video") as generic_generate,
    ):
        result = renderer.render_project(project, output, threads=3, render_mode="fast")

    assert result["render_mode"] == "fast"
    materialize.assert_called_once_with(
        project, tmp_path / ".fixture-fast-mpt", threads=3, normalize_video=True
    )
    concat.assert_called_once_with(
        ["scene-image.mp4", "scene-video.mp4"],
        tmp_path / ".fixture-fast-mpt" / "combined-fast.mp4",
        5.0,
    )
    mux.assert_called_once()
    generic_combine.assert_not_called()
    generic_generate.assert_not_called()


def test_api_project_render_honors_configurable_output_root(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPEBEAT_OUTPUT_ROOT", str(tmp_path))
    expected = tmp_path / "project-1" / "final" / "moneyprinterturbo-master.mp4"

    def fake_run(command, **_kwargs):
        expected.parent.mkdir(parents=True, exist_ok=True)
        expected.write_bytes(b"video")
        expected.with_suffix(".manifest.json").write_text("{}", encoding="utf-8")
        assert command[-2:] == ["--output", str(expected)]
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    with (
        patch.object(video_controller.subprocess, "run", side_effect=fake_run),
        patch.object(video_controller.sm.state, "patch_task") as patch_task,
    ):
        video_controller._run_type_beat_project_render("task-1", "project-1")

    final = patch_task.call_args_list[-1].kwargs
    assert final["state"] == video_controller.const.TASK_STATE_COMPLETE
    assert final["output_path"] == str(expected)


def test_renderer_cli_bootstraps_repo_root(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
    assert "StrictlyBeats type-beat" in result.stdout


def _shape_project(scene_count: int, motion_count: int):
    return {
        "scenes": [
            {"asset_type": "video" if index < motion_count else "image"}
            for index in range(scene_count)
        ]
    }


def test_production_shape_requires_30_to_50_scenes_and_exactly_10_motion():
    renderer.validate_production_shape(_shape_project(33, 10))
    with pytest.raises(SystemExit, match="30-50 scenes"):
        renderer.validate_production_shape(_shape_project(29, 10))
    with pytest.raises(SystemExit, match="exactly 10 motion scenes"):
        renderer.validate_production_shape(_shape_project(33, 9))
