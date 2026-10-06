import json
import math
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
import requests
from loguru import logger

from app.config import config
from app.models.schema import MaterialInfo, VideoAspect, VideoConcatMode, VideoParams
from app.services import material, task, task_artifacts, video
from app.utils import utils
from test.services.test_material_firefly import _download_response, _png_bytes, _webhook_response


@pytest.fixture(scope="module")
def native_bytes(tmp_path_factory):
    filename = tmp_path_factory.mktemp("native-firefly") / "hero.mp4"
    subprocess.run([
        utils.get_ffmpeg_binary(), "-y", "-f", "lavfi", "-i",
        "color=c=blue:s=96x64:r=30:d=5.1", "-an", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "-threads", "1", str(filename),
    ], capture_output=True, check=True, timeout=30)
    return filename.read_bytes()


@pytest.fixture
def provider(tmp_path, native_bytes):
    def task_dir(task_id, **_kwargs):
        directory = tmp_path / task_id
        directory.mkdir(parents=True, exist_ok=True)
        return str(directory)

    response = _webhook_response({"success": True, "video_url": "https://cdn.example/hero.mp4?signature=PRIVATE-SENTINEL"})
    with (
        patch.object(config, "app", {"firefly_webhook_url": "https://hooks.example/generate", "firefly_concurrency": 1}),
        patch.object(config, "proxy", {}),
        patch.object(utils, "task_dir", side_effect=task_dir),
        patch.object(material.requests, "post", return_value=response) as post,
        patch.object(material.requests, "get", return_value=_download_response(native_bytes)) as get,
    ):
        yield SimpleNamespace(post=post, get=get, root=tmp_path, native_bytes=native_bytes)


@pytest.mark.parametrize("aspect,size", [(VideoAspect.landscape, (1920, 1080)), (VideoAspect.portrait, (1080, 1920)), (VideoAspect.square, (1080, 1080))])
@pytest.mark.parametrize("token", ["", "PRIVATE-SENTINEL"])
def test_hero_uses_existing_webhook_contract_and_auth_only_on_post(provider, aspect, size, token):
    config.app["firefly_webhook_token"] = token
    messages = []
    sink = logger.add(lambda message: messages.append(str(message)))
    try:
        item = material._generate_firefly_hero("task-one", "Hands, tools, no text.", aspect, scene_prompt=True)
    finally:
        logger.remove(sink)
    assert item.duration == pytest.approx(5.1, abs=1 / 30)
    assert Path(item.url).is_file()
    assert provider.post.call_count == provider.get.call_count == 1
    options = provider.post.call_args.kwargs
    assert options["json"] == {
        "prompt": "Hands, tools, no text.", "media_type": "video", "model": "firefly-video",
        "width": size[0], "height": size[1], "duration": 5,
    }
    assert options["timeout"] == material.FIREFLY_REQUEST_TIMEOUT
    assert options.get("headers") == ({"Authorization": f"Bearer {token}"} if token else None)
    assert "headers" not in provider.get.call_args.kwargs
    manifest = (Path(item.url).parent / "firefly-videos.json").read_text()
    assert "PRIVATE-SENTINEL" not in manifest + "".join(messages)
    assert "cdn.example" not in manifest + "".join(messages)


def test_native_video_cache_is_per_task_and_image_writes_do_not_erase_it(provider):
    first = material._generate_firefly_hero("task-one", "wood", VideoAspect.landscape)
    task_artifacts.record_firefly_image("task-one", "a" * 64, str(provider.root / "task-one" / "image.png"))
    retry = material._generate_firefly_hero("task-one", "wood", VideoAspect.landscape)
    assert retry.url == first.url
    assert provider.post.call_count == provider.get.call_count == 1
    second = material._generate_firefly_hero("task-two", "wood", VideoAspect.landscape)
    assert second.url != first.url
    assert provider.post.call_count == provider.get.call_count == 2
    assert len(task_artifacts.read_firefly_video_manifest("task-one")) == 1
    assert len(task_artifacts.read_firefly_image_manifest("task-one")) == 1


@pytest.mark.parametrize("damage", ["delete", "corrupt", "truncate"])
def test_missing_or_corrupt_cached_video_never_buys_a_replacement(provider, damage):
    first = material._generate_firefly_hero("task-one", "wood", VideoAspect.landscape)
    path = Path(first.url)
    if damage == "delete":
        path.unlink()
    else:
        path.write_bytes(b"not a video" if damage == "corrupt" else provider.native_bytes[:1800])
    with pytest.raises(material.OpenAIImagePaidResultError):
        material._generate_firefly_hero("task-one", "wood", VideoAspect.landscape)
    assert provider.post.call_count == provider.get.call_count == 1


