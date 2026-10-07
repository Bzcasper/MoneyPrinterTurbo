#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import subprocess
import uuid
from pathlib import Path

DB_CONTAINER = os.environ.get("MUSIC_INTEL_DB_CONTAINER", "ai-postgres")
DB_NAME = os.environ.get("MUSIC_INTEL_DB_NAME", "music_intel")
DB_USER = os.environ.get("MUSIC_INTEL_DB_USER", "postgres")
REMOTE_HOST = os.environ.get("MUSIC_INTEL_REMOTE_HOST", "").strip()
MEDIA_HOST = os.environ.get("TYPEBEAT_REMOTE_MEDIA_HOST", REMOTE_HOST).strip()
REMOTE_MEDIA_ROOT = os.environ.get(
    "TYPEBEAT_REMOTE_PROJECT_ROOT", "/srv/data/n8n-media/store/strictlybeats"
).rstrip("/")


def _safe_id(value: str, label: str) -> str:
    value = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value):
        raise SystemExit(f"invalid {label}")
    return value


def _q(value: object) -> str:
    return "'" + str(value if value is not None else "").replace("'", "''") + "'"


def _remote_command(args: list[str], *, timeout: int = 120, capture: bool = True):
    if not REMOTE_HOST:
        cmd = args
    else:
        cmd = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            REMOTE_HOST, shlex.join(args),
        ]
    if capture:
        return subprocess.check_output(cmd, text=True, timeout=timeout)
    subprocess.run(cmd, check=True, timeout=timeout)
    return ""


def psql_rows(sql: str) -> list[list[str]]:
    raw = _remote_command([
        "docker", "exec", DB_CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME,
        "-At", "-F", "|", "-c", sql,
    ], timeout=60)
    return [line.split("|") for line in raw.splitlines() if line.strip()]


def psql_exec(sql: str) -> None:
    _remote_command([
        "docker", "exec", DB_CONTAINER, "psql", "-v", "ON_ERROR_STOP=1",
        "-U", DB_USER, "-d", DB_NAME, "-c", sql,
    ], timeout=120, capture=False)


def remote_file_exists(path: str) -> bool:
    if MEDIA_HOST:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", MEDIA_HOST,
             f"test -s {shlex.quote(path)}"],
            timeout=30,
            check=False,
        )
        return result.returncode == 0
    return Path(path).is_file() and Path(path).stat().st_size > 0


def remote_mkdir(path: str) -> None:
    if MEDIA_HOST:
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", MEDIA_HOST,
             f"mkdir -p {shlex.quote(path)}"],
            check=True, timeout=30,
        )
    else:
        Path(path).mkdir(parents=True, exist_ok=True)


def extract_frame(video_path: str, output_path: str, seek_seconds: float) -> None:
    args = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{seek_seconds:.3f}", "-i", video_path,
        "-frames:v", "1",
        "-vf", "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080",
        "-q:v", "2", output_path,
    ]
    if MEDIA_HOST:
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             MEDIA_HOST, shlex.join(args)],
            check=True, timeout=180,
        )
    else:
        subprocess.run(args, check=True, timeout=180)


