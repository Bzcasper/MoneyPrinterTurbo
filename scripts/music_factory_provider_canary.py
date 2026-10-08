"""Verify optional video generators before admitting them to continuous rotation.

Usage: python3 -m scripts.music_factory_provider_canary grok2api --approve-free-route
A successful decoded-motion canary with the explicit free-route approval updates
the 24h evidence ledger; unsuccessful checks never enable paid fallback.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

from scripts.music_factory_provider_router import (
    HEALTH, PROVIDERS, request_video, validated_path
)
from scripts.music_factory_worker import _normalize_video_length, moving

CANARY_DIR = Path("/home/bobby/.cache/scene-continuity/video-canaries")
PROMPT = (
    "Cinematic five-second native moving-video shot, no cuts: "
    "one cobalt glass prism floating four feet above polished black stone "
    "in an enclosed signal chamber. 35mm waist-level lateral camera dolly; "
    "near foreground columns create real parallax. Fine silver haze travels "
    "through an actual cyan spotlight, reflections move consistently with "
    "the camera; physically stable single prism, no morphing, no people, "
    "no faces, no lettering, no logos, no graphics. 16:9 landscape."
)


def verify_provider(provider: str, *, activate: bool, timeout: int) -> dict:
    if provider not in PROVIDERS[1:]:
        raise ValueError("Use grok2api, metaai or qwenapi for optional canaries")
    CANARY_DIR.mkdir(parents=True, exist_ok=True)
    result = {"provider": provider, "verified_at": int(time.time()),
              "motion_verified": False, "approved_free_route": False}
    partial = CANARY_DIR / (provider + ".partial.mp4")
    try:
        response = request_video(provider, PROMPT, 1, timeout=timeout)
        remote = validated_path(provider, response, slot=1)
        subprocess.run(
            ["scp", "-q", "-o", "BatchMode=yes", "bobby-nuc:" + remote, str(partial)],
            timeout=120, check=True, capture_output=True
        )
        _normalize_video_length(partial)
        if not moving(partial):
            raise ValueError("Decoded-frame motion gate did not pass")
        dest = CANARY_DIR / (provider + ".mp4")
        partial.replace(dest)
        result.update({
            "motion_verified": True,
            "approved_free_route": bool(activate),
            "model": str(response.get("model") or provider)[:95],
            "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
            "canary_file": str(dest),
            "free_policy_source": "user_explicit_free_route_approval" if activate
                                  else "review_required",
        })
    except Exception as exc:
        result["error"] = (type(exc).__name__ + ": " + str(exc))[:230]
    finally:
        partial.unlink(missing_ok=True)
    try:
        previous = json.loads(HEALTH.read_text())
    except (OSError, ValueError):
        previous = {}
    previous[provider] = result
    HEALTH.parent.mkdir(parents=True, exist_ok=True)
    stage = HEALTH.with_suffix(".tmp")
    stage.write_text(json.dumps(previous, indent=2) + "\n")
    stage.chmod(0o600)
    stage.replace(HEALTH)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("provider", choices=PROVIDERS[1:])
    parser.add_argument("--approve-free-route", action="store_true")
    parser.add_argument("--timeout", type=int, default=330)
    options = parser.parse_args()
    receipt = verify_provider(options.provider, activate=options.approve_free_route,
                              timeout=options.timeout)
    print(json.dumps({k: v for k, v in receipt.items()
                      if k not in {"canary_file"}}, indent=2))
    raise SystemExit(0 if receipt["motion_verified"] else 1)
