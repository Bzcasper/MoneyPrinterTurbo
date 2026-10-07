#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from app.models.schema import VideoAspect, VideoConcatMode, VideoParams
from app.services import video

DB_CONTAINER = os.environ.get("MUSIC_INTEL_DB_CONTAINER", "ai-postgres")
DB_NAME = os.environ.get("MUSIC_INTEL_DB_NAME", "music_intel")
DB_USER = os.environ.get("MUSIC_INTEL_DB_USER", "postgres")


def psql_rows(sql: str) -> list[list[str]]:
    cmd = ["docker", "exec", DB_CONTAINER, "psql", "-U", DB_USER, "-d", DB_NAME, "-At", "-F", "|", "-c", sql]
    raw = subprocess.check_output(cmd, text=True)
    return [line.split("|") for line in raw.splitlines() if line.strip()]


def load_project(project_id: str) -> dict:
    pid = project_id.replace("'", "''")
    project = psql_rows(
        f"select p.source_clip_id,p.title,m.canonical_source_path,m.canonical_source_kind "
        f"from media_video_projects p join v_canonical_track_media m on m.clip_id=p.source_clip_id "
        f"where p.project_id='{pid}' limit 1;"
    )
    if not project:
        raise SystemExit(f"project not found or source media unavailable: {project_id}")
    source_clip_id, title, audio_path, source_kind = project[0]

    scene_rows = psql_rows(
        f"select ordinal,start_seconds,end_seconds from media_video_scenes "
        f"where project_id='{pid}' order by ordinal;"
    )
    if not scene_rows:
        raise SystemExit(f"no canonical scenes found for project: {project_id}")


    asset_rows = psql_rows(
        f"select s.ordinal,coalesce(a.local_path,'') from media_video_scenes s "
        f"left join media_video_assets a on a.asset_id=s.locked_video_asset_id "
        f"where s.project_id='{pid}' and a.local_path is not null order by s.ordinal;"
    )
    assets = [row[1] for row in asset_rows if len(row) > 1 and row[1] and Path(row[1]).is_file()]
    if not assets:
        raise SystemExit(f"no locked local motion assets found for project: {project_id}")

    cuts = [float(scene_rows[0][1])] + [float(row[2]) for row in scene_rows]
    return {
        "project_id": project_id,
        "source_clip_id": source_clip_id,
        "title": title,
        "audio_path": audio_path,
        "source_kind": source_kind,
        "cuts": cuts,
        "assets": assets,
    }


def normalize_audio(source: str, work_dir: Path) -> str:
    source_path = Path(source)
    if not source_path.is_file():
        raise SystemExit(f"source audio is missing: {source}")
    if source_path.suffix.lower() == ".wav":
        return str(source_path)

    output = work_dir / "source-audio.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source_path),
         "-vn", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(output)],
        check=True,
    )
    return str(output)


def render_project(project: dict, output: Path, *, threads: int = 4) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    work_dir = output.parent / f".{project['project_id']}-mpt"
    work_dir.mkdir(parents=True, exist_ok=True)

    audio = normalize_audio(project["audio_path"], work_dir)
    duration = project["cuts"][-1]
    cuts = video.validate_beat_cut_times(project["cuts"], duration)
    combined = str(work_dir / "combined.mp4")


    video.combine_videos(
        combined_video_path=combined,
        video_paths=project["assets"],
        audio_file=audio,
        video_aspect=VideoAspect.landscape,
        video_concat_mode=VideoConcatMode.sequential,
        max_clip_duration=5,
        threads=threads,
        target_duration=duration,
        cut_times=cuts,
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

        bgm_volume=0.5,
        voice_volume=0,
        n_threads=threads,
        beat_visual_effects=True,
        beat_overlay_cards=False,
    )
    bgm_ok = video.generate_video(combined, audio, "", str(output), params, bgm_file_override="")
    result = {
        **project,
        "audio_path": audio,
        "output": str(output),
        "duration_seconds": duration,
        "scene_count": len(cuts) - 1,
        "asset_count": len(project["assets"]),
        "bgm_ok": bool(bgm_ok),
    }
    manifest = output.with_suffix(".manifest.json")
    manifest.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a canonical StrictlyBeats type-beat project")
    parser.add_argument("project_id")
    parser.add_argument("--output")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()

    project = load_project(args.project_id)
    default_output = Path("/srv/data/n8n-media/store/strictlybeats") / args.project_id / "final" / "moneyprinterturbo-master.mp4"
    output = Path(args.output) if args.output else default_output

    plan = {
        **project,
        "output": str(output),
        "duration_seconds": project["cuts"][-1],
        "scene_count": len(project["cuts"]) - 1,
        "asset_count": len(project["assets"]),
    }
    if args.plan_only:
        print(json.dumps(plan, indent=2))
        return

    result = render_project(project, output, threads=max(1, args.threads))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
