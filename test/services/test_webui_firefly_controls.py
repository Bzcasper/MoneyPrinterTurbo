import ast
import hashlib
import io
import json
import math
import re
import tempfile
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from loguru import logger
from PIL import Image

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoParams
from app.services import bgm as bgm_service
from app.services import material, video, voice
from app.utils import utils
from test.services.test_material_firefly import _download_response, _png_bytes, _webhook_response
from test.services.test_webui_content_vertical import (
    WEBUI_MAIN, _GroupedSelectHarness, _running_app, _select_vertical,
)


@pytest.fixture
def controls():
    """Exercise the real helpers without executing the whole Streamlit entry point."""
    names = {
        "_firefly_test_fingerprint", "_firefly_uploaded_audio_duration",
        "_firefly_generation_estimate", "_estimate_voiceover_duration_range",
    }
    tree = ast.parse(WEBUI_MAIN.read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(functions) == len(names)
    namespace = {
        "hashlib": hashlib, "json": json, "math": math, "re": re,
        "tempfile": tempfile, "Path": Path, "VideoAspect": VideoAspect,
        "material": material, "video": video, "voice": voice, "utils": utils,
        "config": config, "bgm_service": bgm_service,
        "CUSTOM_AUDIO_EXTENSIONS": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"},
        "st": SimpleNamespace(session_state={}),
        "_matching_full_voice_preview_duration": Mock(return_value=None),
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(WEBUI_MAIN), "exec"), namespace)
    with patch.dict(config.app, firefly_concurrency=1, firefly_webhook_token="", firefly_image_model="firefly-image-5"):
        yield namespace


def params(**changes):
    values = dict(video_subject="workbench", video_source="firefly", voice_name=voice.NO_VOICE_NAME)
    values.update(changes)
    return VideoParams(**values)


def upload(seconds=6.25, name="beat.wav"):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * round(seconds * 8000))
    buffer.name = name
    buffer.seek(0)
    return buffer


def test_diy_estimate_counts_actual_paragraphs_and_reuses_images_across_outputs(controls):
    p = params(content_vertical="diy", video_script="Finished shelf.\n\nCut wood.\nMeasure the edge.\n\nUse the shelf.", video_count=5)
    with patch.object(voice, "get_audio_duration") as probe, patch.object(material.requests, "post") as post:
        estimate = controls["_firefly_generation_estimate"](p)
    assert (estimate["image_min"], estimate["image_max"]) == (3, 3)
    assert (estimate["minutes_min"], estimate["minutes_max"]) == (6, 15)
    probe.assert_not_called()
    post.assert_not_called()


def test_missing_diy_script_uses_requested_paragraph_count(controls):
    estimate = controls["_firefly_generation_estimate"](params(content_vertical="diy", paragraph_number=6))
    assert estimate["image_max"] == 6


@pytest.mark.parametrize("terms,scenes,count", [
    ('["hands, wood", "finished, shelf"]', True, 2),
    ("hands, wood\nfinished, shelf", True, 2),
    ("hands,wood，shelf", False, 3),
    (["hands", "", "shelf"], False, 2),
])
def test_estimate_parses_scene_prompts_without_splitting_commas(controls, terms, scenes, count):
    p = params(video_terms=terms, firefly_scene_prompts=scenes, video_script="wood " * 200)
    estimate = controls["_firefly_generation_estimate"](p)
    assert (estimate["image_min"], estimate["image_max"]) == (count, count)


@pytest.mark.parametrize("changes", [
    {"video_terms": "[broken", "firefly_scene_prompts": True},
    {"video_terms": "[1]", "firefly_scene_prompts": True},
    {"video_terms": ",，,"},
    {"content_vertical": "diy", "video_script": "First.\n\nSecond.", "video_terms": ["only one"]},
])
def test_invalid_terms_do_not_produce_a_misleading_estimate(controls, changes):
    assert controls["_firefly_generation_estimate"](params(**changes)) is None


@pytest.mark.parametrize("preview,clip_duration,count,expected", [(7, 5, 2, 3), (4.1, 3, 2, 4)])
def test_estimate_matches_actual_request_count_for_measured_audio_and_multiple_outputs(controls, tmp_path, preview, clip_duration, count, expected):
    controls["_matching_full_voice_preview_duration"].return_value = preview
    p = params(video_script="wood", voice_name="en-US-JennyNeural-Female", video_count=count, video_clip_duration=clip_duration)
    estimate = controls["_firefly_generation_estimate"](p)
    image = MaterialInfo(provider="firefly", url="image.png", duration=clip_duration)
    with (
        patch.object(material, "generate_images_firefly", return_value=[image]) as generate,
        patch.object(material, "_render_openai_image_video", return_value="clip.mp4"),
        patch.object(material, "_persist_material_sources"),
        patch.object(material.task_artifacts, "read_firefly_image_manifest", return_value={}),
    ):
        clips = material._download_videos_firefly_on_demand(
            task_id="estimate", search_terms=["one", "two", "three", "four", "five"],
            video_aspect=VideoAspect.landscape, audio_duration=math.ceil(preview) * count,
            max_clip_duration=clip_duration, material_directory=str(tmp_path),
        )
    assert len(clips) == generate.call_count == estimate["image_min"] == estimate["image_max"] == expected


