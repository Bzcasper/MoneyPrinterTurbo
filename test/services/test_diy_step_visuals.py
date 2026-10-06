import math
import subprocess
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image

from app.config import config
from app.models.schema import VideoAspect, VideoConcatMode, VideoParams
from app.services import material, task, task_artifacts, video, voice
from app.utils import utils
from test.services.test_webui_content_vertical import (
    _GroupedSelectHarness, _running_app, _select_vertical,
)


def diy_params(**changes):
    values = dict(
        video_subject="Build a shelf", content_vertical="diy", video_source="firefly",
        video_aspect="16:9", subtitle_enabled=False, bgm_type="", paragraph_number=8,
    )
    values.update(changes)
    return VideoParams(**values)


def test_paragraphs_preserve_single_line_breaks_and_ignore_blank_lines():
    assert voice.split_script_paragraphs("  Hook.\nSame step.\n\n\n材料。\n \nDone.  ") == [
        "Hook.\nSame step.", "材料。", "Done.",
    ]
    assert voice.split_script_paragraphs("") == []


def test_paragraph_holds_follow_edge_cues_including_gaps_and_trailing_audio():
    cues = [
        SimpleNamespace(content="Cut wood.", start=timedelta(seconds=0.5), end=timedelta(seconds=7)),
        SimpleNamespace(content="Glue it.", start=timedelta(seconds=8.5), end=timedelta(seconds=17)),
        SimpleNamespace(content="Done.", start=timedelta(seconds=18), end=timedelta(seconds=20)),
    ]
    assert voice.script_paragraph_durations(
        "Cut wood.\n\nGlue it.\n\nDone.", 21, SimpleNamespace(cues=cues)
    ) == [8.5, 9.5, 3.0]


def test_legacy_cue_crossing_a_paragraph_boundary_is_interpolated():
    sub = SimpleNamespace(subs=["ＡＢ。CD。"], offset=[(10000000, 90000000)])
    assert voice.script_paragraph_durations("AB.\n\nCD.", 10, sub) == [5, 5]


@pytest.mark.parametrize("sub", [
    None,
    SimpleNamespace(subs=["Different text"], offset=[(0, 80000000)]),
    SimpleNamespace(subs=["AAAA", "BB"], offset=[(0, math.inf), (30000000, 90000000)]),
    SimpleNamespace(subs=["AAAA", "BB"], offset=[(0, 70000000), (60000000, 90000000)]),
    SimpleNamespace(cues=[SimpleNamespace(content="AAAABB", start=0, end=10)]),
])
def test_missing_or_invalid_boundaries_fall_back_to_text_proportions(sub):
    assert voice.script_paragraph_durations("AAAA.\n\nBB.", 18, sub) == [12, 6]


@pytest.mark.parametrize("duration", [0, -1, math.nan, math.inf])
def test_invalid_paragraph_duration_is_rejected(duration):
    with pytest.raises(ValueError, match="positive duration"):
        voice.script_paragraph_durations("Step.", duration)


def test_diy_scene_count_uses_actual_paragraphs_and_skips_reranking():
    params = diy_params()
    script = "Finished shelf.\n\nCut wood.\n\nUse it."
    with (
        patch.object(task.llm, "generate_terms", return_value=["one", "two", "three"]) as generate,
        patch.object(task.twelvelabs, "rerank_terms_by_subject") as rerank,
    ):
        assert task.generate_terms("diy", params, script) == ["one", "two", "three"]
    assert generate.call_args.kwargs["amount"] == 3
    assert generate.call_args.kwargs["match_script_order"] is True
    assert params.firefly_scene_prompts is True
    rerank.assert_not_called()
    assert not video.is_diy_step_mode(diy_params(video_source="pexels"))
    assert not video.is_diy_step_mode(diy_params(content_vertical="type_beat"))


def test_manual_scene_count_mismatch_stops_before_audio_or_paid_images():
    params = diy_params(video_script="Cut wood.\n\nGlue it.", video_terms=["one scene"])
    with (
        patch.dict(config.app, firefly_webhook_url="http://firefly.test/webhook"),
        patch.object(utils, "check_ffmpeg_ready", return_value=True),
        patch.object(task.sm.state, "update_task"),
        patch.object(task, "generate_audio") as audio,
        patch.object(task, "get_video_materials") as materials,
    ):
        result = task._run_pipeline("diy", params)
    assert result["failed_stage"] == "terms"
    assert "one scene prompt per script paragraph" in result["error"]
    audio.assert_not_called()
    materials.assert_not_called()


