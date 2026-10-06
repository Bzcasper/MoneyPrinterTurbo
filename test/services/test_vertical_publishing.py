import tomllib
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.config import config
from app.models.schema import VideoParams
from app.services import task, task_artifacts, upload_post, verticals
from app.utils import utils
from test.services.test_webui_content_vertical import (
    _GroupedSelectHarness, _running_app, _select_vertical,
)


def publishing_config(**changes):
    values = dict(
        upload_post_enabled=True, upload_post_api_key="fixture-key",
        upload_post_username="global-profile", upload_post_auto_upload=True,
        upload_post_platforms=["youtube"], upload_post_youtube_privacy_status="public",
        upload_post_youtube_made_for_kids=False,
        upload_post_verticals={
            "diy": {"username": "diy-profile", "title_template": "{subject} | DIY", "youtube_privacy_status": "private"},
            "type_beat": {"username": "beat-profile", "title_template": "{title} | {bpm} BPM {key}", "description_template": "{description}\nLease: {lease_url}"},
            "jewelry": {"username": "jewelry-profile", "description_template": "{subject}\n{hashtags}", "youtube_privacy_status": "unlisted"},
        },
    )
    values.update(changes)
    return values


@pytest.mark.parametrize("key,profile", [("diy", "diy-profile"), ("type_beat", "beat-profile"), ("jewelry", "jewelry-profile")])
def test_bindings_are_separate_and_do_not_mutate_config(key, profile):
    settings = publishing_config()
    selected = verticals.get_publishing_settings(key, settings)
    assert selected["username"] == profile
    selected["username"] = "changed"
    assert settings["upload_post_verticals"][key]["username"] == profile
    assert verticals.get_publishing_settings("none", settings) == {}
    assert verticals.get_publishing_settings(key, {}) == {}


def test_templates_preserve_generated_fields_and_expand_only_public_context():
    metadata = {"title": "Midnight", "caption": "Noir instrumental", "hashtags": ["#beat", "#noir"]}
    settings = {
        "title_template": "{title} | {bpm} BPM {key}",
        "description_template": "{{Original}} {description}\nLease: {lease_url}\n{hashtags}",
    }
    result = verticals.apply_publishing_templates(metadata, settings, {"bpm": "120", "key": "A minor", "lease_url": "https://lease.invalid/beat"})
    assert result["title"] == "Midnight | 120 BPM A minor"
    assert result["caption"] == "{Original} Noir instrumental\nLease: https://lease.invalid/beat\n#beat #noir"
    assert result["hashtags"] == metadata["hashtags"]
    assert metadata["title"] == "Midnight"
    assert verticals.apply_publishing_templates(metadata, {}, {}) == metadata


@pytest.mark.parametrize("changes", [
    {"username": 42}, {"youtube_privacy_status": "secret"},
    {"title_template": "{unknown}"}, {"title_template": "{subject.upper}"},
    {"title_template": "{subject[0]}"}, {"title_template": "{title!r}"},
    {"description_template": "{title:>1000000}"}, {"description_template": "{title"},
])
def test_invalid_settings_fail_before_queueing_or_upload(changes):
    with (
        patch.object(task, "_cross_post_slots") as slots,
        patch.object(task, "_cross_post_executor") as executor,
        patch.object(task, "_patch_cross_post_state") as state,
    ):
        error = task._schedule_cross_post(
            "publish", ["video.mp4"], VideoParams(content_vertical="diy", video_subject="Shelf"),
            "script", ["youtube"], "public", publishing_settings=changes,
        )
    assert error.startswith("invalid vertical publishing settings")
    assert "secret" not in error and "1000000" not in error
    slots.acquire.assert_not_called()
    executor.submit.assert_not_called()
    assert state.call_args.kwargs["cross_post_state"] == task.const.CROSS_POST_STATE_FAILED


def test_queue_snapshots_profile_privacy_templates_and_detected_beat_details(tmp_path):
    settings = publishing_config()
    params = VideoParams(
        content_vertical="type_beat", video_subject="Night drive", beat_bpm=0,
        beat_key="A minor", beat_lease_url="https://lease.invalid/beat",
    )
    metadata = {"title": "Midnight", "caption": "Noir instrumental", "hashtags": ["#beat"]}
    with (
        patch.object(config, "app", settings),
        patch.object(utils, "task_dir", return_value=str(tmp_path)),
        patch.object(task, "_cross_post_slots") as slots,
        patch.object(task, "_cross_post_executor") as executor,
        patch.object(task, "_register_cross_post_future"),
    ):
        task_artifacts.write_script_data("publish", {"beat_analysis": {"bpm": 120}})
        slots.acquire.return_value = True
        executor.submit.return_value = Future()
        assert task._schedule_cross_post(
            "publish", ["video.mp4"], params, "script", ["youtube"], "unlisted",
            publishing_metadata=metadata,
        ) is None
        queued = executor.submit.call_args
        settings["upload_post_username"] = "changed-global"
        settings["upload_post_verticals"]["type_beat"]["username"] = "changed-profile"
        settings["upload_post_verticals"]["type_beat"]["title_template"] = "Changed title"
        params.beat_key = "Changed key"
        metadata["hashtags"].append("#changed")
        with (
            patch.object(task, "_patch_cross_post_state", return_value=True),
            patch.object(task.llm, "generate_social_metadata") as llm,
            patch.object(upload_post, "cross_post_video", return_value={"success": True}) as upload,
        ):
            task._run_cross_post(*queued.args[1:], **queued.kwargs)
    llm.assert_not_called()
    assert queued.args[7] == "unlisted"
    assert upload.call_args.kwargs["account"]["upload_post_username"] == "beat-profile"
    extra = upload.call_args.kwargs["youtube_extra"]
    assert extra["youtube_title"] == "Midnight | 120 BPM A minor"
    assert extra["privacyStatus"] == "unlisted"
    assert extra["tags"] == ["#beat"]
    assert extra["youtube_description"].endswith("Lease: https://lease.invalid/beat")