def test_tts_estimate_is_a_range_and_respects_rate_and_script_order_scene_count(controls):
    p = params(video_script="wood " * 100, voice_name="en-US-JennyNeural-Female", voice_rate=2, match_materials_to_script=True)
    estimate = controls["_firefly_generation_estimate"](p)
    assert 1 <= estimate["image_min"] <= estimate["image_max"] <= 8
    slower = controls["_firefly_generation_estimate"](p.model_copy(update={"voice_rate": 1}))
    assert slower["image_max"] > estimate["image_max"]


def test_real_uploaded_beat_duration_is_probed_without_network_or_persistence(controls):
    uploaded = upload()
    p = params(content_vertical="type_beat", beat_length_mode=True, bgm_type="custom", bgm_file="beat.wav")
    with patch.object(material.requests, "post") as post, patch.object(voice, "get_audio_duration", wraps=voice.get_audio_duration) as probe:
        estimate = controls["_firefly_generation_estimate"](p, uploaded_bgm_file=uploaded)
        repeated = controls["_firefly_generation_estimate"](p, uploaded_bgm_file=uploaded)
    assert estimate == repeated
    assert estimate["image_max"] == 2
    assert probe.call_count == 1
    assert not Path(probe.call_args.args[0]).exists()
    assert uploaded.tell() == 0
    post.assert_not_called()


def test_saved_beat_duration_is_whitelisted_and_scene_count_caps_long_beats(controls):
    p = params(content_vertical="type_beat", beat_length_mode=True, bgm_type="custom", bgm_file="beat.wav")
    with patch.object(bgm_service, "resolve_bgm_file", return_value="safe-beat.wav") as resolve, patch.object(voice, "get_audio_duration", return_value=180):
        estimate = controls["_firefly_generation_estimate"](p)
    resolve.assert_called_once_with("beat.wav")
    assert estimate["image_max"] == 5
    with patch.object(bgm_service, "resolve_bgm_file", side_effect=ValueError("outside whitelist")), patch.object(voice, "get_audio_duration") as probe:
        controls["_firefly_generation_estimate"](p)
    probe.assert_not_called()


def test_custom_voice_upload_takes_precedence_over_script_and_preview(controls):
    estimate = controls["_firefly_generation_estimate"](params(video_script="wood " * 200), uploaded_audio_file=upload())
    assert estimate["image_max"] == 2
    controls["_matching_full_voice_preview_duration"].assert_not_called()


@pytest.mark.parametrize("name,limit", [("bad.exe", 1000000), ("beat.wav", 1)])
def test_invalid_or_oversized_upload_is_not_probed(controls, name, limit):
    with patch.object(bgm_service, "MAX_BGM_UPLOAD_BYTES", limit), patch.object(voice, "get_audio_duration") as probe:
        assert controls["_firefly_uploaded_audio_duration"](upload(name=name)) is None
    probe.assert_not_called()


def test_upload_duration_cache_keys_content_instead_of_name_and_size(controls):
    first = upload(seconds=1)
    second = upload(seconds=1)
    second.seek(-2, io.SEEK_END)
    second.write(b"\x01\x00")
    with patch.object(voice, "get_audio_duration", side_effect=[1, 2]) as probe:
        assert controls["_firefly_uploaded_audio_duration"](first) == 1
        assert controls["_firefly_uploaded_audio_duration"](second) == 2
    assert probe.call_count == 2


@pytest.mark.parametrize("concurrency,expected", [(0, 1), (4, 4), (99, 4), ("bad", 1)])
def test_time_estimate_uses_clamped_concurrency_and_local_successful_test(controls, concurrency, expected):
    p = params(content_vertical="diy", paragraph_number=5)
    controls["st"].session_state["firefly_test_image"] = {"fingerprint": controls["_firefly_test_fingerprint"](p), "seconds": 80}
    with patch.dict(config.app, firefly_concurrency=concurrency):
        estimate = controls["_firefly_generation_estimate"](p)
    assert estimate["concurrency"] == expected
    assert estimate["basis_key"] == "Firefly Estimate Measured"
    assert estimate["minutes_min"] == math.ceil(5 / expected)
    assert estimate["minutes_max"] == math.ceil(5 / expected) * 2


@pytest.mark.parametrize("changes", [
    {"firefly_image_model": "gpt-image-1"},
    {"firefly_webhook_url": "https://different.example/webhook"},
    {"firefly_webhook_token": "new-test-token"},
])
def test_changed_provider_does_not_reuse_old_test_timing(controls, changes):
    p = params()
    controls["st"].session_state["firefly_test_image"] = {"fingerprint": controls["_firefly_test_fingerprint"](p), "seconds": 1}
    with patch.dict(config.app, changes):
        assert controls["_firefly_generation_estimate"](p)["basis_key"] == "Firefly Estimate Assumed"
    assert controls["_firefly_generation_estimate"](p.model_copy(update={"video_aspect": VideoAspect.landscape}))["basis_key"] == "Firefly Estimate Assumed"