def test_pipeline_saves_and_forwards_full_paragraph_holds(tmp_path):
    params = diy_params(video_script="AAAA.\n\nBB.", video_terms=["one", "two"])
    with (
        patch.dict(config.app, firefly_webhook_url="http://firefly.test/webhook"),
        patch.object(utils, "task_dir", return_value=str(tmp_path)),
        patch.object(utils, "check_ffmpeg_ready", return_value=True),
        patch.object(task.sm.state, "update_task"),
        patch.object(task, "generate_audio", return_value=("voice.wav", 18, None)),
        patch.object(task, "generate_subtitle", return_value=""),
        patch.object(task, "get_video_materials", return_value=["one.mp4", "two.mp4"]) as materials,
        patch.object(task, "generate_final_videos", return_value=(["final.mp4"], ["combined.mp4"], [])) as final,
        patch.object(task.upload_post.upload_post_service, "is_configured", return_value=False),
    ):
        result = task._run_pipeline("diy", params)
        saved = task_artifacts.read_script_data("diy")
    assert result["videos"] == ["final.mp4"]
    assert saved["diy_step_durations"] == [12, 6]
    assert materials.call_args.kwargs["step_durations"] == [12, 6]
    assert final.call_args.kwargs["step_durations"] == [12, 6]


def test_material_stage_does_not_buy_extra_images_for_multiple_outputs():
    params = diy_params(video_count=2, firefly_scene_prompts=True)
    with patch.object(material, "download_videos", return_value=["one.mp4", "two.mp4"]) as download:
        assert task.get_video_materials("diy", params, ["one", "two"], 18, step_durations=[12, 6])
    assert download.call_args.kwargs["audio_duration"] == 18
    assert download.call_args.kwargs["firefly_clip_durations"] == [12, 6]
    assert download.call_args.kwargs["video_concat_mode"] == VideoConcatMode.sequential


@pytest.mark.parametrize("concurrency", [1, 3])
def test_every_paragraph_is_generated_with_its_hold_and_camera_index(tmp_path, concurrency):
    holds = [7.25, 9.5, 2.75]
    complete = {index: threading.Event() for index in range(3)}

    def generate(search_term, **kwargs):
        index = int(search_term)
        assert kwargs["minimum_duration"] == math.ceil(holds[index])
        return [material.MaterialInfo(
            provider="firefly", url=str(tmp_path / f"{index}.png"), duration=20,
            source_info={"provider": "firefly", "search_term": search_term},
        )]

    def render(filename, duration, *, clip_index):
        assert duration == holds[clip_index]
        if concurrency > 1 and clip_index < 2:
            assert complete[clip_index + 1].wait(5)
        complete[clip_index].set()
        return filename.replace(".png", ".mp4")

    with (
        patch.dict(config.app, firefly_concurrency=concurrency),
        patch.object(task_artifacts, "read_firefly_image_manifest", return_value={}),
        patch.object(material, "generate_images_firefly", side_effect=generate) as generate_mock,
        patch.object(material, "_render_openai_image_video", side_effect=render),
        patch.object(material, "_persist_material_sources") as persist,
    ):
        paths = material._download_videos_firefly_on_demand(
            task_id="diy", search_terms=["0", "1", "2"], video_aspect=VideoAspect.landscape,
            audio_duration=sum(holds), max_clip_duration=5, material_directory=str(tmp_path),
            scene_prompts=True, clip_durations=holds,
        )
    assert paths == [str(tmp_path / f"{index}.mp4") for index in range(3)]
    assert generate_mock.call_count == 3
    assert all(call.kwargs["scene_prompt"] is True for call in generate_mock.call_args_list)
    assert [record["search_term"] for record in persist.call_args.args[1]] == ["0", "1", "2"]


