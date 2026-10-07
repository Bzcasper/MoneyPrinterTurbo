#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.models.schema import VideoAspect, VideoConcatMode, VideoParams  # noqa: E402
from app.services import video  # noqa: E402

DB_CONTAINER = os.environ.get("MUSIC_INTEL_DB_CONTAINER", "ai-postgres")
DB_NAME = os.environ.get("MUSIC_INTEL_DB_NAME", "music_intel")
DB_USER = os.environ.get("MUSIC_INTEL_DB_USER", "postgres")
TYPEBEAT_OUTPUT_ROOT = Path(
    os.environ.get("TYPEBEAT_OUTPUT_ROOT", "/srv/data/n8n-media/store/strictlybeats")
)
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def psql_rows(sql: str) -> list[list[str]]:
    cmd = [
        "docker", "exec", DB_CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME,
        "-At", "-F", "|", "-c", sql,
    ]
    raw = subprocess.check_output(cmd, text=True)
    return [line.split("|") for line in raw.splitlines() if line.strip()]


def _asset_type(asset_type: str, path: str) -> str:
    normalized = (asset_type or "").strip().lower()
    if normalized in {"image", "video"}:
        return normalized
    suffix = Path(path).suffix.lower()
    if suffix in _IMAGE_EXTENSIONS:
        return "image"
    if suffix in _VIDEO_EXTENSIONS:
        return "video"
    raise SystemExit(f"unsupported scene asset type: {asset_type or suffix or 'unknown'} ({path})")


def _validate_scenes(scenes: list[dict]) -> list[dict]:
    if not scenes:
        raise SystemExit("no canonical scenes found")
    ordered = sorted(scenes, key=lambda item: int(item["ordinal"]))
    previous_end = None
    seen_ordinals: set[int] = set()
    for scene in ordered:
        ordinal = int(scene["ordinal"])
        if ordinal in seen_ordinals:
            raise SystemExit(f"duplicate scene ordinal: {ordinal}")
        seen_ordinals.add(ordinal)
        start = float(scene["start_seconds"])
        end = float(scene["end_seconds"])
        if not all(math.isfinite(value) for value in (start, end)) or end <= start:
            raise SystemExit(f"invalid scene timing at ordinal {ordinal}: {start}..{end}")
        if previous_end is None:
            if abs(start) > 0.05:
                raise SystemExit(f"canonical scene timeline must start at zero, got {start}")
        elif abs(start - previous_end) > 0.05:
            raise SystemExit(
                f"canonical scene timeline is not contiguous at ordinal {ordinal}: "
                f"expected {previous_end}, got {start}"
            )
        asset_path = str(scene.get("asset_path") or "").strip()
        if not asset_path or not Path(asset_path).is_file():
            raise SystemExit(f"scene asset is missing at ordinal {ordinal}: {asset_path or '<empty>'}")
        scene["asset_type"] = _asset_type(str(scene.get("asset_type") or ""), asset_path)
        scene["asset_path"] = asset_path
        scene["ordinal"] = ordinal
        scene["start_seconds"] = start
        scene["end_seconds"] = end
        previous_end = end
    return ordered


def _build_project(
    *, project_id: str, source_clip_id: str, title: str, audio_path: str,
    source_kind: str, scenes: list[dict],
) -> dict:
    scenes = _validate_scenes(scenes)
    if not Path(audio_path).is_file():
        raise SystemExit(f"source audio is missing: {audio_path}")
    cuts = [scenes[0]["start_seconds"]] + [scene["end_seconds"] for scene in scenes]
    return {
        "project_id": project_id,
        "source_clip_id": source_clip_id,
        "title": title,
        "audio_path": audio_path,
        "source_kind": source_kind,
        "cuts": cuts,
        "scenes": scenes,
        "assets": [scene["asset_path"] for scene in scenes],
    }


