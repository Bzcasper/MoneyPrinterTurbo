"""Persistent reference-frame custody for sequential Firefly-generated motion scenes."""

from __future__ import annotations

import fcntl
import io
import shlex
import subprocess
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

from scripts.extract_firefly_tail_frame import extract_last_frame

ROOT = Path("/home/bobby/Videos/scene-director/reference-frames")
STORAGE_URL = "http://10.0.0.242:5678/webhook/firefly-storage-upload"
PROJECT = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
ADOBE_ID = re.compile(r"^[A-Za-z0-9._:-]{4,128}$")


def valid(project_id: str, slot: int) -> None:
    if (
        not PROJECT.fullmatch(str(project_id))
        or not isinstance(slot, int)
        or not 1 <= slot <= 50
    ):
        raise ValueError("Invalid project or scene ordinal")


def directory(root: Path, project_id: str) -> Path:
    if not PROJECT.fullmatch(project_id):
        raise ValueError("Invalid project ID")
    target = (root.resolve() / project_id).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("Unsafe project reference path")
    target.mkdir(parents=True, exist_ok=True)
    return target


def record_path(root: Path, project_id: str, slot: int) -> Path:
    valid(project_id, slot)
    return directory(root, project_id) / f"scene-{slot:03d}-reference.json"


def resolve_previous(
    project_id: str, slot: int, prompt: str, duration: float, root: Path = ROOT
) -> dict:
    valid(project_id, slot)
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 1490:
        raise ValueError("Missing approved scene prompt")
    if not 5 <= float(duration) <= 10:
        raise ValueError("Firefly motion scene must be 5–10 seconds")
    previous = None
    if slot > 1:
        path = record_path(root, project_id, slot - 1)
        if not path.is_file():
            raise ValueError(
                f"Previous scene {slot - 1} lacks persisted Adobe visual reference"
            )
        previous = json.loads(path.read_text())
        if previous.get("slot") != slot - 1 or not ADOBE_ID.fullmatch(
            str(previous.get("reference_image_id") or "")
        ):
            raise ValueError("Previous Adobe visual-reference custody invalid")
    return {
        "project_id": project_id,
        "slot": slot,
        "prompt": prompt,
        "duration": float(duration),
        "reference_image_id": previous["reference_image_id"] if previous else None,
        "reference_from_slot": slot - 1 if previous else None,
        "reference_frame_sha256": previous.get("frame_sha256") if previous else None,
        "visual_reference_required": slot > 1,
    }


def upload_adobe(frame: dict, url: str = STORAGE_URL) -> str:
    data = json.dumps(
        {
            "kind": "image",
            "fileName": f"scene-{frame['slot']:03d}-final.jpg",
            "mimeType": "image/jpeg",
            "base64": frame["base64"],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        if response.status != 200:
            raise ValueError(f"Adobe reference upload HTTP {response.status}")
        payload = json.load(response)
    if payload.get("success") is not True or payload.get("statusCode") != 200:
        raise ValueError("Adobe reference storage failed")
    images = (payload.get("response") or {}).get("images") or []
    ident = str(images[0].get("id") or "") if images else ""
    if not ADOBE_ID.fullmatch(ident):
        raise ValueError("Adobe storage did not return a durable image asset ID")
    return ident


def audit_video_entry(previous_frame: Path, source_video: str) -> dict:
    """Compare actual first moving frame against previous scene last frame.

    A reference asset ID alone is not proof of image conditioning.
    """
    from scripts.extract_firefly_tail_frame import MEDIA, NUC

    if not MEDIA.fullmatch(source_video):
        raise ValueError("Invalid Firefly generated video for visual continuity QA")
    if not previous_frame.is_file():
        raise ValueError("Previous scene's visual reference JPEG is missing")
    args = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        source_video,
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-c:v",
        "mjpeg",
        "pipe:1",
    ]
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", NUC, shlex.join(args)],
        capture_output=True,
        check=True,
        timeout=60,
    )

    def pixels(data: bytes) -> np.ndarray:
        with Image.open(io.BytesIO(data)) as im:
            rgb = im.convert("RGB").resize((160, 90), Image.Resampling.BILINEAR)
            return np.asarray(rgb, dtype=np.float64) / 255.0

    anchor = pixels(previous_frame.read_bytes())
    first = pixels(result.stdout)
    delta = float(np.abs(anchor - first).mean())
    a, b = anchor.mean(axis=2), first.mean(axis=2)
    mu_a, mu_b = float(a.mean()), float(b.mean())
    variance_a, variance_b = float(a.var()), float(b.var())
    covariance = float(((a - mu_a) * (b - mu_b)).mean())
    similarity = float(
        ((2 * mu_a * mu_b + 0.0001) * (2 * covariance + 0.0009))
        / ((mu_a * mu_a + mu_b * mu_b + 0.0001) * (variance_a + variance_b + 0.0009))
    )
    passed = similarity >= 0.35 and delta <= 0.20
    return {
        "passed": passed,
        "luma_ssim": round(similarity, 6),
        "rgb_mae": round(delta, 6),
        "threshold_ssim": 0.35,
        "threshold_mae": 0.20,
    }


