"""Security and integrity regressions for signed R2 media ingestion."""

import hashlib
import io
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.r2_media_intake import fetch_verified_media, validate_signed_url

KEY = "clips/b3d59e13-4db5-42ae-91f7-1e97885f1726/audio/b3d59e13-4db5-42ae-91f7-1e97885f1726.wav"
PROJECT = "mvbeat_obsidian_nightclub_20261008_e2e2"
CLIP = "b3d59e13-4db5-42ae-91f7-1e97885f1726"
HOST = "https://scene-continuity-relay.eternaleleganceemporium.workers.dev"
CUSTOM_HOST = "https://scene-media.aitoolpool.com"


def signed_url(key=KEY, expiry=None):
    if expiry is None:
        expiry = int(time.time()) + 300
    return f"{HOST}/v1/media/{key}?exp={expiry}&sig={'a' * 64}"


def test_valid_relay_url_and_rejects_expired_or_wrong_hosts():
    validate_signed_url(signed_url(), KEY)
    validate_signed_url(signed_url().replace(HOST, CUSTOM_HOST), KEY)
    for url in (
        signed_url().replace(HOST, "http://127.0.0.1:8080"),
        signed_url().replace(HOST, "https://evil.example"),
        signed_url(expiry=int(time.time()) - 1),
        signed_url(expiry=int(time.time()) + 1800),
        signed_url().replace("sig=" + "a" * 64, "sig=" + "short"),
        signed_url().replace(KEY, "clips/other/audio/file.wav"),
    ):
        with pytest.raises(ValueError):
            validate_signed_url(url, KEY)


def test_ingest_checks_identity_before_network(tmp_path):
    with pytest.raises(ValueError, match="R2 clip key"):
        fetch_verified_media(
            signed_url=signed_url(),
            asset_key=KEY,
            expected_sha256="a" * 64,
            project_id=PROJECT,
            clip_id="other",
            root=tmp_path,
        )


def test_download_verified_bytes_and_idempotent_replay(tmp_path):
    raw = b"RIFF" + b"valid wav bytes" * 100
    sha = hashlib.sha256(raw).hexdigest()

    class FakeResponse(io.BytesIO):
        status = 200

    class FakeOpener:
        def open(self, request, timeout):
            return FakeResponse(raw)

    probe = SimpleNamespace(
        stdout=json.dumps(
            {
                "format": {"duration": "131.7335", "size": str(len(raw))},
                "streams": [{"codec_type": "audio", "codec_name": "pcm_s16le"}],
            }
        )
    )
    args = dict(
        signed_url=signed_url(),
        asset_key=KEY,
        expected_sha256=sha,
        project_id=PROJECT,
        clip_id=CLIP,
        root=tmp_path,
    )
    with (
        patch(
            "scripts.r2_media_intake.urllib.request.build_opener",
            return_value=FakeOpener(),
        ),
        patch("scripts.r2_media_intake.subprocess.run", return_value=probe),
    ):
        first = fetch_verified_media(**args)
        second = fetch_verified_media(**args)
    assert first["success"]
    assert first["sha256"] == sha
    assert first["duration_seconds"] == 131.7335
    assert first["media_type"] == "audio"
    assert first["publishing_approved"] is False
    assert first["previously_imported"] is False
    assert second["previously_imported"] is True


def test_wrong_download_checksum_rejected(tmp_path):
    data = b"not the expected source" * 100

    class FakeResponse(io.BytesIO):
        status = 200

    class FakeOpener:
        def open(self, request, timeout):
            return FakeResponse(data)

    with patch(
        "scripts.r2_media_intake.urllib.request.build_opener", return_value=FakeOpener()
    ):
        with pytest.raises(ValueError, match="checksum/size"):
            fetch_verified_media(
                signed_url=signed_url(),
                asset_key=KEY,
                expected_sha256="a" * 64,
                project_id=PROJECT,
                clip_id=CLIP,
                root=tmp_path,
            )
    assert not list(tmp_path.rglob("*.partial"))
