#!/usr/bin/env python3
"""Independent QA for a Modal-sourced WAV + 30–50 real-motion master.

A technical pass is never publication or narrative approval.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

from scripts.assemble_typebeat_project import _q, psql_exec, psql_rows

RENDER_ROOT = Path("/home/bobby/Videos/scene-director/modal-production")
PROJECT_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def audit_visual_reference_chain(scenes: list[dict]) -> dict:
    """Audit actual Adobe last-frame provenance recorded in the render manifest."""
    if not 30 <= len(scenes) <= 50:
        raise ValueError("Adobe reference chain requires 30-50 scenes")
    seen = set()
    previous = None
    for i, scene in enumerate(scenes, 1):
        state = scene.get("continuityState") or {}
        ref = state.get("video_reference") or {}
        incoming = ref.get("incoming")
        outgoing = ref.get("outgoing")
        digest = ref.get("final_frame_sha256")
        if ref.get("custody") != "adobe_storage_previous_scene_frame":
            raise ValueError(f"Missing Adobe last-frame custody for scene {i}")
        if not isinstance(outgoing, str) or len(outgoing) < 4 or outgoing in seen:
            raise ValueError(f"Invalid/duplicate Adobe outgoing reference at scene {i}")
        if not SHA256.fullmatch(str(digest or "")):
            raise ValueError(f"Missing Adobe final-frame SHA-256 at scene {i}")
        similarity = ref.get("entry_similarity")
        if i > 1 and (
            not isinstance(similarity, dict)
            or similarity.get("passed") is not True
            or float(similarity.get("luma_ssim") or 0) < 0.35
            or float(similarity.get("rgb_mae") or 1) > 0.20
        ):
            raise ValueError(
                f"Scene {i} failed actual first-frame reference continuity"
            )
        if (i == 1 and incoming not in (None, "")) or (i > 1 and incoming != previous):
            raise ValueError(f"Adobe visual continuity break at scene {i}")
        seen.add(outgoing)
        previous = outgoing
    return {
        "mode": "adobe_previous_last_frame",
        "scene_count": len(scenes),
        "verified_handoffs": len(scenes) - 1,
        "unique_references": len(seen),
    }


def verify(
    project_id: str, *, workspace: Path | None = None, update_database: bool = True
) -> dict:
    if not PROJECT_ID.fullmatch(project_id):
        raise ValueError("Invalid Modal project ID")
    root = (workspace if workspace is not None else RENDER_ROOT / project_id).resolve()
    plan = json.loads((root / "project.json").read_text())
    assigned = json.loads((root / "assignments.json").read_text())
    result = json.loads((root / "result.json").read_text())
    audit = json.loads((root / "qa" / "boundary-audit.json").read_text())
    if (
        plan.get("id") != project_id
        or result.get("project_id") != project_id
        or audit.get("project_id") != project_id
    ):
        raise ValueError("Project identity mismatch between plan/render/audit")
    if not plan.get("director", {}).get("motionOnly"):
        raise ValueError("Rendered project missing motion-only contract")
    scenes = plan.get("scenes") or []
    by_id = {a["id"]: a for a in plan.get("assets", [])}
    if not 30 <= len(scenes) <= 50 or len(assigned) != len(scenes):
        raise ValueError("All-motion scene count outside approved range")
    if len({a["sceneId"] for a in assigned}) != len(scenes):
        raise ValueError("Missing or repeated scene assignment")
    if {a["sceneId"] for a in assigned} != {s["id"] for s in scenes}:
        raise ValueError("Scene assignment mismatch")
    if any(
        a["kind"] != "video" or by_id.get(a["assetId"], {}).get("kind") != "video"
        for a in assigned
    ):
        raise ValueError("Found a still-image or missing motion scene")
    ref_mode = (plan.get("director") or {}).get("referenceChainMode")
    if ref_mode is not None and ref_mode != "adobe_previous_last_frame":
        raise ValueError("Unknown scene visual-reference contract")
    visual_chain = audit_visual_reference_chain(scenes) if ref_mode else None
    expected_clip = (plan.get("audioSource") or {}).get("clipId")
    if (
        not expected_clip
        or expected_clip != result.get("clip_id")
        or result.get("audio_volume") != "suno-playback-library"
    ):
        raise ValueError("Modal source WAV UUID/volume mismatch")
    expected_audio_sha = str((plan.get("audioSource") or {}).get("sha256") or "")
    if (
        not SHA256.fullmatch(expected_audio_sha)
        or result.get("source_wav_sha256") != expected_audio_sha
    ):
        raise ValueError("Modal source WAV digest mismatch")
    declared = Path(result["output_path"]).resolve()
    if (
        not declared.is_relative_to(root)
        or declared.suffix.lower() != ".mp4"
        or not declared.is_file()
    ):
        raise ValueError("Modal master output outside project workspace")
    digest = hashlib.sha256(declared.read_bytes()).hexdigest()
    if digest != result.get("output_sha256"):
        raise ValueError("Modal master SHA-256 differs from renderer output")
    if (
        result.get("status") != "TECHNICAL_PASS"
        or not result.get("all_motion")
        or result.get("publishing_approved") is not False
    ):
        raise ValueError("Missing technical PASS or unsafe publication state")
    if (
        int(audit.get("scene_count", -1)) != len(scenes)
        or int(audit.get("boundary_count", -1)) != len(scenes) - 1
    ):
        raise ValueError("Incomplete scene-boundary audit")
    if audit.get("video_file") != str(declared) or audit.get("contact_sheet") is None:
        raise ValueError("QA contact sheet/video mismatch")
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(declared),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    streams = json.loads(probe.stdout)
    kinds = {s.get("codec_type") for s in streams.get("streams", [])}
    if not {"video", "audio"} <= kinds:
        raise ValueError("Missing video or embedded WAV-derived audio")
    duration = float(streams.get("format", {}).get("duration") or 0)
    if abs(duration - float(plan["analysis"]["durationSec"])) > 0.15:
        raise ValueError("Rendered master duration differs from Modal WAV")
    record = (
        psql_rows(
            f"SELECT project_id,status FROM media_video_projects WHERE project_id={_q(project_id)} LIMIT 1;"
        )
        if update_database
        else [[project_id, "ASSETS_READY"]]
    )
    if not record or record[0][1] not in ("ASSETS_READY", "QA_REVIEW"):
        raise ValueError("Project database status disallows QA")
    metadata = {
        "render_engine": "modal-remotion-motion-only",
        "render_path": str(declared),
        "render_sha256": digest,
        "source_modal_wav_sha256": expected_audio_sha,
        "source_clip_id": expected_clip,
        "technical_qa": "PASS",
        "motion_qa": "PASS",
        "motion_scene_count": len(scenes),
        "image_scene_count": 0,
        "narrative_ready": bool(audit.get("narrative_ready")),
        "visual_reference_chain": visual_chain,
        "visual_review": "PENDING",
        "rights_review": "PENDING",
        "listening_review": "PENDING",
        "publishing_approved": False,
    }
    if update_database:
        sql = (
            "UPDATE media_video_projects SET status='QA_REVIEW', "
            "metadata=coalesce(metadata,'{}'::jsonb)||"
            + _q(json.dumps(metadata))
            + "::jsonb,updated_at=now() WHERE project_id="
            + _q(project_id)
            + " AND status IN ('ASSETS_READY','QA_REVIEW');"
        )
        psql_exec(sql)
    return {
        "success": True,
        "project_id": project_id,
        "technical_qa": "PASS",
        "motion_qa": "PASS",
        "scene_count": len(scenes),
        "video_scene_count": len(scenes),
        "image_scene_count": 0,
        "render_path": str(declared),
        "sha256": digest,
        "contact_sheet": audit["contact_sheet"],
        "narrative_ready": bool(audit.get("narrative_ready")),
        "visual_reference_chain": visual_chain,
        "visual_review": "PENDING",
        "rights_review": "PENDING",
        "listening_review": "PENDING",
        "publishing_approved": False,
        "render_engine": "modal-remotion-motion-only",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.project_id)))
