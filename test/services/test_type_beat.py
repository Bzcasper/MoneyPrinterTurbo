import math
import subprocess
import sys
import wave
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest

from app.models.schema import VideoAspect, VideoConcatMode, VideoParams
from app.services import task, task_artifacts, video, voice
from app.utils import utils


def beat_params(**changes):
    values = dict(
        video_subject="Noir instrumental", content_vertical="type_beat",
        beat_length_mode=True, voice_name=voice.NO_VOICE_NAME,
        video_aspect="16:9", video_concat_mode="sequential",
        subtitle_enabled=False, bgm_type="custom", bgm_file="beat.wav",
        bgm_volume=0.5,
    )
    values.update(changes)
    return VideoParams(**values)


@pytest.mark.parametrize("voice_name", ["no-voice", "none"])
def test_beat_audio_uses_actual_duration_and_skips_tts(voice_name):
    params = beat_params(voice_name=voice_name)
    with (
        patch.object(task.bgm_service, "resolve_bgm_file", return_value="/beat.wav"),
        patch.object(voice, "get_audio_duration", return_value=127.25),
        patch.object(voice, "tts") as tts,
    ):
        assert task.generate_audio("beat", params, "Short script") == ("/beat.wav", 127.25, None)
    tts.assert_not_called()


def test_beat_mode_is_opt_in_and_requires_no_voice():
    assert not video.is_beat_length_mode(beat_params(beat_length_mode=False))
    assert not video.is_beat_length_mode(beat_params(content_vertical="diy"))
    assert not video.is_beat_length_mode(beat_params(voice_name="en-US-JennyNeural"))
    assert not video.is_beat_length_mode(SimpleNamespace())


@pytest.mark.parametrize("changes", [{"bgm_file": ""}, {"bgm_type": "random"}])
def test_missing_beat_fails_preflight_before_paid_work(changes):
    with (
        patch.object(task.sm.state, "update_task"),
        patch.object(utils, "check_ffmpeg_ready", return_value=True),
        patch.object(task, "generate_script") as script,
        patch.object(task, "_mark_task_failed", return_value={"failed_stage": "preflight"}) as failed,
    ):
        result = task._run_pipeline("missing-beat", beat_params(video_source="local", **changes))
    assert result["failed_stage"] == "preflight"
    assert "custom beat file" in failed.call_args.args[2]
    script.assert_not_called()


@pytest.mark.parametrize("duration", [0, float("nan"), float("inf")])
def test_invalid_beat_duration_is_rejected(duration):
    with (
        patch.object(task.bgm_service, "resolve_bgm_file", return_value="/beat.wav"),
        patch.object(voice, "get_audio_duration", return_value=duration),
        pytest.raises(ValueError, match="duration"),
    ):
        task._resolve_type_beat_audio(beat_params())


def test_cut_selection_uses_events_and_fills_quiet_gaps():
    cuts = video._beat_cut_times([0.5, 1.0, 2.4, 3.0, 5.0, 7.4, float("nan")], 11.7, 5)
    assert cuts == pytest.approx([0, 5, 7.4, 11.7])
    assert video._beat_cut_times([], 11.7, 5) == pytest.approx([0, 5, 10, 11.7])


def test_analysis_falls_back_to_onsets_and_preserves_explicit_bpm():
    detector = SimpleNamespace(
        load=Mock(return_value=(np.zeros(10), 22050)),
        onset=SimpleNamespace(
            onset_strength=Mock(return_value=np.ones(5)),
            onset_detect=Mock(return_value=np.array([2.5, 5.0, 7.5])),
        ),
        beat=SimpleNamespace(beat_track=Mock(return_value=(np.array([119.0]), np.array([])))),
    )
    with patch.dict(sys.modules, {"librosa": detector}):
        result = video.analyze_beat("beat.wav", 8.0, 5.0, 120.0)
    assert result == {"bpm": 120.0, "cut_times": [0, 5.0, 8.0]}
    assert detector.beat.beat_track.call_args.kwargs["bpm"] == 120.0
    assert detector.beat.beat_track.call_args.kwargs["units"] == "time"
    assert detector.onset.onset_detect.call_args.kwargs["units"] == "time"


