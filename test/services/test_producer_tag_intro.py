from unittest.mock import patch

import pytest

from scripts.render_typebeat_project import (
    official_tag_drop_positions,
    tag_preview_audio,
)


def test_tag_intro_refuses_missing_asset(tmp_path):
    with patch(
        "scripts.render_typebeat_project._materialize_remote_path",
        return_value=str(tmp_path / "missing.wav"),
    ):
        with pytest.raises(RuntimeError, match="unavailable"):
            tag_preview_audio(str(tmp_path / "master.wav"), "test", tmp_path)


def test_tag_intro_refuses_tampered_asset(tmp_path):
    wav = tmp_path / "not-the-official-tag.wav"
    wav.write_bytes(b"wrong")
    with patch(
        "scripts.render_typebeat_project._materialize_remote_path",
        return_value=str(wav),
    ):
        with pytest.raises(RuntimeError, match="integrity mismatch"):
            tag_preview_audio(str(tmp_path / "master.wav"), "test", tmp_path)


def test_every_instrumental_beat_video_repeats_approved_tag_without_overwriting_master():
    assert official_tag_drop_positions(159.0135, kind="beat") == [
        0.8,
        30.0,
        60.0,
        90.0,
        120.0,
        150.0,
    ]
    assert official_tag_drop_positions(92.075, kind="beat") == [0.8, 30.0, 60.0]
    assert official_tag_drop_positions(159.0135, kind="song") == []
    with pytest.raises(ValueError):
        official_tag_drop_positions(159.0, kind="album")