@pytest.mark.parametrize("payload,status", [
    ({"success": False, "error_message": "PRIVATE-SENTINEL"}, 200),
    ({"success": True}, 200),
    ({"success": True, "video_url": "ftp://cdn.example/hero.mp4"}, 200),
    ({"success": True, "video_url": "https://user:PRIVATE-SENTINEL@cdn.example/hero.mp4"}, 200),
    ({"success": True, "video_url": "https://cdn.example/hero.mp4"}, 500),
    ([], 200),
])
def test_unconfirmed_video_response_is_not_repeated_on_retry(provider, payload, status):
    provider.post.return_value = _webhook_response(payload, status)
    with pytest.raises(material.OpenAIImageUnconfirmedError) as error:
        material._generate_firefly_hero("task-one", "wood", VideoAspect.landscape)
    assert "PRIVATE-SENTINEL" not in str(error.value)
    with pytest.raises(material.OpenAIImageUnconfirmedError, match="already requested"):
        material._generate_firefly_hero("task-one", "wood", VideoAspect.landscape)
    provider.post.assert_called_once()
    provider.get.assert_not_called()


def test_transport_failure_redacts_details_and_prevents_automatic_resubmission(provider):
    config.app["firefly_webhook_token"] = "PRIVATE-SENTINEL"
    provider.post.side_effect = requests.Timeout("https://hooks.example/generate?token=PRIVATE-SENTINEL")
    with pytest.raises(material.OpenAIImageUnconfirmedError) as error:
        material._generate_firefly_hero("task-one", "wood", VideoAspect.landscape)
    assert "PRIVATE-SENTINEL" not in str(error.value)
    assert error.value.__suppress_context__ is True
    with pytest.raises(material.OpenAIImageUnconfirmedError):
        material._generate_firefly_hero("task-one", "wood", VideoAspect.landscape)
    provider.post.assert_called_once()


@pytest.mark.parametrize("failure", ["http", "declared-size", "stream-size", "invalid-video", "image", "transport"])
def test_paid_download_failure_is_bounded_and_does_not_resubmit(provider, failure):
    response = _download_response(provider.native_bytes)
    if failure == "http":
        response.status_code = 403
    elif failure == "declared-size":
        response.headers["Content-Length"] = str(material.FIREFLY_VIDEO_MAX_BYTES + 1)
    elif failure == "stream-size":
        response.headers.clear()
    elif failure in {"invalid-video", "image"}:
        response = _download_response(b"not a video" if failure == "invalid-video" else _png_bytes())
    else:
        provider.get.side_effect = requests.Timeout("PRIVATE-SENTINEL")
    provider.get.return_value = response
    with patch.object(material, "FIREFLY_VIDEO_MAX_BYTES", len(provider.native_bytes) - 1 if failure == "stream-size" else material.FIREFLY_VIDEO_MAX_BYTES):
        with pytest.raises(material.OpenAIImagePaidResultError) as error:
            material._generate_firefly_hero("task-one", "wood", VideoAspect.landscape)
    assert "PRIVATE-SENTINEL" not in str(error.value)
    assert not list((provider.root / "task-one").glob("*.mp4"))
    with pytest.raises(material.OpenAIImageUnconfirmedError):
        material._generate_firefly_hero("task-one", "wood", VideoAspect.landscape)
    provider.post.assert_called_once()
    provider.get.assert_called_once()


def test_valid_download_is_retained_if_final_rename_fails(provider):
    original_replace = material.os.replace
    def fail_video_replace(source, target):
        if str(target).endswith(".mp4"):
            raise OSError("disk unavailable")
        return original_replace(source, target)
    with patch.object(material.os, "replace", side_effect=fail_video_replace):
        with pytest.raises(material.OpenAIImagePaidResultError):
            material._generate_firefly_hero("task-one", "wood", VideoAspect.landscape)
    recovery = list((provider.root / "task-one").glob(".*.mp4"))
    assert len(recovery) == 1
    assert material._validate_firefly_video_file(str(recovery[0])) > 5
    provider.post.assert_called_once()