def test_analysis_failure_uses_uniform_cuts():
    detector = SimpleNamespace(load=Mock(side_effect=ValueError("invalid audio")))
    with patch.dict(sys.modules, {"librosa": detector}):
        assert video.analyze_beat("broken.wav", 12.3, 5, 90) == {
            "bpm": 90.0, "cut_times": [0, 5.0, 10.0, 12.3],
        }


def write_wave(filename, samples, sample_rate=22050):
    with wave.open(str(filename), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes((np.asarray(samples) * 16000).astype("<i2").tobytes())


def test_real_click_track_detects_tempo_and_beat_aligned_cuts(tmp_path):
    import librosa

    filename = tmp_path / "clicks.wav"
    write_wave(filename, librosa.clicks(times=np.arange(0.5, 8, 0.5), sr=22050, length=8 * 22050))
    result = video.analyze_beat(str(filename), 8.0, 3.0)
    assert result["bpm"] == pytest.approx(120, abs=8)
    assert result["cut_times"][0] == 0
    assert result["cut_times"][-1] == 8
    for cut in result["cut_times"][1:-1]:
        assert min(abs(cut - beat) for beat in np.arange(0.5, 8, 0.5)) < 0.1


def test_timed_clip_plan_cycles_sources_and_accounts_for_playback_speed():
    items = [video.SubClippedVideoClip("one.mp4", 0, 1, 96, 64)]
    planned = video._plan_timed_clips(items, [0, 2, 4.7], 2)
    assert [item.duration for item in planned] == pytest.approx([2, 2.7])
    assert [item.end_time for item in planned] == [1, 1]
    assert [item.source_file_path for item in planned] == ["one.mp4", "one.mp4"]
    with pytest.raises(ValueError, match="increase"):
        video._plan_timed_clips(items, [0, 0], 1)


def test_type_beat_metadata_uses_provided_facts_and_keeps_lease_link():
    params = beat_params(beat_title="Midnight", beat_key="A minor", beat_genre="Trap", beat_lease_url="https://lease.invalid/beat")
    with patch.object(task.llm, "generate_social_metadata", return_value={
        "title": "Midnight | Noir Instrumental", "caption": "A nocturnal trap instrumental.",
        "hashtags": ["#trap", "#instrumental"],
    }) as generate:
        result = task.generate_type_beat_metadata(params, "Wet city streets", 120)
    prompt = generate.call_args.kwargs["video_script"]
    for fact in ("Midnight", "120", "A minor", "Trap", "https://lease.invalid/beat"):
        assert fact in prompt
    assert "do not name artists" in prompt
    assert result["caption"].endswith("Lease: https://lease.invalid/beat")
    assert result["hashtags"] == ["#trap", "#instrumental"]


def test_cross_post_uses_prepared_metadata_without_another_llm_call():
    metadata = {"title": "Beat title", "caption": "Beat description", "hashtags": ["#beat"]}
    with (
        patch.object(task, "_patch_cross_post_state", return_value=True),
        patch.object(task.llm, "generate_social_metadata") as generate,
        patch.object(task.upload_post, "cross_post_video", return_value={"success": True}) as publish,
    ):
        task._run_cross_post("beat", ("final.mp4",), "subject", "script", "en", ("youtube",), "private", publishing_metadata=metadata)
    generate.assert_not_called()
    extra = publish.call_args.kwargs["youtube_extra"]
    assert extra["youtube_title"] == "Beat title"
    assert extra["youtube_description"] == "Beat description"
    assert extra["tags"] == ["#beat"]
    assert extra["privacyStatus"] == "private"


def test_schedule_copies_prepared_metadata_before_background_execution():
    metadata = {"title": "Original", "caption": "Caption", "hashtags": ["#beat"]}
    with (
        patch.object(task, "_cross_post_slots") as slots,
        patch.object(task, "_cross_post_executor") as executor,
        patch.object(task, "_register_cross_post_future"),
    ):
        slots.acquire.return_value = True
        executor.submit.return_value = Future()
        assert task._schedule_cross_post("beat", ["video.mp4"], beat_params(), "script", ["youtube"], "private", publishing_metadata=metadata) is None
    metadata["hashtags"].append("#changed")
    assert executor.submit.call_args.kwargs["publishing_metadata"]["hashtags"] == ["#beat"]


def test_cross_post_worker_forwards_metadata_and_releases_slot():
    with patch.object(task, "_run_cross_post") as run, patch.object(task, "_cross_post_slots") as slots:
        task._run_cross_post_with_slot("beat", publishing_metadata={"title": "Beat"})
    run.assert_called_once_with("beat", publishing_metadata={"title": "Beat"})
    slots.release.assert_called_once()


def test_task_script_reader_returns_saved_metadata_and_handles_missing_or_corrupt(tmp_path):
    with patch.object(utils, "task_dir", return_value=str(tmp_path)):
        assert task_artifacts.read_script_data("beat") == {}
        task_artifacts.write_script_data("beat", {"publishing_metadata": {"title": "Beat"}})
        assert task_artifacts.read_script_data("beat")["publishing_metadata"]["title"] == "Beat"
        (tmp_path / "script.json").write_text("{broken")
        assert task_artifacts.read_script_data("beat") == {}


@pytest.mark.parametrize("synced", [True, False])
def test_final_video_uses_beat_duration_once_and_preserves_analysis(tmp_path, synced):
    params = beat_params(video_source="firefly", beat_sync_cuts=synced, beat_bpm=100)
    analysis = {"bpm": 120.0, "cut_times": [0.0, 4.7, 8.4]}
    with (
        patch.object(utils, "task_dir", return_value=str(tmp_path)),
        patch.object(task.sm.state, "update_task"),
        patch.object(video, "analyze_beat", return_value=analysis) as detect,
        patch.object(video, "combine_videos") as combine,
        patch.object(video, "generate_video", return_value=True) as render,
        patch.object(video, "apply_type_beat_visuals") as effects,
    ):
        task_artifacts.write_script_data("beat", {"script": "Wet city streets"})
        final, combined, warnings = task.generate_final_videos(
            "beat", params, ["clip.mp4"], "beat.wav", "", 8.4
        )
        saved = task_artifacts.read_script_data("beat")
    assert combine.call_args.kwargs["target_duration"] == 8.4
    assert render.call_args.kwargs["audio_path"] == "beat.wav"
    assert render.call_args.kwargs["bgm_file_override"] == ""
    if synced:
        detect.assert_called_once_with("beat.wav", 8.4, params.video_clip_duration, 100)
        assert combine.call_args.kwargs["cut_times"] == analysis["cut_times"]
        assert saved["beat_analysis"] == analysis
    else:
        detect.assert_not_called()
        assert "cut_times" not in combine.call_args.kwargs
        assert saved["beat_analysis"] == {"bpm": 100.0}
    effects.assert_called_once_with(final[0], params, 120 if synced else 100)
    assert final == [str(tmp_path / "final-1.mp4")]
    assert combined == [str(tmp_path / "combined-1.mp4")]
    assert warnings == []


def test_pipeline_returns_saves_and_schedules_prepared_beat_metadata(tmp_path):
    params = beat_params(video_source="local", beat_title="Midnight", beat_key="A minor")
    metadata = {"title": "Midnight", "caption": "Noir beat", "hashtags": ["#beat"]}

    def finish(*args):
        task_artifacts.patch_script_data("beat", beat_analysis={"bpm": 120.0})
        return ["final.mp4"], ["combined.mp4"], []

    account = SimpleNamespace(
        is_configured=lambda: True, auto_upload=True, platforms=["youtube"],
        youtube_privacy_status="private", youtube_made_for_kids=False,
    )
    with (
        patch.object(utils, "task_dir", return_value=str(tmp_path)),
        patch.object(utils, "check_ffmpeg_ready", return_value=True),
        patch.object(task.sm.state, "update_task") as update,
        patch.object(task, "generate_script", return_value="Wet city streets"),
        patch.object(task.bgm_service, "resolve_bgm_file", return_value="beat.wav"),
        patch.object(voice, "get_audio_duration", return_value=8.4),
        patch.object(voice, "tts") as tts,
        patch.object(task, "generate_subtitle", return_value=""),
        patch.object(task, "get_video_materials", return_value=["clip.mp4"]) as materials,
        patch.object(task, "generate_final_videos", side_effect=finish),
        patch.object(task.llm, "generate_social_metadata", return_value=metadata) as generate,
        patch.object(task.upload_post, "upload_post_service", account),
        patch.object(task, "_schedule_cross_post", return_value=None) as schedule,
    ):
        result = task._run_pipeline("beat", params)
        saved = task_artifacts.read_script_data("beat")
    tts.assert_not_called()
    assert result["audio_duration"] == 8.4
    assert materials.call_args.args[3] == 8.4
    assert result["publishing_metadata"] == saved["publishing_metadata"] == metadata
    assert "BPM: 120" in generate.call_args.kwargs["video_script"]
    assert schedule.call_args.kwargs["publishing_metadata"] == metadata
    assert schedule.call_args.kwargs["youtube_privacy_status"] == "private"
    update.assert_called_with("beat", state=task.const.TASK_STATE_COMPLETE, progress=100, **result)


def test_effect_failure_preserves_original_and_removes_temporary_files(tmp_path):
    original = tmp_path / "final.mp4"
    original.write_bytes(b"original")
    clip = SimpleNamespace(size=(96, 64), duration=2.0, reader=SimpleNamespace(close=Mock()))
    with (
        patch.object(video, "_open_video_clip_quietly", return_value=clip),
        patch.object(video.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "ffmpeg")),
        pytest.raises(subprocess.CalledProcessError),
    ):
        video.apply_type_beat_visuals(str(original), beat_params(), 120)
    assert original.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [original]
    clip.reader.close.assert_called_once()