def register_scene(
    project_id: str,
    scene_result: dict,
    root: Path = ROOT,
    extract=extract_last_frame,
    upload=upload_adobe,
    audit=audit_video_entry,
) -> dict:
    if not isinstance(scene_result, dict):
        raise ValueError("Missing Firefly scene result")
    slot = scene_result.get("slot")
    valid(project_id, slot)
    if not (
        scene_result.get("success") is True
        and scene_result.get("provider") == "firefly"
        and scene_result.get("model") == "firefly-video"
        and scene_result.get("firefly_fair_use") is True
        and scene_result.get("credit_spending_allowed") is False
    ):
        raise ValueError("Scene failed strict generated-video / fair-use approval")
    source = str(scene_result.get("host_path") or "")
    path = record_path(root, project_id, slot)
    directory(root, project_id).mkdir(parents=True, exist_ok=True)
    lock_path = directory(root, project_id) / ".custody.lock"
    with lock_path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if path.is_file():
            prior = json.loads(path.read_text())
            if prior.get("source_host_path") != source:
                raise ValueError("Scene reference already committed to another video")
            row = prior
        else:
            match = resolve_previous(
                project_id,
                slot,
                scene_result.get("prompt") or "",
                float(scene_result.get("duration") or 5),
                root,
            )
            requested = str(scene_result.get("reference_image_id") or "")
            if slot > 1:
                if (
                    requested != match["reference_image_id"]
                    or scene_result.get("reference_requested") is not True
                ):
                    raise ValueError(
                        "Generated video omitted the exact previous Adobe reference"
                    )
            elif requested or scene_result.get("reference_requested") is True:
                raise ValueError(
                    "First scene may not inherit another project's visual reference"
                )
            similarity = None
            if slot > 1:
                prior_frame = (
                    directory(root, project_id) / f"scene-{slot - 1:03d}-out.jpg"
                )
                similarity = audit(prior_frame, source)
                if similarity.get("passed") is not True:
                    raise ValueError(
                        f"Adobe first-frame continuity rejected scene {slot}; "
                        f"SSIM={similarity.get('luma_ssim')}, RGB MAE={similarity.get('rgb_mae')}"
                    )
            frame = extract(source, project_id, slot, root)
            ident = upload(frame)
            row = {
                "project_id": project_id,
                "slot": slot,
                "source_host_path": source,
                "reference_image_id": ident,
                "frame_sha256": frame["sha256"],
                "frame_bytes": frame["bytes"],
                "input_reference_image_id": requested or None,
                "entry_similarity": similarity,
            }
            with tempfile.NamedTemporaryFile(
                "w",
                dir=path.parent,
                prefix=path.name + ".",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp.write(json.dumps(row, indent=2) + "\n")
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp_path = Path(tmp.name)
            os.replace(tmp_path, path)
    return {
        "success": True,
        "project_id": project_id,
        "slot": slot,
        "scene_result": {
            **scene_result,
            "next_reference_image_id": row["reference_image_id"],
            "reference_frame_sha256": row["frame_sha256"],
            "reference_custody": "adobe_storage_previous_scene_frame",
            "reference_similarity": row.get("entry_similarity"),
        },
        "reference_ready": True,
        "publishing_approved": False,
    }