def load_project(project_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", project_id):
        raise SystemExit("invalid project id")
    pid = project_id.replace("'", "''")
    project = psql_rows(
        f"select p.source_clip_id,p.title,m.canonical_source_path,m.canonical_source_kind "
        f"from media_video_projects p join v_canonical_track_media m on m.clip_id=p.source_clip_id "
        f"where p.project_id='{pid}' limit 1;"
    )
    if not project:
        raise SystemExit(f"project not found or source media unavailable: {project_id}")
    source_clip_id, title, audio_path, source_kind = project[0]

    rows = psql_rows(
        f"select s.ordinal,s.start_seconds,s.end_seconds,"
        f"coalesce(v.asset_type,''),coalesce(v.local_path,''),"
        f"coalesce(a.asset_type,''),coalesce(a.local_path,''),"
        f"coalesce(fa.asset_type,''),coalesce(fa.local_path,'') "
        f"from media_video_scenes s "
        f"left join media_video_assets v on v.asset_id=s.locked_video_asset_id "
        f"left join media_video_assets a on a.asset_id=s.anchor_asset_id "
        f"left join media_video_assets fa on fa.asset_id=s.fallback_asset_id "
        f"where s.project_id='{pid}' order by s.ordinal;"
    )
    scenes: list[dict] = []
    for row in rows:
        if len(row) < 9:
            raise SystemExit(f"invalid scene row for project: {project_id}")
        ordinal, start, end = row[:3]
        candidates = (
            (row[3], row[4], "locked_video"),
            (row[5], row[6], "anchor"),
            (row[7], row[8], "fallback"),
        )
        selected = next(
            ((asset_type, asset_path, role) for asset_type, asset_path, role in candidates
             if asset_path and Path(asset_path).is_file()),
            None,
        )
        if not selected:
            raise SystemExit(
                f"no local canonical asset found for project {project_id} scene {ordinal}"
            )
        asset_type, asset_path, asset_role = selected
        scenes.append({
            "ordinal": int(ordinal),
            "start_seconds": float(start),
            "end_seconds": float(end),
            "asset_type": asset_type,
            "asset_path": asset_path,
            "asset_role": asset_role,
        })

    return _build_project(
        project_id=project_id,
        source_clip_id=source_clip_id,
        title=title,
        audio_path=audio_path,
        source_kind=source_kind,
        scenes=scenes,
    )


def load_manifest(path: str) -> dict:
    manifest_path = Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid render manifest: {manifest_path}: {exc}") from exc
    project_id = str(payload.get("project_id") or manifest_path.stem)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", project_id):
        raise SystemExit("invalid project id in manifest")
    scenes = payload.get("scenes")
    if not isinstance(scenes, list):
        raise SystemExit("render manifest requires a scenes array")
    return _build_project(
        project_id=project_id,
        source_clip_id=str(payload.get("source_clip_id") or ""),
        title=str(payload.get("title") or project_id),
        audio_path=str(payload.get("audio_path") or ""),
        source_kind=str(payload.get("source_kind") or "manifest"),
        scenes=[dict(scene) for scene in scenes if isinstance(scene, dict)],
    )


def normalize_audio(source: str, work_dir: Path) -> str:
    source_path = Path(source)
    if not source_path.is_file():
        raise SystemExit(f"source audio is missing: {source}")
    if source_path.suffix.lower() == ".wav":
        return str(source_path)

    output = work_dir / "source-audio.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source_path),
            "-vn", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(output),
        ],
        check=True,
        timeout=900,
    )
    return str(output)


def _render_image_motion(image_path: str, output: Path, duration: float, ordinal: int) -> str:
    width, height = VideoAspect.landscape.to_resolution()
    fps = 30
    # Deterministic, subtle Ken Burns movement. Direction alternates by scene so
    # still-image sections do not read as frozen slides while remaining stable.
    zoom_step = 0.00045 if ordinal % 2 else 0.00035
    x_expr = "iw/2-(iw/zoom/2)" if ordinal % 3 else "iw/2-(iw/zoom/2)+8*sin(on/45)"
    y_expr = "ih/2-(ih/zoom/2)" if ordinal % 4 else "ih/2-(ih/zoom/2)+6*cos(on/50)"
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},"
        f"zoompan=z='min(zoom+{zoom_step:.5f},1.08)':x='{x_expr}':y='{y_expr}':"
        f"d=1:s={width}x{height}:fps={fps},format=yuv420p"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-loop", "1", "-framerate", str(fps), "-i", image_path,
            "-vf", vf, "-t", f"{duration:.3f}", "-an", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(output),
        ],
        check=True,
        timeout=max(120, int(duration * 12)),
    )
    return str(output)