@pytest.mark.parametrize("manifest", ["{", '{"task_id":"another-task","videos":{}}', '{"task_id":"task-one","videos":{"broken":null}}'])
def test_invalid_request_manifest_stops_before_a_paid_request(provider, manifest):
    directory = Path(utils.task_dir("task-one"))
    (directory / "firefly-videos.json").write_text(manifest)
    with pytest.raises(material.OpenAIImagePaidResultError, match="manifest"):
        material._generate_firefly_hero("task-one", "wood", VideoAspect.landscape)
    provider.post.assert_not_called()
    provider.get.assert_not_called()


def test_request_reservation_failure_stops_before_post(provider):
    with patch.object(task_artifacts, "_write_json_atomic", side_effect=OSError("disk unavailable")):
        with pytest.raises(material.OpenAIImagePaidResultError, match="no new video request"):
            material._generate_firefly_hero("task-one", "wood", VideoAspect.landscape)
    provider.post.assert_not_called()


def test_concurrent_request_reservations_claim_only_once(provider):
    key = "a" * 64
    path = str(provider.root / "task-one" / "hero.mp4")
    with ThreadPoolExecutor(max_workers=4) as executor:
        claims = list(executor.map(lambda _: task_artifacts.reserve_firefly_video("task-one", key, path), range(8)))
    assert sum(fresh for _, fresh in claims) == 1
    assert {value for value, _ in claims} == {path}


def test_native_cache_cannot_use_another_tasks_saved_video(provider):
    material._generate_firefly_hero("task-one", "wood", VideoAspect.landscape)
    other = material._generate_firefly_hero("task-two", "wood", VideoAspect.landscape)
    manifest_path = provider.root / "task-one" / "firefly-videos.json"
    payload = json.loads(manifest_path.read_text())
    key = next(iter(payload["videos"]))
    payload["videos"][key] = other.url
    manifest_path.write_text(json.dumps(payload))
    with pytest.raises(material.OpenAIImagePaidResultError, match="does not belong"):
        material._generate_firefly_hero("task-one", "wood", VideoAspect.landscape)
    assert provider.post.call_count == 2


@pytest.mark.parametrize("enabled", [False, True])
def test_public_download_passes_hero_option_only_when_enabled(provider, enabled):
    with patch.object(material, "_download_videos_firefly_on_demand", return_value=["first.mp4"]) as download:
        assert material.download_videos("task-one", ["first"], source="firefly", audio_duration=5,
                                        firefly_hero_shot=enabled) == ["first.mp4"]
    assert download.call_args.kwargs.get("hero_shot", False) is enabled
    if not enabled:
        assert "hero_shot" not in download.call_args.kwargs


def test_invalid_paragraph_hold_plan_is_rejected_before_buying_the_hero(provider):
    with patch.object(material, "_generate_firefly_hero") as generate:
        with pytest.raises(ValueError, match="positive hold"):
            material._download_videos_firefly_on_demand(
                task_id="task-one", search_terms=["first", "second"], video_aspect=VideoAspect.landscape,
                audio_duration=10, max_clip_duration=5, material_directory=str(provider.root),
                clip_durations=[5, 0], hero_shot=True,
            )
    generate.assert_not_called()
    provider.post.assert_not_called()


def test_failed_filler_composition_reuses_native_video_and_png_on_retry(provider):
    def response(_url, **options):
        payload = options["json"]
        return _webhook_response({"success": True, "video_url": "https://cdn.example/hero.mp4"} if payload["media_type"] == "video" else
                                 {"success": True, "image_url": "https://cdn.example/image.png"})
    provider.post.side_effect = response
    provider.get.side_effect = lambda url, **_: _download_response(provider.native_bytes if url.endswith(".mp4") else _png_bytes())
    options = dict(task_id="task-one", search_terms=["first", "second"], video_aspect=VideoAspect.landscape,
                   audio_duration=16, max_clip_duration=3, material_directory=utils.task_dir("task-one"),
                   scene_prompts=True, clip_durations=[8, 8], hero_shot=True)
    with (
        patch.object(material, "_render_openai_image_video", side_effect=lambda path, *_args, **_kwargs: path + ".mp4"),
        patch.object(material, "_fill_firefly_hero", side_effect=material.OpenAIImagePaidResultError("local render failed")),
    ):
        with pytest.raises(material.OpenAIImagePaidResultError):
            material._download_videos_firefly_on_demand(**options)
    assert provider.post.call_count == 2
    assert len(task_artifacts.read_firefly_video_manifest("task-one")) == 1
    assert len(task_artifacts.read_firefly_image_manifest("task-one")) == 1
    assert all(Path(path).is_file() for path in task_artifacts.read_firefly_image_manifest("task-one").values())
    with (
        patch.object(material, "_render_openai_image_video", side_effect=lambda path, *_args, **_kwargs: path + ".mp4"),
        patch.object(material, "_fill_firefly_hero", return_value=str(provider.root / "filled.mp4")),
    ):
        assert len(material._download_videos_firefly_on_demand(**options)) == 2
    assert [call.kwargs["json"]["media_type"] for call in provider.post.call_args_list] == ["video", "image", "image"]