def motion_ordinals(scene_count: int) -> list[int]:
    if scene_count == 33:
        return [3, 6, 9, 13, 16, 19, 23, 26, 29, 32]
    start, end = 2, max(2, scene_count - 1)
    vals = [round(start + i * (end - start) / 9) for i in range(10)]
    out: list[int] = []
    for value in vals:
        value = max(1, min(scene_count, value))
        while value in out and value < scene_count:
            value += 1
        while value in out and value > 1:
            value -= 1
        out.append(value)
    if len(set(out)) != 10:
        raise SystemExit("could not distribute 10 unique motion scenes")
    return sorted(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="Assemble a canonical type-beat project from 10 free motion clips")
    ap.add_argument("--payload", required=True)
    args = ap.parse_args()
    payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))

    clip_id = str(payload.get("clip_id") or "").strip()
    try:
        uuid.UUID(clip_id)
    except ValueError as exc:
        raise SystemExit("invalid clip_id") from exc

    title = str(payload.get("title") or "Untitled Type Beat").strip()[:200]
    scene_count = int(payload.get("scene_count") or 33)
    if not 30 <= scene_count <= 50:
        raise SystemExit("scene_count must be 30-50")
    motions = payload.get("motion_assets")
    if not isinstance(motions, list) or len(motions) != 10:
        raise SystemExit("exactly 10 motion_assets are required")

    project_id = _safe_id(
        payload.get("project_id") or f"mvbeat_{clip_id.split('-')[0]}_freevideo",
        "project_id",
    )
    source = psql_rows(
        "select a.duration_seconds,m.canonical_source_path "
        "from music_assets a join v_canonical_track_media m using (clip_id) "
        f"where a.clip_id={_q(clip_id)}::uuid and m.media_present=true limit 1;"
    )
    if not source:
        raise SystemExit("canonical beat not found")
    duration = float(source[0][0] or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise SystemExit("canonical beat has invalid duration")

    normalized_motions: list[dict] = []
    for index, item in enumerate(motions, start=1):
        if not isinstance(item, dict):
            raise SystemExit(f"motion asset {index} is invalid")
        path = str(item.get("host_path") or item.get("local_path") or "").strip()
        if not path or not remote_file_exists(path):
            raise SystemExit(f"motion asset {index} is missing: {path or '<empty>'}")
        normalized_motions.append({
            "path": path,
            "provider": str(item.get("provider") or "free-web-video")[:80],
            "model": str(item.get("model") or "unknown")[:120],
            "prompt": str(item.get("prompt") or "")[:6000],
        })

    motion_scene_ordinals = motion_ordinals(scene_count)
    motion_by_ordinal = dict(zip(motion_scene_ordinals, normalized_motions))
    project_root = f"{REMOTE_MEDIA_ROOT}/{project_id}"
    still_dir = f"{project_root}/stills"
    remote_mkdir(still_dir)

    image_paths: dict[int, tuple[str, int]] = {}
    for ordinal in range(1, scene_count + 1):
        if ordinal in motion_by_ordinal:
            continue
        nearest = min(motion_scene_ordinals, key=lambda x: abs(x - ordinal))
        source_video = motion_by_ordinal[nearest]["path"]
        output = f"{still_dir}/scene-{ordinal:03d}.jpg"
        seek = 0.8 + (ordinal % 4) * 0.55
        extract_frame(source_video, output, seek)
        if not remote_file_exists(output):
            raise SystemExit(f"derived still missing after extraction: {output}")
        image_paths[ordinal] = (output, nearest)

    style = str(payload.get("visual_style") or (
        "continuous abstract black-chrome signal city, cobalt and molten-gold light ribbons, "
        "smoked glass, reflective floor, volumetric haze, premium futuristic trap visualizer, "
        "no people, no text"
    ))[:4000]
    negative = "no people, no faces, no text, no logos, no watermark, no random style shift, no low-resolution artifacts"

    scene_rows: list[str] = []
    asset_rows: list[str] = []
    updates: list[str] = []
    motion_asset_ids: dict[int, str] = {}

    for ordinal in range(1, scene_count + 1):
        start = duration * (ordinal - 1) / scene_count
        end = duration * ordinal / scene_count
        sid = f"{project_id}-scene-{ordinal:03d}"
        is_motion = ordinal in motion_by_ordinal
        pref = "video" if is_motion else "image_motion"
        beat = f"Scene {ordinal:02d}/{scene_count}: the same signal city advances deeper; preserve palette, geometry and direction of travel."
        visual_prompt = f"{style}. {beat}"
        motion_prompt = (
            f"Forward cinematic motion continues from the prior scene; light ribbons pulse on the beat. {style}"
            if is_motion else
            "Subtle Ken Burns drift only; preserve continuity from the nearest motion frame."
        )
        scene_rows.append(
            "(" + ",".join([
                _q(sid), _q(project_id), str(ordinal), _q("type-beat"),
                _q(beat), f"{start:.6f}", f"{end:.6f}", _q("present"),
                _q("signal-city"), _q(pref), _q(visual_prompt), _q(motion_prompt),
                "'[]'::jsonb", "'{}'::jsonb", _q("PLANNED"),
                _q("abstract continuous visualizer"), "'{}'::jsonb", "'{}'::jsonb",
                _q("motion" if is_motion else "image_motion"), _q("free-only"),
                _q("high"), _q("high" if is_motion else "medium"), _q("high"),
                "true", _q("READY"), "2", _q(negative)
            ]) + ")"
        )

    # Insert project/scenes first, then assets, then lock references.
    sql = [
        "BEGIN;",
        f"DELETE FROM media_video_projects WHERE project_id={_q(project_id)};",
        "INSERT INTO media_video_projects "
        "(project_id,project_type,source_clip_id,title,status,commercial_gate_required,continuity_pack,visual_direction,metadata) VALUES "
        f"({_q(project_id)},'type_beat',{_q(clip_id)}::uuid,{_q(title)},'ASSETS_READY',false,"
        f"{_q(json.dumps({'mode': 'continuous_signal_city', 'identity': 'no_character'}))}::jsonb,"
        f"{_q(json.dumps({'style': style}))}::jsonb,"
        f"{_q(json.dumps({'provider_policy': 'free_only', 'required_motion_scenes': 10, 'scene_count': scene_count}))}::jsonb);",
        "INSERT INTO media_video_scenes "
        "(scene_id,project_id,ordinal,section_name,source_text,start_seconds,end_seconds,timeline_mode,"
        "location_key,asset_preference,visual_prompt,motion_prompt,continuity_refs,qa_requirements,status,"
        "director_brief,prompt_optimized,prompt_engine,scene_type,provider_strategy,continuity_priority,"
        "motion_priority,visual_priority,needs_anchor,scene_state,max_candidates,negative_constraints) VALUES "
        + ",".join(scene_rows) + ";",
    ]

    for ordinal in motion_scene_ordinals:
        sid = f"{project_id}-scene-{ordinal:03d}"
        aid = f"{project_id}-motion-{ordinal:03d}"
        motion_asset_ids[ordinal] = aid
        item = motion_by_ordinal[ordinal]
        metadata = json.dumps({"billing_mode": "free_only", "source": "n8n-free-video-router"})
        asset_rows.append(
            f"({_q(aid)},{_q(project_id)},{_q(sid)},'video',{_q(item['provider'])},{_q(item['model'])},"
            f"{_q(item['path'])},{_q(item['path'])},{_q(item['prompt'])},{_q(metadata)}::jsonb,"
            "'APPROVED','locked_video','APPROVED','LOCKED')"
        )
        updates.append(
            f"UPDATE media_video_scenes SET locked_video_asset_id={_q(aid)},anchor_asset_id=NULL,"
            f"scene_state='LOCKED' WHERE scene_id={_q(sid)};"
        )

    for ordinal, (path, parent_ordinal) in image_paths.items():
        sid = f"{project_id}-scene-{ordinal:03d}"
        aid = f"{project_id}-still-{ordinal:03d}"
        parent = motion_asset_ids[parent_ordinal]
        prompt = f"{style}. Derived continuity frame for scene {ordinal:02d}."
        metadata = json.dumps({"derived_from_motion_ordinal": parent_ordinal, "billing_mode": "free_only"})
        asset_rows.append(
            f"({_q(aid)},{_q(project_id)},{_q(sid)},'image','moneyprinter-frame-extract','ffmpeg-frame',"
            f"{_q(path)},{_q(path)},{_q(prompt)},{_q(metadata)}::jsonb,"
            f"'APPROVED','anchor','APPROVED','LOCKED')"
        )
        updates.append(
            f"UPDATE media_video_scenes SET anchor_asset_id={_q(aid)},fallback_asset_id=NULL,"
            f"scene_state='LOCKED' WHERE scene_id={_q(sid)};"
        )

    sql.append(
        "INSERT INTO media_video_assets "
        "(asset_id,project_id,scene_id,asset_type,provider,model,uri,local_path,prompt,metadata,status,asset_role,qa_status,lock_status) VALUES "
        + ",".join(asset_rows) + ";"
    )
    sql.extend(updates)
    sql.append("COMMIT;")
    psql_exec("\n".join(sql))

    print(json.dumps({
        "ok": True,
        "project_id": project_id,
        "clip_id": clip_id,
        "title": title,
        "duration_seconds": duration,
        "scene_count": scene_count,
        "motion_scene_count": len(motion_scene_ordinals),
        "image_scene_count": len(image_paths),
        "motion_ordinals": motion_scene_ordinals,
        "provider_policy": "free_only",
    }))


if __name__ == "__main__":
    main()
