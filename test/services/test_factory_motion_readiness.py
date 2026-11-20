"""Factory scheduling requires recent verified free motion, never webhook-only success."""
import json
import time

from scripts.music_factory_provider_router import factory_provider_readiness


def test_missing_evidence_holds(tmp_path):
    result = factory_provider_readiness(health_file=tmp_path / "missing.json")
    assert result["status"] == "HOLD_MOTION_PROVIDER_UNVERIFIED"
    assert result["ready"] is False
    assert result["publishing_approved"] is False


def test_verified_optional_free_provider_allows_queue(tmp_path):
    path = tmp_path / "providers.json"
    now = time.time()
    path.write_text(json.dumps({
        "grok2api": {
            "provider": "grok2api", "motion_verified": True,
            "approved_free_route": True, "verified_at": now
        }
    }))
    result = factory_provider_readiness(now=now, health_file=path)
    assert result["ready"] is True
    assert result["verified_providers"] == ["grok2api"]
    assert result["publishing_approved"] is False


def test_free_route_without_decoded_motion_holds(tmp_path):
    path = tmp_path / "providers.json"
    now = time.time()
    path.write_text(json.dumps({"grok2api": {
        "provider": "grok2api", "motion_verified": False,
        "approved_free_route": True, "verified_at": now
    }}))
    assert factory_provider_readiness(now=now, health_file=path)["ready"] is False


def test_firefly_requires_explicit_zero_credit_receipt(tmp_path):
    path = tmp_path / "providers.json"
    now = time.time()
    base = {"provider": "firefly", "motion_verified": True,
            "approved_free_route": True, "verified_at": now}
    path.write_text(json.dumps({"firefly": base}))
    assert factory_provider_readiness(now=now, health_file=path)["ready"] is False
    path.write_text(json.dumps({"firefly": {**base, "zero_credits_verified": False}}))
    assert factory_provider_readiness(now=now, health_file=path)["ready"] is False
    path.write_text(json.dumps({"firefly": {**base, "zero_credits_verified": True}}))
    result = factory_provider_readiness(now=now, health_file=path)
    assert result["ready"] is True
    assert result["verified_providers"] == ["firefly"]


def test_canaries_expire_after_one_day(tmp_path):
    path = tmp_path / "providers.json"
    now = time.time()
    path.write_text(json.dumps({"grok2api": {
        "provider": "grok2api", "motion_verified": True,
        "approved_free_route": True, "verified_at": now - 86401
    }}))
    assert factory_provider_readiness(now=now, health_file=path)["ready"] is False
