"""No-network tests for opt-in private TwelveLabs asset review."""
from __future__ import annotations

from contextlib import nullcontext
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services import twelvelabs
from app.services.twelvelabs_private_review import analyze_local_video
from scripts import twelvelabs_private_review as cli


@pytest.fixture
def private_video(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "FACTORY", tmp_path.resolve())
    video = tmp_path / "autovideo_test" / "final" / "review.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x" * 40000)
    metadata = MagicMock(returncode=0, stdout=json.dumps({"format": {"duration": "120.0"}}))
    with patch.object(cli.subprocess, "run", return_value=metadata):
        yield video


def test_private_video_preflight_cannot_upload_even_with_key(private_video, monkeypatch):
    monkeypatch.setattr(twelvelabs, "is_enabled", lambda: True)
    with patch.object(cli, "analyze_local_video") as remote:
        report = cli.run(private_video)
    assert report["remote_upload_permitted"] is False
    assert report["publishing_approved"] is False
    assert len(report["sha256"]) == 64
    remote.assert_not_called()


def test_private_video_requires_real_key_before_any_network(private_video, monkeypatch):
    monkeypatch.setattr(twelvelabs, "is_enabled", lambda: False)
    with patch.object(cli, "analyze_local_video") as remote:
        with pytest.raises(RuntimeError, match="No TwelveLabs API key"):
            cli.run(private_video, allow_upload=True)
    remote.assert_not_called()


def test_private_video_disallows_path_outside_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "FACTORY", (tmp_path / "safe").resolve())
    p = tmp_path / "outside.mp4"
    p.write_bytes(b"fake" * 5000)
    with pytest.raises(ValueError, match="inside"):
        cli.plan_for(p)


def test_private_asset_upload_waits_then_analyzes_and_returns_review_only(tmp_path, monkeypatch):
    p = tmp_path / "motion.mp4"
    p.write_bytes(b"z" * 30000)
    monkeypatch.setattr(twelvelabs, "is_enabled", lambda: True)
    client = MagicMock()
    client.assets.create.return_value = SimpleNamespace(id="a" * 24)
    client.assets.retrieve.side_effect = [
        SimpleNamespace(status="processing"),
        SimpleNamespace(status="ready"),
    ]
    client.analyze.return_value = SimpleNamespace(data="A hooded figure changes location twice.")
    saved = []
    slept = []
    with patch.object(twelvelabs, "_managed_client", return_value=nullcontext(client)):
        result = analyze_local_video(
            p, "Describe visible changes between the shots.",
            allow_remote_upload=True, on_asset_created=saved.append,
            sleep=slept.append, poll_interval_seconds=.2,
        )
    assert saved == ["a" * 24]
    assert slept == [.2]
    assert result["publishing_approved"] is False
    assert result["visual_identity_verified"] is False
    assert "changes location" in result["response"]
    assert client.assets.create.call_args.kwargs["method"] == "direct"
    assert client.analyze.call_args.kwargs["model_name"] == "pegasus1.5"
    assert client.analyze.call_args.kwargs["video"].asset_id == "a" * 24


def test_private_asset_resume_reuses_existing_asset(tmp_path, monkeypatch):
    p = tmp_path / "motion.mp4"
    p.write_bytes(b"z" * 30000)
    monkeypatch.setattr(twelvelabs, "is_enabled", lambda: True)
    client = MagicMock()
    client.assets.retrieve.return_value = SimpleNamespace(status="ready")
    client.analyze.return_value = SimpleNamespace(data="Two shots of a person near water.")
    with patch.object(twelvelabs, "_managed_client", return_value=nullcontext(client)):
        result = analyze_local_video(
            p, "Describe the visual continuity in detail",
            existing_asset_id="b" * 24,
            allow_remote_upload=True,
        )
    client.assets.create.assert_not_called()
    assert result["asset_id"] == "b" * 24


def test_private_asset_refuses_implicit_upload_before_client(tmp_path, monkeypatch):
    p = tmp_path / "motion.mp4"
    p.write_bytes(b"z" * 30000)
    monkeypatch.setattr(twelvelabs, "is_enabled", lambda: True)
    with patch.object(twelvelabs, "_managed_client") as client:
        with pytest.raises(PermissionError):
            analyze_local_video(p, "Describe the physical motion in the video")
    client.assert_not_called()


def test_private_asset_rejects_large_file_before_remote_call(tmp_path, monkeypatch):
    p = tmp_path / "motion.mp4"
    p.write_bytes(b"y" * 30000)
    monkeypatch.setattr(twelvelabs, "is_enabled", lambda: True)
    # Patch the safety threshold rather than pathlib internals; this tests
    # rejection before any network without allocating a 201 MB test file.
    with patch("app.services.twelvelabs_private_review.MAX_DIRECT_VIDEO_BYTES", 25000):
        with patch.object(twelvelabs, "_managed_client") as client:
            with pytest.raises(ValueError, match="200 MB"):
                analyze_local_video(
                    p, "Describe the visual details of this clip",
                    allow_remote_upload=True,
                )
    client.assert_not_called()


def test_private_pegasus_prompt_uses_actual_ordered_lyrics_as_evidence(private_video):
    story = {
        "planning_source": "canonical_suno_lyrics_grounded_v2",
        "scenes": [
            {"lyric_excerpt": f"Evidence line {i} about the physical river"}
            for i in range(1, 31)
        ],
    }
    (private_video.parent.parent / "story.json").write_text(json.dumps(story))
    prompt = cli._review_prompt(private_video)
    assert "UNTRUSTED REFERENCE DATA" in prompt
    assert "Shot 01 expected lyric evidence: Evidence line 1" in prompt
    assert "Shot 30 expected lyric evidence: Evidence line 30" in prompt
    assert "ready for publishing" in prompt
    assert len(prompt) < 5000
