"""Bounded, evidence-gated free video adapters for the music factory.

Firefly remains the default. Other providers enter the scene rotation only
after a real motion canary has been verified on this host. Never confuse an
n8n workflow's 'success' execution status with a verified video asset.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time
import urllib.request
import urllib.error

class VideoProviderPolicyError(RuntimeError):
    """Explicit provider refusal. Never retry the identical rejected prompt."""


PROVIDERS = ("firefly", "grok2api", "metaai", "qwenapi", "wan22_local")
HEALTH = Path("/home/bobby/.cache/scene-continuity/video-provider-health.json")
LAN = "http://10.0.0.243:5678/webhook"
ENDPOINTS = {
    "firefly": LAN + "/strictlybeats-unlimited-firefly-video",
    "grok2api": LAN + "/grok-video-ops",
    "metaai": LAN + "/metaai-video-generate",
    "qwenapi": LAN + "/qwen-video-generate",
}
STORED = re.compile(r"^/srv/data/n8n-media/store/[A-Za-z0-9/_-]+\.mp4$")
FIREFLY_STORED = re.compile(
    r"^/srv/data/n8n-media/store/firefly/videos/fullmotion-[0-9]+-scene-(\d+)\.mp4$"
)


def verified_providers(*, now: float | None = None, health_file: Path = HEALTH) -> tuple[str, ...]:
    """Only current on-host decoded-motion canaries authorize optional routes."""
    now = time.time() if now is None else now
    try:
        report = json.loads(health_file.read_text())
    except (OSError, ValueError, TypeError):
        report = {}
    active = []
    for name in PROVIDERS[1:]:
        evidence = report.get(name, {})
        if not isinstance(evidence, dict):
            continue
        if name == "wan22_local":
            from scripts.music_factory_wan22_local import production_ready
            if not production_ready(health_file=health_file, now=now):
                continue
        if (evidence.get("motion_verified") is True
            and evidence.get("provider") == name
            and float(evidence.get("verified_at") or 0) <= now
            and 0 <= now - float(evidence.get("verified_at") or 0) < 86400
            and evidence.get("approved_free_route") is True):
            active.append(name)
    return tuple(active)


def factory_provider_readiness(*, now: float | None = None, health_file: Path = HEALTH) -> dict:
    """Fail closed until a *real decoded-motion* and free-route receipt is current.

    No paid-provider fallback is inferred from the presence of a webhook or login.
    This is a scheduling gate, not a provider authorization bypass.
    """
    now = time.time() if now is None else now
    active = verified_providers(now=now, health_file=health_file)
    try:
        evidence = json.loads(health_file.read_text())
    except (OSError, ValueError, TypeError):
        evidence = {}
    firefly = evidence.get("firefly", {}) if isinstance(evidence, dict) else {}
    firefly_ready = (
        isinstance(firefly, dict)
        and firefly.get("provider") == "firefly"
        and firefly.get("motion_verified") is True
        and firefly.get("zero_credits_verified") is True
        and firefly.get("approved_free_route") is True
        and float(firefly.get("verified_at") or 0) <= now
        and 0 <= now - float(firefly.get("verified_at") or 0) < 86400
    )
    providers = list(active)
    if firefly_ready:
        providers.append("firefly")
    return {
        "ready": bool(providers),
        "status": "READY" if providers else "HOLD_MOTION_PROVIDER_UNVERIFIED",
        "verified_providers": providers,
        "publishing_approved": False,
    }


def scene_provider_order(slot: int, *, now: float | None = None, health_file: Path = HEALTH) -> tuple[str, ...]:
    """Spread verified routes across the edit while always retaining Firefly."""
    active = verified_providers(now=now, health_file=health_file)
    # Do not retry Firefly when billing/fair-use entitlement is unverified.
    # The /factory/next hold also enforces this, but manual worker invocations
    # must independently fail closed rather than attempting a paid provider.
    firefly_ready = "firefly" in factory_provider_readiness(
        now=now, health_file=health_file
    )["verified_providers"]
    if not active:
        return ("firefly",) if firefly_ready else ()
    picked = active[(slot - 1) % len(active)]
    return (picked, "firefly") if firefly_ready else (picked,)


def provider_request(provider: str, prompt: str, slot: int) -> dict:
    if provider not in PROVIDERS or not (1 <= slot <= 50):
        raise ValueError("Unsupported provider or scene number")
    if provider == "firefly":
        return {"slot": slot, "duration": 5, "prompt": prompt,
                "strict_unlimited": True, "allow_credit_usage": False}
    if provider == "grok2api":
        return {"slot": slot, "prompt": prompt, "operation": "generate",
                "model": "Build/grok-imagine-video-1.5", "duration": 5,
                "size": "16:9", "resolution": "720p", "allow_credit_usage": False}
    if provider == "qwenapi":
        return {"slot": slot, "prompt": prompt, "duration": 5,
                "size": "16:9", "resolution": "720p", "allow_credit_usage": False}
    return {"slot": slot, "prompt": prompt, "allow_credit_usage": False}


def request_video(
    provider: str, prompt: str, slot: int,
    timeout: int = 360, *, image_file: str | None = None
) -> dict:
    if provider == "wan22_local":
        from scripts.music_factory_wan22_local import run_approved_scene
        return run_approved_scene(
            prompt, slot, image_path=image_file, timeout=timeout
        )
    payload = json.dumps(provider_request(provider, prompt, slot)).encode()
    req = urllib.request.Request(
        ENDPOINTS[provider], data=payload, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")
        # Permanent request rejection and billing/fair-use holds fail closed.
        # Retrying the same rejected request cannot establish zero-credit entitlement.
        if exc.code in (400, 402, 403) or re.search(
            r"fair[- ]use|credit[- ]charged|credit spending|billing.*verif|unlimited video unavailable",
            detail, re.I,
        ):
            raise VideoProviderPolicyError(
                f"Video provider refused a credit-free request (HTTP {exc.code})"
            ) from exc
        if re.search(r"\[nsfw\]|considered unsafe|content.policy|safety.filter", detail, re.I):
            raise VideoProviderPolicyError(
                "Video provider refused the prompt under its content policy"
            ) from exc
        raise
    return result.get("data", result) if isinstance(result, dict) else {}


def validated_path(provider: str, receipt: dict, *, slot: int) -> str:
    """Accept only real durable n8n MP4 locations; reject misleading success."""
    if provider not in PROVIDERS or receipt.get("success") is not True:
        raise ValueError("Video generation did not report success")
    if provider == "wan22_local":
        from scripts.music_factory_wan22_local import DEST
        remote = str(receipt.get("host_path") or "")
        path = Path(remote).expanduser().resolve(strict=True)
        if not path.is_relative_to(DEST.resolve()):
            raise ValueError("Local Wan video outside approved output directory")
        if not path.name.endswith(".mp4") or not path.is_file():
            raise ValueError("Local Wan receipt is not a video file")
        if receipt.get("no_external_api_credits") is not True or (
            receipt.get("native_motion_verified") is not True
        ):
            raise ValueError("Local Wan origin/motion receipt missing")
        import hashlib
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        if checksum != receipt.get("sha256"):
            raise ValueError("Local Wan video SHA256 mismatch")
        return str(path)
    remote = str(receipt.get("host_path") or "")
    if not STORED.fullmatch(remote) or ".." in remote or "//" in remote:
        raise ValueError("Video provider returned an unsafe or missing stored MP4")
    if provider == "firefly":
        m = FIREFLY_STORED.fullmatch(remote)
        if (not m or int(m.group(1)) != slot or
            receipt.get("provider") != "firefly" or
            receipt.get("firefly_fair_use") is not True or
            receipt.get("credit_spending_allowed") is not False):
            raise ValueError("Firefly strict fair-use receipt failed verification")
    return remote


def probe_motion_canary(media_file: Path) -> bool:
    """Use the same real-decoded moving-frame gate as the production worker."""
    from scripts.music_factory_worker import moving
    return moving(media_file)
