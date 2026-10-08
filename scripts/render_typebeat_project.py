#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
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
TYPEBEAT_MIN_SCENES = int(os.environ.get("TYPEBEAT_MIN_SCENES", "30"))
TYPEBEAT_MAX_SCENES = int(os.environ.get("TYPEBEAT_MAX_SCENES", "50"))
TYPEBEAT_REQUIRED_MOTION_SCENES = int(
    os.environ.get("TYPEBEAT_REQUIRED_MOTION_SCENES", "10")
)
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def _remote_db_host() -> str:
    return os.environ.get("MUSIC_INTEL_REMOTE_HOST", "").strip()


def _remote_media_host() -> str:
    return os.environ.get("TYPEBEAT_REMOTE_MEDIA_HOST", _remote_db_host()).strip()


def _remote_cache_root() -> Path:
    return Path(
        os.environ.get(
            "TYPEBEAT_REMOTE_CACHE_ROOT",
            str(Path.home() / ".cache" / "moneyprinterturbo" / "typebeat-remote"),
        )
    )


def psql_rows(sql: str) -> list[list[str]]:
    cmd = [
        "docker", "exec", DB_CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME,
        "-At", "-F", "|", "-c", sql,
    ]
    remote_host = _remote_db_host()
    if remote_host:
        cmd = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            remote_host, shlex.join(cmd),
        ]
    raw = subprocess.check_output(cmd, text=True, timeout=60)
    return [line.split("|") for line in raw.splitlines() if line.strip()]


def _materialize_remote_path(source: str, project_id: str, label: str) -> str:
    source = str(source or "").strip()
    if not source:
        return source
    path = Path(source)
    if path.is_file():
        return str(path)
    remote_host = _remote_media_host()
    if not remote_host:
        return source

    safe_project = re.sub(r"[^A-Za-z0-9._-]+", "_", project_id)[:128] or "project"
    safe_label = re.sub(r"[^A-Za-z0-9._-]+", "_", label)[:96] or "asset"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    suffix = path.suffix.lower()
    target_dir = _remote_cache_root() / safe_project
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{safe_label}-{digest}{suffix}"
    if target.is_file() and target.stat().st_size > 0:
        return str(target)

    tmp = target.with_name(target.name + f".tmp-{os.getpid()}")
    try:
        subprocess.run(
            [
                "scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                f"{remote_host}:{source}", str(tmp),
            ],
            check=True,
            timeout=900,
        )
        if not tmp.is_file() or tmp.stat().st_size <= 0:
            raise OSError(f"empty remote media copy: {source}")
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return str(target)


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
    try:
        audio_path = _materialize_remote_path(audio_path, project_id, "source-audio")
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"failed to materialize source audio for {project_id}: {exc}") from exc

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
        selected = None
        errors: list[str] = []
        for asset_type, candidate_path, role in candidates:
            if not candidate_path:
                continue
            try:
                local_path = _materialize_remote_path(
                    candidate_path, project_id, f"scene-{int(ordinal):03d}-{role}"
                )
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(f"{role}: {exc}")
                continue
            if local_path and Path(local_path).is_file():
                selected = (asset_type, local_path, role)
                break
        if not selected:
            detail = f" ({'; '.join(errors)})" if errors else ""
            raise SystemExit(
                f"no local canonical asset found for project {project_id} scene {ordinal}{detail}"
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


def _render_image_motion(
    image_path: str, output: Path, duration: float, ordinal: int, *, threads: int = 4
) -> str:
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
            "-r", str(fps), "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
            "-profile:v", "high", "-level:v", "4.2", "-video_track_timescale", "90000",
            "-threads", str(max(1, threads)), "-movflags", "+faststart", str(output),
        ],
        check=True,
        timeout=max(120, int(duration * 12)),
    )
    return str(output)


