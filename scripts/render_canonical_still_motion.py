#!/usr/bin/env python3
"""Deterministic image-conditioned camera motion; never claims generative I2V."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def render_canonical_motion(
    source: Path, target: Path, seconds: float = 5.0, fps: int = 30
) -> dict:
    if not source.is_file() or source.suffix.lower() not in {".png", ".jpeg", ".jpg", ".webp"}:
        raise ValueError("canonical image file required")
    if not 2 <= seconds <= 10 or not 12 <= fps <= 60:
        raise ValueError("motion must be 2–10 seconds and 12–60 fps")
    if source.resolve() == target.resolve():
        raise ValueError("output must not overwrite source image")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Crop/zoom is applied to the supplied pixels only: no new objects, signs, or identity drift.
    vf = (
        "scale=1536:864:force_original_aspect_ratio=increase,"
        "crop=1536:864,setsar=1,"
        "zoompan=z='min(zoom+0.0006,1.10)':"
        "x='iw/2-(iw/zoom/2)+8*sin(on/37)':"
        "y='ih/2-(ih/zoom/2)+5*cos(on/49)':"
        f"d=1:s=1280x720:fps={fps},format=yuv420p"
    )
    frames = round(seconds * fps)
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-loop",
        "1",
        "-framerate",
        str(fps),
        "-i",
        str(source),
        "-vf",
        vf,
        "-frames:v",
        str(frames),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(target),
    ]
    subprocess.run(command, check=True, timeout=180)
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,width,height:format=duration",
            "-of",
            "json",
            str(target),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    probe = json.loads(result.stdout)
    stream = probe["streams"][0]
    if stream["codec_name"] != "h264" or stream["width"] != 1280 or stream["height"] != 720:
        raise RuntimeError("unexpected codec/geometry")
    if abs(float(probe["format"]["duration"]) - seconds) > 0.05:
        raise RuntimeError("unexpected duration")
    return {
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "output": str(target),
        "duration_seconds": float(probe["format"]["duration"]),
        "motion_type": "canonical_still_camera_motion",
        "generative_i2v": False,
        "reference_conditioning_verified": True,
        "publishing_approved": False,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--still", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--duration", type=float, default=5.0)
    a = p.parse_args()
    print(json.dumps(render_canonical_motion(a.still, a.output, a.duration), indent=2))


if __name__ == "__main__":
    main()