def test_missing_paragraph_stops_queued_images_and_preserves_completed_sources(tmp_path):
    first = material.MaterialInfo(
        provider="firefly", url=str(tmp_path / "first.png"), duration=8,
        source_info={"provider": "firefly", "search_term": "first"},
    )
    with (
        patch.dict(config.app, firefly_concurrency=1),
        patch.object(task_artifacts, "read_firefly_image_manifest", return_value={}),
        patch.object(material, "generate_images_firefly", side_effect=[[first], [], [first]]) as generate,
        patch.object(material, "_render_openai_image_video", return_value="first.mp4"),
        patch.object(material, "_persist_material_sources") as persist,
    ):
        with pytest.raises(material.OpenAIImagePaidResultError, match="paragraph image is missing"):
            material._download_videos_firefly_on_demand(
                task_id="diy", search_terms=["first", "missing", "unused"],
                video_aspect=VideoAspect.landscape, audio_duration=24, max_clip_duration=5,
                material_directory=str(tmp_path), clip_durations=[8, 8, 8],
            )
    assert generate.call_count == 2
    assert persist.call_args.args[1][0]["search_term"] == "first"


@pytest.mark.parametrize("holds", [[8], [8, 0], [8, math.nan]])
def test_invalid_hold_plan_is_rejected_before_generation(tmp_path, holds):
    with (
        patch.object(task_artifacts, "read_firefly_image_manifest", return_value={}),
        patch.object(material, "generate_images_firefly") as generate,
    ):
        with pytest.raises(ValueError, match="positive hold"):
            material._download_videos_firefly_on_demand(
                task_id="diy", search_terms=["one", "two"], video_aspect=VideoAspect.landscape,
                audio_duration=16, max_clip_duration=5, material_directory=str(tmp_path),
                clip_durations=holds,
            )
    generate.assert_not_called()


def test_final_composition_uses_full_holds_in_order_for_each_output(tmp_path):
    with (
        patch.object(utils, "task_dir", return_value=str(tmp_path)),
        patch.object(task.sm.state, "update_task"),
        patch.object(video, "combine_videos") as combine,
        patch.object(video, "generate_video", return_value=True),
    ):
        final, _, _ = task.generate_final_videos(
            "diy", diy_params(video_count=2), ["one.mp4", "two.mp4"], "voice.wav", "", 18,
            step_durations=[12, 6],
        )
    assert len(final) == combine.call_count == 2
    for call in combine.call_args_list:
        assert call.kwargs["cut_times"] == [0, 12, 18]
        assert call.kwargs["target_duration"] == 18
        assert call.kwargs["strict_cut_order"] is True
        assert call.kwargs["video_concat_mode"] == VideoConcatMode.sequential


@pytest.mark.parametrize("index", range(8))
def test_camera_move_cycles_and_remains_bounded_for_long_fractional_holds(tmp_path, index):
    image = tmp_path / "step.png"
    Image.new("RGB", (128, 96), "blue").save(image)
    with patch.object(video.subprocess, "run") as run:
        result = video._render_image_zoom_video_ffmpeg(str(image), 90.25, clip_index=index)
    command = run.call_args.args[0]
    filters = command[command.index("-vf") + 1]
    move = ("pan-left", "pan-right", "tilt", "zoom-out")[index % 4]
    assert result.endswith(f"zoom-90.250000-{move}.mp4")
    assert command[command.index("-t") + 1] == "90.250000"
    assert "1.12" in filters
    assert "on/2707" in filters if index % 4 < 3 else "max(1,1.12-" in filters


def test_diy_render_failure_keeps_previous_output_without_alternate_camera(tmp_path):
    image = tmp_path / "step.png"
    Image.new("RGB", (128, 96), "blue").save(image)
    previous = tmp_path / "step.png.zoom-8.250000-pan-left.mp4"
    previous.write_bytes(b"previous complete clip")
    with (
        patch.object(video.subprocess, "run", side_effect=RuntimeError("encode failed")),
        patch.object(video, "_render_image_zoom_video_moviepy") as fallback,
    ):
        with pytest.raises(RuntimeError, match="encode failed"):
            video.render_image_zoom_video(str(image), 8.25, clip_index=0)
    assert previous.read_bytes() == b"previous complete clip"
    assert image.exists()
    assert not list(tmp_path.glob(".image-zoom-*"))
    fallback.assert_not_called()


def test_real_camera_variants_move_in_expected_directions(tmp_path):
    pixels = np.zeros((96, 128, 3), dtype=np.uint8)
    pixels[:, :, 0] = np.linspace(0, 255, 128, dtype=np.uint8)
    pixels[:, :, 1] = np.linspace(0, 255, 96, dtype=np.uint8)[:, None]
    pixels[:, :, 2] = 20
    image = tmp_path / "gradient.png"
    Image.fromarray(pixels).save(image)
    frames = []
    for index in range(4):
        filename = video.render_image_zoom_video(str(image), 1.25, clip_index=index)
        with video.VideoFileClip(filename) as clip:
            assert clip.duration == pytest.approx(1.25, abs=1 / video.fps)
            frames.append((clip.get_frame(0).astype(float), clip.get_frame(1.2).astype(float)))
    assert frames[0][1][48, 64, 0] < frames[0][0][48, 64, 0] - 5
    assert frames[1][1][48, 64, 0] > frames[1][0][48, 64, 0] + 5
    assert frames[2][1][48, 64, 1] > frames[2][0][48, 64, 1] + 5
    assert frames[3][1][48, 4, 0] < frames[3][0][48, 4, 0] - 5