def _normalize_video_scene(
    video_path: str, output: Path, duration: float, *, threads: int = 4
) -> str:
    width, height = VideoAspect.landscape.to_resolution()
    fps = 30
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},fps={fps},format=yuv420p,setpts=PTS-STARTPTS"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-stream_loop", "-1", "-i", video_path, "-an", "-vf", vf,
            "-t", f"{duration:.3f}", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(fps),
            "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
            "-profile:v", "high", "-level:v", "4.2", "-video_track_timescale", "90000",
            "-threads", str(max(1, threads)), "-movflags", "+faststart", str(output),
        ],
        check=True,
        timeout=max(120, int(duration * 12)),
    )
    return str(output)


def materialize_scene_sources(
    project: dict, work_dir: Path, *, threads: int = 4, normalize_video: bool = False
) -> list[str]:
    sources: list[str] = []
    for scene in project["scenes"]:
        asset_path = scene["asset_path"]
        duration = scene["end_seconds"] - scene["start_seconds"]
        if scene["asset_type"] == "video":
            if not normalize_video:
                sources.append(asset_path)
                continue
            output = work_dir / f"scene-{scene['ordinal']:03d}-video-motion.mp4"
            sources.append(
                _normalize_video_scene(asset_path, output, duration, threads=threads)
            )
            continue
        output = work_dir / f"scene-{scene['ordinal']:03d}-image-motion.mp4"
        sources.append(
            _render_image_motion(
                asset_path, output, duration, scene["ordinal"], threads=threads
            )
        )
    return sources


def _concat_normalized_scenes(scene_sources: list[str], output: Path, duration: float) -> None:
    concat_file = output.with_suffix(".concat.txt")
    lines = []
    for source in scene_sources:
        source_path = Path(source).resolve()
        if "'" in str(source_path) or "\n" in str(source_path):
            raise SystemExit(f"unsupported scene path for concat: {source_path}")
        lines.append(f"file '{source_path}'")
    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    staged = output.with_name(f".{output.name}.staged.mp4")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(concat_file),
                "-c", "copy", "-fflags", "+genpts", "-t", f"{duration:.3f}",
                "-movflags", "+faststart", str(staged),
            ],
            check=True,
            timeout=max(300, int(duration * 4)),
        )
        if not staged.is_file() or staged.stat().st_size <= 0:
            raise SystemExit("fast concat did not produce a usable output")
        os.replace(staged, output)
    finally:
        concat_file.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)


def _mux_master_audio(combined: Path, audio: str, output: Path, duration: float) -> None:
    staged = output.with_name(f".{output.name}.staged.mp4")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(combined), "-i", audio,
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                "-t", f"{duration:.3f}", "-shortest", "-movflags", "+faststart",
                str(staged),
            ],
            check=True,
            timeout=max(300, int(duration * 4)),
        )
        if not staged.is_file() or staged.stat().st_size <= 0:
            raise SystemExit("fast audio mux did not produce a usable output")
        os.replace(staged, output)
    finally:
        staged.unlink(missing_ok=True)


def validate_production_shape(project: dict) -> None:
    scene_count = len(project["scenes"])
    motion_count = sum(scene["asset_type"] == "video" for scene in project["scenes"])
    if not TYPEBEAT_MIN_SCENES <= scene_count <= TYPEBEAT_MAX_SCENES:
        raise SystemExit(
            f"production type-beat requires {TYPEBEAT_MIN_SCENES}-{TYPEBEAT_MAX_SCENES} scenes, "
            f"got {scene_count}"
        )
    if motion_count != TYPEBEAT_REQUIRED_MOTION_SCENES:
        raise SystemExit(
            f"production type-beat requires exactly {TYPEBEAT_REQUIRED_MOTION_SCENES} motion scenes, "
            f"got {motion_count}"
        )


def _render_result(
    project: dict, output: Path, audio: str, duration: float, cuts: list[float],
    scene_sources: list[str], *, render_mode: str, bgm_ok: bool = True,
) -> dict:
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
        "render_mode": render_mode,
        "bgm_ok": bool(bgm_ok),
    }
    manifest = output.with_suffix(".manifest.json")
    manifest.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def _render_project_legacy(
    project: dict, output: Path, work_dir: Path, audio: str, duration: float,
    cuts: list[float], *, threads: int,
) -> dict:
    scene_sources = materialize_scene_sources(project, work_dir, threads=threads)
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
    return _render_result(
        project, output, audio, duration, cuts, scene_sources,
        render_mode="legacy", bgm_ok=bool(bgm_ok),
    )