def hero_item(tmp_path, duration=5.1):
    item = MaterialInfo(provider="firefly", url=str(tmp_path / "hero.mp4"), duration=int(duration),
                        source_info={"provider": "firefly", "search_term": "first", "rendition": {"id": "firefly-video"}})
    item.duration = duration
    return item


@pytest.mark.parametrize("concurrency", [1, 4])
@pytest.mark.parametrize("holds,required,max_clip,expected_terms,expected_paths", [
    (None, 9, 3, ["second", "third"], ["hero.mp4", "second.mp4", "third.mp4"]),
    (None, 3, 10, [], ["hero.mp4"]),
    (None, 15, 10, ["first", "second"], ["filled.mp4", "second.mp4"]),
    ([2, 9, 7], 18, 3, ["second", "third"], ["hero.mp4", "second.mp4", "third.mp4"]),
    ([8.25, 6], 14.25, 3, ["first", "second"], ["filled.mp4", "second.mp4"]),
])
def test_first_native_scene_and_zoom_fillers_keep_order_and_holds(provider, concurrency, holds, required, max_clip, expected_terms, expected_paths):
    terms = ["first", "second", "third"][:len(holds)] if holds is not None else ["first", "second", "third"]
    hero = hero_item(provider.root)
    def generate(search_term, minimum_duration, **_kwargs):
        return [MaterialInfo(provider="firefly", url=str(provider.root / f"{search_term}.png"), duration=minimum_duration,
                             source_info={"provider": "firefly", "search_term": search_term})]
    def render(path, duration, **_kwargs):
        return str(Path(path).with_suffix(".mp4"))
    with (
        patch.dict(config.app, firefly_concurrency=concurrency),
        patch.object(material, "_generate_firefly_hero", return_value=hero) as native,
        patch.object(material, "generate_images_firefly", side_effect=generate) as images,
        patch.object(material, "_render_openai_image_video", side_effect=render) as rendering,
        patch.object(material, "_fill_firefly_hero", return_value=str(provider.root / "filled.mp4")) as filling,
        patch.object(material, "_persist_material_sources") as persist,
    ):
        paths = material._download_videos_firefly_on_demand(
            task_id="task-one", search_terms=terms, video_aspect=VideoAspect.landscape,
            audio_duration=required, max_clip_duration=max_clip, material_directory=str(provider.root),
            scene_prompts=True, clip_durations=holds, hero_shot=True,
        )
    assert [Path(path).name for path in paths] == expected_paths
    assert sorted(call.kwargs["search_term"] for call in images.call_args_list) == sorted(expected_terms)
    native.assert_called_once_with("task-one", "first", VideoAspect.landscape, scene_prompt=True)
    assert persist.call_args.args[1][0]["rendition"]["id"] == "firefly-video"
    if expected_paths[0] == "filled.mp4":
        hold = holds[0] if holds is not None else max_clip
        hero_hold = math.floor(hero.duration * 30) / 30
        filling.assert_called_once_with(hero, str(provider.root / "first.mp4"), hold, hero_hold, VideoAspect.landscape)
        first = next(call for call in rendering.call_args_list if call.args[0].endswith("first.png"))
        assert first.args[1] == pytest.approx(hold - hero_hold)
        assert first.kwargs["clip_index"] == 0
    else:
        filling.assert_not_called()


def test_hero_failure_stops_before_buying_images(provider):
    with patch.object(material, "_generate_firefly_hero", side_effect=material.OpenAIImageUnconfirmedError("unconfirmed")), patch.object(material, "generate_images_firefly") as images:
        with pytest.raises(material.OpenAIImageUnconfirmedError):
            material._download_videos_firefly_on_demand(
                task_id="task-one", search_terms=["first", "second"], video_aspect=VideoAspect.landscape,
                audio_duration=12, max_clip_duration=5, material_directory=str(provider.root), hero_shot=True,
            )
    images.assert_not_called()


