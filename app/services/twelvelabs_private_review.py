"""Privacy-gated local-video review using a TwelveLabs Pegasus asset.

Never uploads media unless explicitly authorized by the caller, and never
claims that a model description certifies character identity or release QA.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable

from app.services import twelvelabs

MAX_DIRECT_VIDEO_BYTES = 200_000_000
_ASSET_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def analyze_local_video(
    video: Path,
    prompt: str,
    *,
    allow_remote_upload: bool = False,
    existing_asset_id: str | None = None,
    on_asset_created: Callable[[str], None] | None = None,
    max_polls: int = 36,
    poll_interval_seconds: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Upload a private MP4 and analyze it only with explicit opt-in.

    - Returns asset ID, model response and review-only flags.
    - on_asset_created persists the asset ID before any polling, so a retry
      can reuse uploaded content rather than billing/uploading it twice.
    - Passing existing_asset_id avoids uploading bytes on resume.
    """
    if not allow_remote_upload:
        raise PermissionError("Remote private-video analysis requires explicit approval")
    if not twelvelabs.is_enabled():
        raise RuntimeError("TwelveLabs key is not configured")
    if not isinstance(prompt, str) or not 10 <= len(prompt.strip()) <= 5000:
        raise ValueError("A meaningful review prompt of at most 5000 characters is required")
    if not 1 <= max_polls <= 120 or not .1 <= poll_interval_seconds <= 60:
        raise ValueError("Polling budget is invalid")

    clip = Path(video).resolve(strict=True)
    if clip.suffix.lower() != ".mp4" or not clip.is_file():
        raise ValueError("Only existing MP4 review clips are supported")
    size = clip.stat().st_size
    if not 20_000 <= size <= MAX_DIRECT_VIDEO_BYTES:
        raise ValueError("Private video is outside the 20 KB to 200 MB direct-upload limit")
    if existing_asset_id is not None and not _ASSET_ID.fullmatch(existing_asset_id):
        raise ValueError("Invalid resumable TwelveLabs asset ID")

    from twelvelabs.types import VideoContext_AssetId

    with twelvelabs._managed_client() as client:
        if existing_asset_id:
            asset_id = existing_asset_id
        else:
            with clip.open("rb") as source:
                asset = client.assets.create(method="direct", file=source)
            asset_id = str(getattr(asset, "id", "") or "")
            if not _ASSET_ID.fullmatch(asset_id):
                raise RuntimeError("TwelveLabs did not return a valid asset ID")
            if on_asset_created is not None:
                on_asset_created(asset_id)

        for attempt in range(max_polls):
            status = str(client.assets.retrieve(asset_id).status or "").lower()
            if status == "ready":
                break
            if status == "failed":
                raise RuntimeError("TwelveLabs asset processing failed")
            if status not in {"processing", "pending", "uploading", "queued"}:
                raise RuntimeError("Unexpected TwelveLabs asset status")
            if attempt + 1 < max_polls:
                sleep(poll_interval_seconds)
        else:
            raise TimeoutError("TwelveLabs asset processing exceeded bounded poll budget")

        model = twelvelabs._model(
            "MPT_TWELVELABS_PEGASUS_MODEL",
            "twelvelabs_pegasus_model",
            twelvelabs.DEFAULT_PEGASUS_MODEL,
        )
        response = client.analyze(
            model_name=model,
            video=VideoContext_AssetId(asset_id=asset_id),
            prompt=prompt,
            max_tokens=max(twelvelabs._PEGASUS_MIN_MAX_TOKENS, 512),
        )
        content = str(getattr(response, "data", "") or "").strip()
        if not content:
            raise RuntimeError("TwelveLabs returned no review description")
        return {
            "review_status": "PEGASUS_TEXT_REVIEW_NOT_HUMAN_APPROVAL",
            "asset_id": asset_id,
            "pegasus_model": model,
            "response": content,
            "visual_identity_verified": False,
            "publishing_approved": False,
        }