def _render_project_fast(
    project: dict, output: Path, work_dir: Path, audio: str, duration: float,
    cuts: list[float], *, threads: int,
) -> dict:
    scene_sources = materialize_scene_sources(
        project, work_dir, threads=threads, normalize_video=True
    )
    if len(scene_sources) != len(cuts) - 1:
        raise SystemExit(
            f"scene/source mismatch: {len(scene_sources)} assets for {len(cuts) - 1} cuts"
        )
    combined = work_dir / "combined-fast.mp4"
    _concat_normalized_scenes(scene_sources, combined, duration)
    _mux_master_audio(combined, audio, output, duration)
    return _render_result(
        project, output, audio, duration, cuts, scene_sources, render_mode="fast"
    )


OFFICIAL_PRODUCER_TAG_REMOTE = "/mnt/NUC_BACKUP/content-creation/producer-tags/bc-you-nasty-official-v1/BC-you-nasty-OFFICIAL.wav"
OFFICIAL_PRODUCER_TAG_SHA256 = "05818907ec2d762343819960101785022367e13172b1aa1b29653450b20d6b5b"


def tag_preview_audio(audio: str, project_id: str, work_dir: Path) -> str:
    """Mix verified producer ID once at t=0, preserving the clean library master."""
    tag = _materialize_remote_path(OFFICIAL_PRODUCER_TAG_REMOTE, project_id, "official-bc-producer-tag")
    if not Path(tag).is_file():
        raise RuntimeError("official producer tag WAV is unavailable")
    digest = hashlib.sha256(Path(tag).read_bytes()).hexdigest()
    if digest != OFFICIAL_PRODUCER_TAG_SHA256:
        raise RuntimeError("official producer tag integrity mismatch")
    output = work_dir / "tagged-preview.wav"
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-i", str(audio), "-i", str(tag),
        "-filter_complex", "[1:a]volume=0.55[tag];[0:a][tag]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95[out]",
        "-map", "[out]", "-c:a", "pcm_s24le", str(output),
    ], check=True, timeout=180)
    if not output.is_file() or output.stat().st_size < 100000:
        raise RuntimeError("tagged audio was not created")
    return str(output)


def render_project(
    project: dict, output: Path, *, threads: int = 4, render_mode: str | None = None
) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    work_dir = output.parent / f".{project['project_id']}-mpt"
    work_dir.mkdir(parents=True, exist_ok=True)

    audio = normalize_audio(project["audio_path"], work_dir)
    # Type-beat public previews require the verified BC intro tag; source masters remain untouched.
    if project.get("source_clip_id") and project.get("project_id"):
        audio = tag_preview_audio(audio, project["project_id"], work_dir)
    duration = float(project["cuts"][-1])
    cuts = video.validate_beat_cut_times(project["cuts"], duration)
    mode = (render_mode or os.environ.get("TYPEBEAT_RENDER_MODE", "fast")).strip().lower()
    if mode == "legacy":
        return _render_project_legacy(
            project, output, work_dir, audio, duration, cuts, threads=threads
        )
    if mode == "fast":
        return _render_project_fast(
            project, output, work_dir, audio, duration, cuts, threads=threads
        )
    raise SystemExit(f"unsupported type-beat render mode: {mode}")


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
    parser.add_argument(
        "--render-mode", choices=("fast", "legacy"),
        default=os.environ.get("TYPEBEAT_RENDER_MODE", "fast"),
    )
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()

    if bool(args.project_id) == bool(args.manifest):
        raise SystemExit("provide exactly one of project_id or --manifest")
    project = load_manifest(args.manifest) if args.manifest else load_project(args.project_id)
    if not args.manifest:
        validate_production_shape(project)
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
        result = render_project(
            project, output, threads=max(1, args.threads), render_mode=args.render_mode
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
