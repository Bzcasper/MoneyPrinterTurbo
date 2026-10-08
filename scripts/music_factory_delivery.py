"""Bounded private R2 delivery copy for unusually large review masters.

Never modifies the original Modal-rendered MP4. Quality downgrade is recorded.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

MAX_R2_SINGLE_UPLOAD = 64 * 1024 * 1024
DELIVERY_TARGET = 58 * 1024 * 1024


def prepare_delivery(
    source: Path, *, duration_seconds: float, cap_bytes: int = MAX_R2_SINGLE_UPLOAD
) -> tuple[Path, dict]:
    if not source.is_file() or source.stat().st_size < 1024 * 1024:
        raise ValueError("Rendered master is missing or invalid")
    if not 3.0 < duration_seconds < 3600.0 or cap_bytes < 8 * 1024 * 1024:
        raise ValueError("Invalid delivery duration or capacity")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    if source.stat().st_size <= cap_bytes:
        return source, {
            "delivery_reencoded": False,
            "source_master_sha256": source_sha,
            "delivery_sha256": source_sha,
            "delivery_bytes": source.stat().st_size,
            "delivery_qa": "original_master",
        }
    import json

    # Leave headroom for AAC and mux metadata; never send an oversized PUT.
    target = min(DELIVERY_TARGET, cap_bytes - 4 * 1024 * 1024)
    bitrate = max(260, int((target * 8 / duration_seconds / 1000) * 0.75))
    dest = source.with_name(source.stem + "-r2-delivery.mp4")
    for crf in (22, 25, 28):
        staged = dest.with_name("." + dest.stem + "-encoding.mp4")
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    str(crf),
                    "-maxrate",
                    str(bitrate) + "k",
                    "-bufsize",
                    str(bitrate * 2) + "k",
                    "-c:a",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(staged),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=1500,
            )
            if not staged.is_file() or staged.stat().st_size > target:
                continue
            inspection = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=codec_type,codec_name",
                    "-of",
                    "json",
                    str(staged),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=25,
            )
            info = json.loads(inspection.stdout)
            tracks = info["streams"]
            if abs(float(info["format"]["duration"]) - duration_seconds) > 0.25:
                raise ValueError("R2 transcode changed music duration")
            if not any(
                s.get("codec_type") == "video" and s.get("codec_name") == "h264"
                for s in tracks
            ):
                raise ValueError("R2 copy lacks video")
            if not any(s.get("codec_type") == "audio" for s in tracks):
                raise ValueError("R2 copy lacks audio")
            staged.replace(dest)
            return dest, {
                "delivery_reencoded": True,
                "delivery_crf": crf,
                "delivery_video_maxrate_kbps": bitrate,
                "source_master_sha256": source_sha,
                "delivery_sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
                "delivery_bytes": dest.stat().st_size,
                "delivery_qa": "additional_visual_review_required",
            }
        finally:
            staged.unlink(missing_ok=True)
    raise RuntimeError(
        "Cannot fit private delivery MP4 under the bounded R2 upload limit"
    )
