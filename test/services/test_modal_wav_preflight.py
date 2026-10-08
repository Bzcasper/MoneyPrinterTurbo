import json
import subprocess
from unittest.mock import patch

import pytest

from scripts.modal_wav_preflight import verify_song

CLIP = "d512fc9b-5246-4488-a0fd-e5110247f1e9"
TITLE = "VAMPMIN-VAR-003-R04-06954"


def test_preflight_rejects_invalid_uuid_and_title():
    with pytest.raises(ValueError, match="UUID"):
        verify_song("../escape", TITLE)
    with pytest.raises(ValueError, match="title"):
        verify_song(CLIP, "")


def test_preflight_confirms_matching_modal_volume_file_and_sha(tmp_path):
    item = {
        "id": CLIP, "title": TITLE, "duration": 91.8935,
        "_wav_sha256": "a" * 64, "wavBytes": 17643686,
    }
    expected = f"clips/{CLIP}/audio/{CLIP}.wav"

    def subprocess_fake(argv, **kwargs):
        assert argv[2] in ("get", "ls")
        if argv[2] == "ls":
            return subprocess.CompletedProcess(argv, 0, expected + "\n", "")
        output = argv[-1]
        with open(output, "w") as f:
            json.dump({"items": [item]}, f)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    with patch("scripts.modal_wav_preflight.subprocess.run", side_effect=subprocess_fake):
        result = verify_song(CLIP, TITLE)
    assert result["ok"] is True
    assert result["catalog_sha256"] == item["_wav_sha256"]
    assert result["publishing_approved"] is False
    assert result["source_type"] == "playback_derived_wav"


def test_preflight_rejects_missing_catalog_record():
    def subprocess_fake(argv, **kwargs):
        with open(argv[-1], "w") as f:
            json.dump({"items": []}, f)
        return subprocess.CompletedProcess(argv, 0, "", "")

    with patch("scripts.modal_wav_preflight.subprocess.run", side_effect=subprocess_fake):
        with pytest.raises(ValueError, match="missing or duplicated"):
            verify_song(CLIP, TITLE)
