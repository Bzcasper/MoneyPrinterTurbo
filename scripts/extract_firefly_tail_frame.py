#!/usr/bin/env python3
"""Extract a previous approved Firefly MP4's final moving frame for Adobe reference locking."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shlex
import subprocess
from pathlib import Path

NUC = "bobby-nuc"
MEDIA = re.compile(
    r"^/srv/data/n8n-media/store/firefly/videos/fullmotion-[A-Za-z0-9._-]+\.mp4$"
)
PROJECT = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def extract_last_frame(
    path: str, project_id: str, slot: int, output_root: Path
) -> dict:
    if not MEDIA.fullmatch(path):
        raise ValueError(
            "Only approved fullmotion-NUMBER-scene-NUMBER.mp4 sources are eligible"
        )
    if not PROJECT.fullmatch(project_id) or not 1 <= slot <= 50:
        raise ValueError("Invalid project ID or scene slot")
    args = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-sseof",
        "-0.25",
        "-i",
        path,
        "-frames:v",
        "1",
        "-vf",
        "scale=1280:-2",
        "-q:v",
        "3",
        "-f",
        "image2pipe",
        "-c:v",
        "mjpeg",
        "pipe:1",
    ]
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        NUC,
        shlex.join(args),
    ]
    process = subprocess.run(cmd, capture_output=True, timeout=60, check=True)
    frame = process.stdout
    if not frame.startswith(b"\xff\xd8\xff") or not frame.endswith(b"\xff\xd9"):
        raise ValueError("Firefly clip has no valid end-of-shot JPEG reference")
    if not 1024 <= len(frame) <= 4 * 1024 * 1024:
        raise ValueError("Firefly reference JPEG outside size bounds")
    root = output_root.resolve() / project_id
    root.mkdir(parents=True, exist_ok=True)
    dest = root / f"scene-{slot:03d}-out.jpg"
    dest.write_bytes(frame)
    checksum = hashlib.sha256(frame).hexdigest()
    return {
        "status": "REFERENCE_READY",
        "project_id": project_id,
        "slot": slot,
        "source_host_path": path,
        "file_path": str(dest),
        "mime_type": "image/jpeg",
        "bytes": len(frame),
        "sha256": checksum,
        "base64": base64.b64encode(frame).decode("ascii"),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--video", required=True)
    p.add_argument("--project-id", required=True)
    p.add_argument("--slot", type=int, required=True)
    p.add_argument(
        "--out-root",
        type=Path,
        default=Path("/home/bobby/Videos/scene-director/reference-frames"),
    )
    p.add_argument("--metadata-only", action="store_true")
    args = p.parse_args()
    row = extract_last_frame(args.video, args.project_id, args.slot, args.out_root)
    if args.metadata_only:
        row.pop("base64")
    print(json.dumps(row))


if __name__ == "__main__":
    main()
