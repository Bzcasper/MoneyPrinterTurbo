"""R2 size-bound delivery keeps the canonical music-video master untouched."""

import hashlib
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from scripts.music_factory_delivery import prepare_delivery


def test_normal_render_is_preserved_unmodified(tmp_path):
    source = tmp_path / "song.mp4"
    source.write_bytes(b"a" * 2_000_000)
    file, qa = prepare_delivery(source, duration_seconds=121.0)
    assert file == source
    assert qa["delivery_reencoded"] is False
    assert qa["source_master_sha256"] == qa["delivery_sha256"]
    assert qa["delivery_bytes"] == 2_000_000


def test_oversized_master_gets_private_review_copy(tmp_path):
    source = tmp_path / "song.mp4"
    source.write_bytes(b"a" * (9 * 1024 * 1024))
    original_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        if cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"v" * (2 * 1024 * 1024))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "ffprobe":
            return subprocess.CompletedProcess(
                cmd,
                0,
                json.dumps(
                    {
                        "format": {"duration": "101.0"},
                        "streams": [
                            {"codec_type": "video", "codec_name": "h264"},
                            {"codec_type": "audio", "codec_name": "aac"},
                        ],
                    }
                ),
                "",
            )
        raise AssertionError("Unexpected external process")

    with patch("scripts.music_factory_delivery.subprocess.run", side_effect=fake_run):
        file, qa = prepare_delivery(
            source, duration_seconds=101.0, cap_bytes=8 * 1024 * 1024
        )
    assert file.is_file()
    assert file != source
    assert file.stat().st_size < 8 * 1024 * 1024
    assert source.stat().st_size == 9 * 1024 * 1024
    assert qa["delivery_reencoded"] is True
    assert qa["source_master_sha256"] == original_sha
    assert qa["delivery_sha256"] != original_sha
    assert qa["delivery_qa"] == "additional_visual_review_required"
    assert (
        "-c:a" in commands[0] and commands[0][commands[0].index("-c:a") + 1] == "copy"
    )
    assert not list(tmp_path.glob("*.tmp.mp4"))


def test_invalid_delivery_inputs_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        prepare_delivery(tmp_path / "missing.mp4", duration_seconds=100)
    p = tmp_path / "video.mp4"
    p.write_bytes(b"z" * 2_000_000)
    with pytest.raises(ValueError):
        prepare_delivery(p, duration_seconds=-1)
