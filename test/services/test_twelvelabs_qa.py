"""No-network tests for opt-in TwelveLabs QA of generated videos."""

from __future__ import annotations

from contextlib import nullcontext
import json
import stat
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.config import config
from app.models.schema import VideoParams
from app.services import task, twelvelabs, twelvelabs_qa
from app.services.twelvelabs_private_review import analyze_local_video
from app.utils import utils


@pytest.fixture
def task_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(
        twelvelabs_qa.utils,
        "task_dir",
        lambda task_id: str(tmp_path),
    )
    return tmp_path


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "final-1.mp4"
    path.write_bytes(b"v" * 50_000)
    return path


def _enable_twelvelabs(monkeypatch):
    monkeypatch.setattr(twelvelabs, "is_enabled", lambda: True)


def _payload(verdict: str = "pass", publishing_ready: bool = True) -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "publishing_ready": publishing_ready,
            "findings": [
                {
                    "timestamp": "12.0",
                    "severity": "none",
                    "category": "script_match",
                    "detail": "Visible action matches the script.",
                }
            ],
        }
    )


def test_qa_disabled_returns_no_reports(task_storage, video, monkeypatch):
    monkeypatch.setenv("MPT_TWELVELABS_QA_MODE", "off")
    _enable_twelvelabs(monkeypatch)
    params = SimpleNamespace(video_subject="city skyline")
    assert (
        twelvelabs_qa.review_final_videos(
            "task-1", params, "A city skyline.", [str(video)]
        )
        == []
    )


def test_gate_blocks_when_remote_upload_is_not_approved(
    task_storage, video, monkeypatch
):
    monkeypatch.setenv("MPT_TWELVELABS_QA_MODE", "gate")
    monkeypatch.setenv("MPT_TWELVELABS_QA_UPLOAD", "0")
    _enable_twelvelabs(monkeypatch)

    params = SimpleNamespace(video_subject="city skyline")
    reports = twelvelabs_qa.review_final_videos(
        "task-1", params, "A city skyline.", [str(video)]
    )

    assert reports[0]["status"] == "skipped"
    assert reports[0]["skip_reason"] == "remote_upload_not_approved"
    assert reports[0]["publishing_blocker"] is True
    assert twelvelabs_qa.publishing_blocked(reports) is True


def test_gate_passes_and_writes_private_report(task_storage, video, monkeypatch):
    monkeypatch.setenv("MPT_TWELVELABS_QA_MODE", "gate")
    monkeypatch.setenv("MPT_TWELVELABS_QA_UPLOAD", "1")
    _enable_twelvelabs(monkeypatch)

    calls: list[dict] = []

    def fake_analyze(path, prompt, **kwargs):
        calls.append(
            {
                "path": str(path),
                "prompt_sha256": twelvelabs_qa._prompt_digest(prompt),
                **kwargs,
            }
        )
        return {
            "asset_id": "a" * 24,
            "pegasus_model": "pegasus1.5",
            "review_status": "PEGASUS_TEXT_REVIEW_NOT_HUMAN_APPROVAL",
            "response": _payload(),
        }

    monkeypatch.setattr(twelvelabs_qa, "analyze_local_video", fake_analyze)

    params = SimpleNamespace(video_subject="city skyline")
    reports = twelvelabs_qa.review_final_videos(
        "task-1", params, "A city skyline at dusk.", [str(video)]
    )

    assert len(calls) == 1
    assert calls[0]["response_format"]["type"] == "json_schema"
    assert calls[0]["response_format"]["json_schema"]["required"] == [
        "verdict",
        "publishing_ready",
        "findings",
    ]
    assert reports[0]["status"] == "passed"
    assert reports[0]["publishing_blocker"] is False

    report_path = task_storage / "twelvelabs-qa-1.json"
    assert report_path.is_file()
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "passed"
    assert persisted["sha256"]

    # A second identical review must not send paid bytes twice.
    reports_again = twelvelabs_qa.review_final_videos(
        "task-1", params, "A city skyline at dusk.", [str(video)]
    )
    assert len(calls) == 1
    assert reports_again[0]["status"] == "passed"
    assert reports_again[0]["publishing_blocker"] is False


def test_gate_blocks_failed_verdict(task_storage, video, monkeypatch):
    monkeypatch.setenv("MPT_TWELVELABS_QA_MODE", "gate")
    monkeypatch.setenv("MPT_TWELVELABS_QA_UPLOAD", "1")
    _enable_twelvelabs(monkeypatch)

    def fake_analyze(path, prompt, **kwargs):
        return {"asset_id": "b" * 24, "response": _payload("fail", False)}

    monkeypatch.setattr(twelvelabs_qa, "analyze_local_video", fake_analyze)

    params = SimpleNamespace(video_subject="city skyline")
    reports = twelvelabs_qa.review_final_videos(
        "task-1", params, "A city skyline at dusk.", [str(video)]
    )

    assert reports[0]["status"] == "failed"
    assert reports[0]["publishing_blocker"] is True
    assert twelvelabs_qa.blocked_warnings(reports) == [
        {"code": "twelvelabs_qa_blocked", "video_index": 1}
    ]