def test_disabled_effects_and_cards_do_not_reencode():
    with patch.object(video, "_open_video_clip_quietly") as open_clip:
        video.apply_type_beat_visuals("unused.mp4", beat_params(beat_visual_effects=False, beat_overlay_cards=False))
    open_clip.assert_not_called()


@pytest.mark.skipif(not utils.check_ffmpeg_ready(), reason="FFmpeg unavailable")
def test_real_render_keeps_beat_length_cuts_gain_and_audio_with_effects(tmp_path):
    beat = tmp_path / "beat.wav"
    duration = 6.2
    samples = 0.5 * np.sin(2 * math.pi * 440 * np.arange(round(duration * 22050)) / 22050)
    write_wave(beat, samples)
    sources = []
    for color in ("blue", "red", "green"):
        filename = tmp_path / f"{color}.mp4"
        subprocess.run([
            utils.get_ffmpeg_binary(), "-y", "-f", "lavfi", "-i",
            f"color=c={color}:s=96x64:r=30:d=1", "-an", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-threads", "1", str(filename),
        ], capture_output=True, check=True, timeout=30)
        sources.append(str(filename))
    combined = tmp_path / "combined.mp4"
    final = tmp_path / "final.mp4"
    params = beat_params(
        bgm_file=str(beat), voice_volume=0, beat_overlay_cards=True,
        beat_key="A minor", beat_lease_url="https://lease.invalid/beat", n_threads=1,
    )
    with (
        patch.object(VideoAspect, "to_resolution", return_value=(96, 64)),
        patch.object(video, "_get_configured_video_codec", return_value="libx264"),
    ):
        video.combine_videos(
            str(combined), sources, str(beat), video_aspect=VideoAspect.landscape,
            video_concat_mode=VideoConcatMode.sequential, target_duration=duration,
            cut_times=[0, 2.0, 4.7, duration], threads=1,
        )
        with video.VideoFileClip(str(combined)) as clip:
            assert clip.duration == pytest.approx(duration, abs=1 / video.fps)
            assert int(np.argmax(clip.get_frame(1.9)[32, 48])) == 2
            assert int(np.argmax(clip.get_frame(2.1)[32, 48])) == 0
            assert int(np.argmax(clip.get_frame(4.6)[32, 48])) == 0
            assert int(np.argmax(clip.get_frame(4.8)[32, 48])) == 1
        assert video.generate_video(str(combined), str(beat), "", str(final), params, bgm_file_override="")
        video.apply_type_beat_visuals(str(final), params, 120)
    with video.VideoFileClip(str(final)) as clip:
        assert clip.duration == pytest.approx(duration, abs=0.08)
        assert clip.audio is not None
        rms = float(np.sqrt(np.mean(clip.audio.to_soundarray(fps=22050) ** 2)))
        assert 0.06 < rms < 0.1
    assert not list(tmp_path.glob("beat-effects-*"))