def test_task_passes_opt_in_and_keeps_the_hero_first():
    params = VideoParams(video_subject="wood", video_source="firefly", firefly_hero_shot=True)
    with patch.object(material, "download_videos", return_value=["hero.mp4", "filler.mp4"]) as download:
        assert task.get_video_materials("task-one", params, ["first", "second"], 10) == ["hero.mp4", "filler.mp4"]
    assert download.call_args.kwargs["firefly_hero_shot"] is True
    assert download.call_args.kwargs["video_concat_mode"] == VideoConcatMode.sequential
    params.firefly_hero_shot = False
    with patch.object(material, "download_videos", return_value=["first.mp4"]) as download:
        task.get_video_materials("task-one", params, ["first"], 5)
    assert "firefly_hero_shot" not in download.call_args.kwargs
    assert task._materials_follow_script(params) is False


def test_fractional_scene_holds_quantize_global_boundaries_without_accumulating_drift():
    source = video.SubClippedVideoClip(file_path="hero.mp4", start_time=0, end_time=20,
                                      width=96, height=64, duration=20, source_file_path="hero.mp4")
    cuts = [0, 8.25, 10.35, 12.47, 15.61]
    plan = video._plan_timed_clips([source], cuts, 1)
    cumulative = 0
    for index, item in enumerate(plan):
        assert item.duration * video.fps == pytest.approx(round(item.duration * video.fps))
        cumulative += item.duration
        assert cumulative == pytest.approx(cuts[index + 1], abs=1 / video.fps)
    assert round(cumulative * video.fps) == math.ceil(cuts[-1] * video.fps)


def test_real_native_and_zoom_filler_preserve_sources_and_paragraph_timing(provider):
    def response(_url, **options):
        payload = options["json"]
        return _webhook_response({"success": True, "video_url": "https://cdn.example/hero.mp4"} if payload["media_type"] == "video" else
                                 {"success": True, "image_url": f"https://cdn.example/{payload['prompt']}.png"})
    def download(url, **_kwargs):
        content = provider.native_bytes if url.endswith(".mp4") else _png_bytes(96, 64, (0, 255, 0) if "first" in url else (255, 0, 0))
        return _download_response(content)
    provider.post.side_effect, provider.get.side_effect = response, download
    task_artifacts.write_script_data("task-one", {})
    with (
        patch.object(VideoAspect, "to_resolution", return_value=(96, 64)),
        patch.object(video, "_get_configured_video_codec", return_value="libx264"),
        patch.object(video, "AudioFileClip", side_effect=AssertionError("explicit silent composition must not probe audio")),
        patch.dict(config.app, video_clip_concurrency=1),
    ):
        paths = material._download_videos_firefly_on_demand(
            task_id="task-one", search_terms=["first", "second"], video_aspect=VideoAspect.landscape,
            audio_duration=10.35, max_clip_duration=3, material_directory=utils.task_dir("task-one"),
            scene_prompts=True, clip_durations=[8.25, 2.1], hero_shot=True,
        )
        assert len(paths) == 2
        output = str(provider.root / "task-one" / "output.mp4")
        video.combine_videos(output, paths, "", video_aspect=VideoAspect.landscape,
                             video_concat_mode=VideoConcatMode.sequential, target_duration=10.35,
                             cut_times=[0, 8.25, 10.35], strict_cut_order=True, threads=1)
        with video.VideoFileClip(output) as clip:
            assert clip.duration == pytest.approx(10.35, abs=1 / 30)
            assert int(np.argmax(clip.get_frame(1)[32, 48])) == 2
            assert int(np.argmax(clip.get_frame(5.4)[32, 48])) == 1
            assert int(np.argmax(clip.get_frame(8.6)[32, 48])) == 0
    native_path = next(iter(task_artifacts.read_firefly_video_manifest("task-one").values()))
    assert Path(native_path).read_bytes() == provider.native_bytes
    assert len(task_artifacts.read_firefly_image_manifest("task-one")) == 2
    assert all(Path(path).is_file() for path in task_artifacts.read_firefly_image_manifest("task-one").values())
    assert [call.kwargs["json"]["media_type"] for call in provider.post.call_args_list] == ["video", "image", "image"]
    sources = task_artifacts.read_script_data("task-one")["material_sources"]
    assert "PRIVATE-SENTINEL" not in json.dumps(sources)