def test_vertical_privacy_overrides_global_and_templates_use_generated_metadata():
    with (
        patch.object(config, "app", publishing_config()),
        patch.object(task, "_cross_post_slots") as slots,
        patch.object(task, "_cross_post_executor") as executor,
        patch.object(task, "_register_cross_post_future"),
    ):
        slots.acquire.return_value = True
        executor.submit.return_value = Future()
        task._schedule_cross_post("publish", ["video.mp4"], VideoParams(content_vertical="diy", video_subject="Shelf"), "script", ["youtube"], "public")
        queued = executor.submit.call_args
        with (
            patch.object(task, "_patch_cross_post_state", return_value=True),
            patch.object(task.llm, "generate_social_metadata", return_value={"title": "Generated", "caption": "Generated description", "hashtags": ["#diy"]}) as llm,
            patch.object(upload_post, "cross_post_video", return_value={"success": True}) as upload,
        ):
            task._run_cross_post(*queued.args[1:], **queued.kwargs)
    assert llm.call_count == 1
    assert upload.call_args.kwargs["youtube_extra"]["privacyStatus"] == "private"
    assert upload.call_args.kwargs["youtube_extra"]["youtube_title"] == "Shelf | DIY"
    assert upload.call_args.kwargs["youtube_extra"]["youtube_description"] == "Generated description"


def test_configured_vertical_profile_enables_publish_without_global_profile(tmp_path):
    params = VideoParams(
        content_vertical="diy", video_source="local", video_subject="Shelf",
        video_script="Build a shelf.", subtitle_enabled=False, bgm_type="",
    )
    with (
        patch.object(config, "app", publishing_config(upload_post_username="")),
        patch.object(utils, "task_dir", return_value=str(tmp_path)),
        patch.object(utils, "check_ffmpeg_ready", return_value=True),
        patch.object(task.sm.state, "update_task"),
        patch.object(task, "generate_audio", return_value=("voice.wav", 12, None)),
        patch.object(task, "generate_subtitle", return_value=""),
        patch.object(task, "get_video_materials", return_value=["clip.mp4"]),
        patch.object(task, "generate_final_videos", return_value=(["final.mp4"], ["combined.mp4"], [])),
        patch.object(task, "_schedule_cross_post", return_value=None) as schedule,
    ):
        assert not upload_post.upload_post_service.is_configured()
        result = task._run_pipeline("publish", params)
    assert result["videos"] == ["final.mp4"]
    assert result["cross_post_state"] == task.const.CROSS_POST_STATE_PENDING
    assert schedule.call_args.kwargs["publishing_settings"]["username"] == "diy-profile"


def test_profile_snapshot_is_sent_in_existing_upload_post_user_field(tmp_path):
    filename = tmp_path / "video.mp4"
    filename.write_bytes(b"fixture")
    service = upload_post.UploadPostService({
        "upload_post_enabled": True, "upload_post_api_key": "fixture-key", "upload_post_username": "diy-profile",
    })
    response = SimpleNamespace(
        status_code=200, raise_for_status=lambda: None,
        json=lambda: {"success": True, "results": {"youtube": {"success": True}}},
    )
    with patch.object(upload_post.requests, "post", return_value=response) as post:
        assert service.upload_video(str(filename), "Shelf", ["youtube"], youtube_extra={"privacyStatus": "private"})["success"]
    data = dict(post.call_args.kwargs["data"])
    assert data["user"] == "diy-profile"
    assert data["privacyStatus"] == "private"


def test_example_bindings_parse_as_app_tables_and_inherit_when_blank():
    filename = Path(__file__).parents[2] / "config.example.toml"
    with filename.open("rb") as file:
        example = tomllib.load(file)
    assert set(example["app"]["upload_post_verticals"]) == {"diy", "type_beat", "jewelry"}
    for key in ("diy", "type_beat", "jewelry"):
        assert verticals.get_publishing_settings(key, example["app"]) == {}


def test_webui_saves_independent_vertical_bindings_and_restores_them():
    with patch.dict(config.app, upload_post_verticals={}):
        with _running_app(_GroupedSelectHarness()) as app:
            _select_vertical(app, "diy")
            next(item for item in app.text_input if item.key == "vertical_publish_diy_username").set_value("diy-profile").run()
            next(item for item in app.text_input if item.key == "vertical_publish_diy_title_template").set_value("{subject} | DIY").run()
            next(item for item in app.selectbox if item.key == "vertical_publish_diy_youtube_privacy_status").set_value("private").run()
            _select_vertical(app, "type_beat")
            next(item for item in app.text_input if item.key == "vertical_publish_type_beat_username").set_value("beat-profile").run()
            assert config.app["upload_post_verticals"]["diy"]["username"] == "diy-profile"
            assert config.app["upload_post_verticals"]["type_beat"]["username"] == "beat-profile"
            _select_vertical(app, "diy")
            assert not list(app.exception)
            assert next(item for item in app.text_input if item.key == "vertical_publish_diy_username").value == "diy-profile"
            assert next(item for item in app.text_input if item.key == "vertical_publish_diy_title_template").value == "{subject} | DIY"
            assert next(item for item in app.selectbox if item.key == "vertical_publish_diy_youtube_privacy_status").value == "private"