def test_other_material_sources_have_no_firefly_estimate(controls):
    with patch.object(material.requests, "post") as post:
        assert controls["_firefly_generation_estimate"](params(video_source="pexels")) is None
    post.assert_not_called()


@pytest.mark.parametrize("changes,expected", [
    ({"video_terms": ["a", "b", "c"], "video_clip_duration": 3}, (2, 3)),
    ({"video_terms": ["a", "b", "c"], "video_clip_duration": 10}, (3, 3)),
    ({"content_vertical": "diy", "video_script": "Result.\n\nBuild.\n\nUse.", "video_clip_duration": 10}, (2, 3)),
    ({"content_vertical": "diy", "video_script": "Build."}, (0, 1)),
])
def test_hero_estimate_includes_possible_filler_without_requesting_a_video(controls, changes, expected):
    with patch.object(material.requests, "post") as post, patch.object(material.requests, "get") as get:
        estimate = controls["_firefly_generation_estimate"](params(firefly_hero_shot=True, **changes))
    assert (estimate["image_min"], estimate["image_max"]) == expected
    post.assert_not_called()
    get.assert_not_called()


def test_hero_setting_is_optional_persists_and_does_not_generate_on_rerun():
    response = _webhook_response({"success": True, "image_url": "https://cdn.example/test.png"})
    with patch.object(material.requests, "post", return_value=response) as post, patch.object(material.requests, "get", return_value=_download_response(_png_bytes())) as get:
        with _running_app(_GroupedSelectHarness()) as app:
            _select_vertical(app, "diy")
            checkbox = app.checkbox(key="firefly_hero_shot_input")
            assert checkbox.value is False
            checkbox.check().run()
            assert not list(app.exception)
            assert config.ui["firefly_hero_shot"] is True
            assert any("one native video request" in item.value for item in app.caption)
            app.run()
            assert app.checkbox(key="firefly_hero_shot_input").value is True
            post.assert_not_called()
            assert not any("cdn.example" in call.args[0] for call in get.call_args_list)
            app.button(key="firefly_test_image_button").click().run()
            assert post.call_args.kwargs["json"]["media_type"] == "image"
            assert post.call_count == 1
    assert all(call.kwargs["json"]["media_type"] != "video" for call in post.call_args_list)


def test_webui_test_button_sends_one_tiny_scene_request_and_reruns_without_requests():
    response = _webhook_response({"success": True, "image_url": "https://cdn.example/test.png"})
    with patch.object(material.requests, "post", return_value=response) as post, patch.object(material.requests, "get", return_value=_download_response(_png_bytes())):
        with _running_app(_GroupedSelectHarness()) as app:
            post.assert_not_called()
            _select_vertical(app, "diy")
            post.assert_not_called()
            app.button(key="firefly_test_image_button").click().run()
            assert not list(app.exception)
            assert post.call_count == 1
            payload = post.call_args.kwargs["json"]
            assert payload["prompt"] == "A single wooden block on a plain workbench, soft daylight, no text or logos."
            assert payload["media_type"] == "image"
            assert (payload["width"], payload["height"]) == (1920, 1080)
            preview = app.session_state["firefly_test_image"]
            assert Image.open(io.BytesIO(preview["image"])).size == (64, 96)
            assert preview["seconds"] > 0
            assert any("last image test" in item.value for item in app.caption)
            app.run()
            assert not list(app.exception)
            assert post.call_count == 1
            assert app.session_state["firefly_test_image"]["image"] == preview["image"]


def test_webui_disables_test_without_webhook_and_does_not_send_requests():
    with patch.object(material.requests, "post") as post:
        with _running_app(_GroupedSelectHarness()) as app:
            config.app["firefly_webhook_url"] = ""
            app.run()
            assert not list(app.exception)
            assert app.button(key="firefly_test_image_button").disabled
            app.button(key="firefly_test_image_button").click().run()
            post.assert_not_called()


def test_webui_test_failure_is_generic_and_cleans_temporary_files():
    directories = []
    def failed(*args, **kwargs):
        directories.append(Path(kwargs["save_dir"]))
        raise OSError("fake-credential-sentinel")
    messages = []
    handler = logger.add(messages.append, format="{message}")
    try:
        with patch.object(material, "generate_images_firefly", side_effect=failed):
            with _running_app(_GroupedSelectHarness()) as app:
                app.button(key="firefly_test_image_button").click().run()
                assert not list(app.exception)
                assert any("Image test failed" in item.value for item in app.error)
                assert all("fake-credential-sentinel" not in item.value for item in app.error)
        assert len(directories) == 1
        assert all(not directory.exists() for directory in directories)
        assert "fake-credential-sentinel" not in "".join(messages)
    finally:
        logger.remove(handler)