def test_real_composition_keeps_long_paragraphs_and_cleans_failed_step(tmp_path):
    sources = []
    holds = [6.2, 8.5, 2.3]
    for color, hold in zip(("blue", "red", "green"), holds):
        filename = tmp_path / f"{color}.mp4"
        subprocess.run([
            utils.get_ffmpeg_binary(), "-y", "-f", "lavfi", "-i",
            f"color=c={color}:s=96x64:r=30:d={hold}", "-an", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-threads", "1", str(filename),
        ], capture_output=True, check=True, timeout=30)
        sources.append(str(filename))
    audio = tmp_path / "voice.wav"
    assert voice.generate_silent_audio(17, str(audio))
    output = tmp_path / "combined.mp4"
    options = dict(
        video_aspect=VideoAspect.landscape, video_concat_mode=VideoConcatMode.sequential,
        max_clip_duration=5, target_duration=17, cut_times=[0, 6.2, 14.7, 17],
        strict_cut_order=True, threads=1,
    )
    with (
        patch.object(VideoAspect, "to_resolution", return_value=(96, 64)),
        patch.object(video, "_get_configured_video_codec", return_value="libx264"),
        patch.dict(config.app, video_clip_concurrency=1),
    ):
        video.combine_videos(str(output), sources, str(audio), **options)
        with video.VideoFileClip(str(output)) as clip:
            assert clip.duration == pytest.approx(17, abs=1 / video.fps)
            assert int(np.argmax(clip.get_frame(6.1)[32, 48])) == 2
            assert int(np.argmax(clip.get_frame(6.3)[32, 48])) == 0
            assert int(np.argmax(clip.get_frame(14.6)[32, 48])) == 0
            assert int(np.argmax(clip.get_frame(14.8)[32, 48])) == 1
        before = output.read_bytes()
        with (
            patch.object(video, "_write_videofile_with_codec_fallback", side_effect=RuntimeError("encode failed")),
            patch.object(video, "concat_video_clips_with_ffmpeg") as concat,
        ):
            with pytest.raises(RuntimeError, match="every DIY paragraph"):
                video.combine_videos(str(output), sources, str(audio), **options)
        concat.assert_not_called()
    assert output.read_bytes() == before
    assert not list(tmp_path.glob("temp-clip-*"))
    assert all(Path(source).exists() for source in sources)


def test_unreadable_diy_step_is_not_replaced_by_another_paragraph(tmp_path):
    with (
        patch.object(video, "AudioFileClip", return_value=SimpleNamespace(duration=16)),
        patch.object(video, "close_clip"),
        patch.object(video, "_open_video_clip_quietly", side_effect=ValueError("bad clip")),
        patch.object(video, "concat_video_clips_with_ffmpeg") as concat,
    ):
        with pytest.raises(RuntimeError, match="paragraph video is unreadable"):
            video.combine_videos(
                str(tmp_path / "combined.mp4"), ["one.mp4", "two.mp4"], "voice.wav",
                video_concat_mode=VideoConcatMode.sequential, target_duration=16,
                cut_times=[0, 8, 16], strict_cut_order=True,
            )
    concat.assert_not_called()


def test_webui_scene_button_counts_actual_paragraphs_and_explains_holds():
    with _running_app(_GroupedSelectHarness()) as app:
        _select_vertical(app, "diy")
        app.session_state["video_subject"] = "Build a shelf"
        app.session_state["video_script"] = "Finished shelf.\n\nCut wood.\n\nUse it."
        app.run()
        with patch.object(task.llm, "generate_terms", return_value=["one", "two", "three"]) as generate:
            next(item for item in app.button if item.key == "auto_generate_terms").click().run()
        assert not list(app.exception)
        assert generate.call_args.kwargs["amount"] == 3
        assert any("One image per script paragraph" in item.value for item in app.caption)
