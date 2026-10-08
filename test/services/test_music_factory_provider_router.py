"""Free video provider routing and signed motion-receipt regressions."""
import io
import json
import urllib.error
from unittest.mock import patch
import pytest

from scripts.music_factory_provider_router import (
    provider_request, scene_provider_order, validated_path, verified_providers,
    request_video, VideoProviderPolicyError
)


def test_provider_health_requires_recent_motion_and_free_route(tmp_path):
    path = tmp_path / "provider-health.json"
    now = 1000000.0
    assert scene_provider_order(1, now=now, health_file=path) == ("firefly",)
    path.write_text(json.dumps({
        "grok2api": {"provider": "grok2api", "motion_verified": True,
                     "approved_free_route": True, "verified_at": now - 60},
        "metaai": {"provider": "metaai", "motion_verified": True,
                   "approved_free_route": True, "verified_at": now - 86500},
        "qwenapi": {"provider": "qwenapi", "motion_verified": False,
                    "approved_free_route": True, "verified_at": now},
    }))
    assert verified_providers(now=now, health_file=path) == ("grok2api",)
    assert scene_provider_order(1, now=now, health_file=path) == (
        "grok2api", "firefly"
    )


def test_zero_credit_requested_from_every_provider():
    for provider in ("firefly", "grok2api", "metaai", "qwenapi"):
        assert provider_request(provider, "shot", 12)["allow_credit_usage"] is False
    with pytest.raises(ValueError):
        provider_request("unsafe-provider", "shot", 1)


def test_receipt_requires_stored_video_and_firefly_fair_use():
    path = "/srv/data/n8n-media/store/firefly/videos/fullmotion-100-scene-12.mp4"
    good = {"success": True, "host_path": path, "provider": "firefly",
            "firefly_fair_use": True, "credit_spending_allowed": False}
    assert validated_path("firefly", good, slot=12) == path
    with pytest.raises(ValueError):
        validated_path("firefly", dict(good, credit_spending_allowed=True), slot=12)
    with pytest.raises(ValueError):
        validated_path("firefly", good, slot=13)
    with pytest.raises(ValueError):
        validated_path("grok2api", {"success": True,
            "host_path": "/srv/data/n8n-media/store/../escape.mp4"}, slot=12)
    with pytest.raises(ValueError):
        validated_path("metaai", {"success": False,
            "host_path": "/srv/data/n8n-media/store/meta/videos/output.mp4"}, slot=12)


def test_explicit_content_refusal_is_not_misreported_as_generic_500():
    refusal = urllib.error.HTTPError(
        "https://example.invalid/video", 500, "Internal server error", {},
        io.BytesIO(b'{"message":"[nsfw] The provided prompt is considered unsafe."}'),
    )
    with patch("scripts.music_factory_provider_router.urllib.request.urlopen", side_effect=refusal):
        with pytest.raises(VideoProviderPolicyError, match="content policy"):
            request_video("firefly", "A harmless original cinematic landscape", 25)


def test_unrelated_500_stays_retryable():
    error = urllib.error.HTTPError(
        "https://example.invalid/video", 500, "Server Error", {},
        io.BytesIO(b'{"message":"upstream signer timeout"}'),
    )
    with patch("scripts.music_factory_provider_router.urllib.request.urlopen", side_effect=error):
        with pytest.raises(urllib.error.HTTPError):
            request_video("firefly", "A cinematic landscape", 25)