def materialize_scene_sources(project: dict, work_dir: Path) -> list[str]:
    sources: list[str] = []
    for scene in project["scenes"]:
        asset_path = scene["asset_path"]
        if scene["asset_type"] == "video":
            sources.append(asset_path)
            continue
        duration = scene["end_seconds"] - scene["start_seconds"]
        output = work_dir / f"scene-{scene['ordinal']:03d}-image-motion.mp4"
        sources.append(_render_image_motion(asset_path, output, duration, scene["ordinal"]))
    return sources


def render_project(project: dict, output: Path, *, threads: int = 4) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    work_dir = output.parent / f".{project['project_id']}-mpt"
    work_dir.mkdir(parents=True, exist_ok=True)

    audio = normalize_audio(project["audio_path"], work_dir)
    duration = float(project["cuts"][-1])
    cuts = video.validate_beat_cut_times(project["cuts"], duration)
    scene_sources = materialize_scene_sources(project, work_dir)
    if len(scene_sources) != len(cuts) - 1:
        raise SystemExit(
            f"scene/source mismatch: {len(scene_sources)} assets for {len(cuts) - 1} cuts"
        )
    combined = str(work_dir / "combined.mp4")

    video.combine_videos(
        combined_video_path=combined,
        video_paths=scene_sources,
        audio_file=audio,
        video_aspect=VideoAspect.landscape,
        video_concat_mode=VideoConcatMode.sequential,
        max_clip_duration=5,
        threads=threads,
        target_duration=duration,
        cut_times=cuts,
        strict_cut_order=True,
    )
    params = VideoParams(
        video_subject=project["title"] or project["project_id"],
        content_vertical="type_beat",
        beat_length_mode=True,
        beat_sync_cuts=True,
        beat_cut_times=cuts,
        voice_name="no-voice",
        video_aspect="16:9",
        video_concat_mode="sequential",
        subtitle_enabled=False,
        bgm_type="custom",
        bgm_file=audio,
        bgm_volume=1.0,
        voice_volume=0,
        n_threads=threads,
        beat_visual_effects=True,
        beat_overlay_cards=False,
    )
    bgm_ok = video.generate_video(
        combined, audio, "", str(output), params, bgm_file_override=""
    )
    if not bgm_ok or not output.is_file() or output.stat().st_size <= 0:
        raise SystemExit("MoneyPrinterTurbo final render did not produce a usable output")
    image_count = sum(scene["asset_type"] == "image" for scene in project["scenes"])
    video_count = sum(scene["asset_type"] == "video" for scene in project["scenes"])
    result = {
        **project,
        "audio_path": audio,
        "output": str(output),
        "duration_seconds": duration,
        "scene_count": len(cuts) - 1,
        "asset_count": len(scene_sources),
        "image_scene_count": image_count,
        "video_scene_count": video_count,
        "bgm_ok": bool(bgm_ok),
    }
    manifest = output.with_suffix(".manifest.json")
    manifest.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def _default_output(project_id: str) -> Path:
    return TYPEBEAT_OUTPUT_ROOT / project_id / "final" / "moneyprinterturbo-master.mp4"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render a canonical StrictlyBeats type-beat project"
    )
    parser.add_argument("project_id", nargs="?")
    parser.add_argument("--manifest", help="Render from a local canonical manifest instead of PostgreSQL")
    parser.add_argument("--audio", help="Override the canonical/manifest source audio path")
    parser.add_argument("--output")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()

    if bool(args.project_id) == bool(args.manifest):
        raise SystemExit("provide exactly one of project_id or --manifest")
    project = load_manifest(args.manifest) if args.manifest else load_project(args.project_id)
    if args.audio:
        if not Path(args.audio).is_file():
            raise SystemExit(f"source audio is missing: {args.audio}")
        project["audio_path"] = args.audio
        project["source_kind"] = "audio_override"
    output = Path(args.output) if args.output else _default_output(project["project_id"])

    plan = {
        **project,
        "output": str(output),
        "duration_seconds": project["cuts"][-1],
        "scene_count": len(project["cuts"]) - 1,
        "asset_count": len(project["assets"]),
        "image_scene_count": sum(scene["asset_type"] == "image" for scene in project["scenes"]),
        "video_scene_count": sum(scene["asset_type"] == "video" for scene in project["scenes"]),
    }
    if args.plan_only:
        print(json.dumps(plan, indent=2))
        return

    lock_path = output.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit(f"render already in progress: {project['project_id']}") from exc
        result = render_project(project, output, threads=max(1, args.threads))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