def test_malformed_json_fails_closed(task_storage, video, monkeypatch):
    monkeypatch.setenv("MPT_TWELVELABS_QA_MODE", "gate")
    monkeypatch.setenv("MPT_TWELVELABS_QA_UPLOAD", "1")
    _enable_twelvelabs(monkeypatch)

    monkeypatch.setattr(
        twelvelabs_qa,
        "analyze_local_video",
        lambda path, prompt, **kwargs: {"asset_id": "c" * 24, "response": "not json"},
    )

    params = SimpleNamespace(video_subject="city skyline")
    reports = twelvelabs_qa.review_final_videos(
        "task-1", params, "A city skyline at dusk.", [str(video)]
    )

    assert reports[0]["status"] == "malformed"
    assert reports[0]["publishing_blocker"] is True


def test_extract_payload_accepts_fenced_json():
    payload = twelvelabs_qa._extract_payload(
        '```json\n{"verdict":"pass","publishing_ready":true,"findings":[]}\n```'
    )
    assert payload == {
        "verdict": "pass",
        "publishing_ready": True,
        "findings": [],
    }


def test_private_review_can_request_structured_qa(tmp_path, monkeypatch):
    video = tmp_path / "review.mp4"
    video.write_bytes(b"z" * 40_000)
    monkeypatch.setattr(twelvelabs, "is_enabled", lambda: True)
    client = MagicMock()
    client.assets.create.return_value = SimpleNamespace(id="d" * 24)
    client.assets.retrieve.return_value = SimpleNamespace(status="ready")
    client.analyze.return_value = SimpleNamespace(
        data='{"verdict":"pass","publishing_ready":true,"findings":[]}'
    )
    schema = twelvelabs_qa.response_schema()

    with patch.object(twelvelabs, "_managed_client", return_value=nullcontext(client)):
        result = analyze_local_video(
            video,
            "Describe visible continuity in this generated video.",
            allow_remote_upload=True,
            response_format={"type": "json_schema", "json_schema": schema},
            max_tokens=2048,
        )

    assert result["asset_id"] == "d" * 24
    assert client.analyze.call_args.kwargs["response_format"]["json_schema"] == schema
    assert client.analyze.call_args.kwargs["max_tokens"] == 2048


def test_private_review_rejects_bad_max_tokens(tmp_path, monkeypatch):
    video = tmp_path / "review.mp4"
    video.write_bytes(b"z" * 40_000)
    monkeypatch.setattr(twelvelabs, "is_enabled", lambda: True)

    with pytest.raises(ValueError, match="max_tokens"):
        analyze_local_video(
            video,
            "Describe visible continuity in this generated video.",
            allow_remote_upload=True,
            max_tokens=511,
        )


def _publishing_config():
    return dict(
        upload_post_enabled=True,
        upload_post_api_key="fixture-key",
        upload_post_username="global-profile",
        upload_post_auto_upload=True,
        upload_post_platforms=["youtube"],
        upload_post_youtube_privacy_status="public",
        upload_post_youtube_made_for_kids=False,
        upload_post_verticals={},
    )


def test_failed_qa_blocks_cross_posting(tmp_path, monkeypatch):
    monkeypatch.setenv("MPT_TWELVELABS_QA_MODE", "gate")
    blocked = {
        "video_index": 1,
        "status": "failed",
        "publishing_blocker": True,
    }
    monkeypatch.setattr(
        task.twelvelabs_qa,
        "review_final_videos",
        lambda *args, **kwargs: [blocked],
    )

    params = VideoParams(
        video_source="local",
        video_subject="QA video",
        video_script="A short script.",
        subtitle_enabled=False,
        bgm_type="",
    )
    with (
        patch.object(config, "app", _publishing_config()),
        patch.object(utils, "task_dir", return_value=str(tmp_path)),
        patch.object(utils, "check_ffmpeg_ready", return_value=True),
        patch.object(task.sm.state, "update_task"),
        patch.object(task, "generate_audio", return_value=("voice.wav", 12, None)),
        patch.object(task, "generate_subtitle", return_value=""),
        patch.object(task, "get_video_materials", return_value=["clip.mp4"]),
        patch.object(
            task,
            "generate_final_videos",
            return_value=(["final.mp4"], ["combined.mp4"], []),
        ),
        patch.object(task, "_schedule_cross_post") as schedule,
    ):
        result = task._run_pipeline("task-1", params)

    schedule.assert_not_called()
    assert result["videos"] == ["final.mp4"]
    assert result["twelvelabs_review"] == [blocked]
    assert result["cross_post_state"] == task.const.CROSS_POST_STATE_FAILED
    assert result["cross_post_error"] == "publishing blocked by TwelveLabs QA"
    assert {
        "code": "twelvelabs_qa_blocked",
        "video_index": 1,
    } in result["warnings"]
