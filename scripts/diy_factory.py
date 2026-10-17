#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DIY Longform Factory runner (planning + native render).

Replaces the n8n "DIY Longform Factory" render hop: keeps chapter/scene
planning and run logging, renders with MoneyPrinterTurbo's own
``video_project`` pipeline (no Modal), and can emit an OpenMontage scaffold +
shorts plan to multiply output.

Planning is LLM-backed via ``--llm`` (the repo's configured provider). With no
provider available, pass ``--demo`` to plan from a built-in fixture so the whole
plan -> gate -> render chain can be exercised offline with a tiny synthetic
clip. Prepared per-scene media (image clip + narration audio) is supplied by
``--media-dir``; in demo mode a single generated still clip is reused.

Examples
--------
# Offline smoke test of the full chain (no provider, no network):
python -m scripts.diy_factory --demo --topic "Build a birdhouse" \
    --vertical diy --project-dir storage/projects/birdhouse

# Real run using the configured LLM + your prepared scene media:
python -m scripts.diy_factory --topic "Build a birdhouse" --vertical diy \
    --llm oneapi --media-dir /path/to/prepared/scenes \
    --project-dir storage/projects/birdhouse --openmontage-root /home/bobby/projects/OpenMontage
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from app.services import diy_factory, openmontage_bridge


def _demo_generate(prompt: str) -> str:
    """Deterministic offline planner used by --demo (no provider, no network)."""
    if "<Task>Write a structured long-form plan" in prompt:
        return json.dumps(
            {
                "hook": "Build it in one afternoon.",
                "optimized_brief": "Result-first, one action per scene.",
                "pacing_notes": "calm and clear",
                "materials_list": ["board", "saw", "nails"],
                "visual_bible": {"color_palette": "muted blues and warm wood tones"},
                "chapters": [
                    {"chapter_number": 1, "title": "Materials", "target_seconds": 60},
                    {"chapter_number": 2, "title": "Assembly", "target_seconds": 90},
                ],
            }
        )
    title = "Materials" if "Materials" in prompt else "Assembly"
    return json.dumps(
        {
            "chapter_title": title,
            "scenes": [
                {
                    "scene_number": i,
                    "narration": f"{title}: step {i} narrated clearly.",
                    "image_prompt": f"clean instructional still of {title.lower()} step {i}",
                    "duration_hint_seconds": 5,
                }
                for i in range(1, 4)
            ],
        }
    )


def _make_demo_clip(path: Path, seconds: float = 5.0) -> str:
    """Create a tiny silent color clip with ffmpeg for --demo rendering."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi", "-i",
            f"color=c=0x223344:s=1920x1080:d={seconds}:r=30",
            "-pix_fmt", "yuv420p", str(path),
        ],
        check=True, capture_output=True,
    )
    return str(path)


def _make_demo_audio(path: Path, seconds: float = 5.0) -> str:
    """Create a silent audio clip for demo scenes that carry narration."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
         "-t", f"{seconds}", "-c:a", "aac", str(path)],
        check=True, capture_output=True,
    )
    return str(path)


def _resolve_media_factory(media_dir: Path | None, demo_clip: str | None, demo_audio: str | None = None):
    def resolve(scene: dict) -> dict:
        if media_dir is not None:
            n = int(scene["scene_number"])
            footage = media_dir / f"scene-{n:03d}.mp4"
            audio = media_dir / f"scene-{n:03d}.mp3"
            return {
                "footage": str(footage),
                "audio": str(audio) if audio.exists() else None,
                "duration": float(scene.get("duration_hint_seconds") or 5.0),
            }
        # Demo mode: reuse the synthetic still; attach silent audio only when the
        # scene carries narration (the renderer rejects narration without audio).
        audio = demo_audio if (demo_audio and scene.get("narration")) else None
        return {"footage": demo_clip, "audio": audio,
                "duration": float(scene.get("duration_hint_seconds") or 5.0)}

    return resolve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--topic", required=True)
    parser.add_argument("--vertical", default="diy", choices=["diy", "type_beat", "jewelry"])
    parser.add_argument("--project-dir", required=True,
                        help="video_project working directory for the master render")
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--media-dir", default=None,
                        help="dir with prepared scene-NNN.mp4 / scene-NNN.mp3")
    parser.add_argument("--llm", default=None,
                        help="configured LLM provider (e.g. oneapi); omit with --demo")
    parser.add_argument("--demo", action="store_true",
                        help="plan from a built-in fixture and render a synthetic clip")
    parser.add_argument("--skill-level", default="")
    parser.add_argument("--target-minutes", type=int, default=None)
    parser.add_argument("--openmontage-root", default=None,
                        help="if set, export an OpenMontage scaffold + shorts plan")
    parser.add_argument("--no-render", action="store_true",
                        help="plan + save + gate only; skip the FFmpeg render")
    parser.add_argument("--target-seconds", type=int, default=30,
                        help="shorts target length for OpenMontage clip-factory")
    args = parser.parse_args(argv)

    if args.demo and args.llm:
        parser.error("--demo and --llm are mutually exclusive")

    generate = _demo_generate if args.demo else None
    if generate is None:
        from app.config import config
        from app.services.llm import _generate_response

        # Snapshot the loaded config so a provider override is applied to the
        # request without mutating global state. Falls back to config.toml's
        # llm_provider when --llm is omitted.
        app_config = dict(config.app)
        if args.llm:
            app_config["llm_provider"] = args.llm

        def generate(prompt: str) -> str:  # noqa: F811
            return _generate_response(prompt, app_config=app_config)

    run_id = diy_factory.new_run_id()
    project_id = args.project_id or f"{args.vertical}-{run_id[:8]}"
    print(f"[factory] run_id={run_id} project_id={project_id} vertical={args.vertical}")

    outline = diy_factory.plan_structure(
        topic=args.topic, skill_level=args.skill_level, target_minutes=args.target_minutes,
        vertical=args.vertical, generate=generate,
    )
    chapters = outline["chapters"]
    chapter_outputs = [
        diy_factory.write_chapter_scenes(
            chapter=chapter, chapter_index=idx, total_chapters=len(chapters),
            outline=outline, vertical=args.vertical, generate=generate,
        )
        for idx, chapter in enumerate(chapters)
    ]
    scenes = diy_factory.aggregate_scenes(
        outline=outline, chapter_outputs=chapter_outputs, vertical=args.vertical,
    )
    gate = diy_factory.readiness_gate(scenes=scenes, vertical=args.vertical)
    print(f"[factory] scenes={len(scenes)} gate_ok={gate['ok']} "
          f"errors={gate['errors']} warnings={len(gate['warnings'])}")

    runs_dir = Path(args.project_dir).parent / "factory-runs"
    saved = diy_factory.save_run(
        directory=runs_dir, run_id=run_id, topic=args.topic, vertical=args.vertical,
        outline=outline, scenes=scenes, gate=gate,
    )
    print(f"[factory] saved run -> {saved['run']}")

    master = None
    if args.no_render:
        pass
    elif args.demo:
        with tempfile.TemporaryDirectory() as td:
            demo_clip = _make_demo_clip(Path(td) / "demo.mp4")
            demo_audio = _make_demo_audio(Path(td) / "demo.m4a")
            master = _render(args, scenes, project_id, gate, demo_clip, demo_audio)
    else:
        master = _render(args, scenes, project_id, gate, None, None)

    if args.openmontage_root:
        om_paths = openmontage_bridge.export_project(
            openmontage_root=args.openmontage_root, project_name=project_id,
            topic=args.topic, vertical=args.vertical, outline=outline, scenes=scenes,
            master_video=master,
        )
        shorts = openmontage_bridge.plan_shorts(
            scenes=scenes, vertical=args.vertical, target_seconds=args.target_seconds,
        )
        print(f"[factory] openmontage scaffold -> {om_paths['project_dir']}")
        print(f"[factory] shorts planned -> {len(shorts.get('shorts', []))} "
              f"(pipeline={shorts.get('pipeline')})")
    return 0


def _render(args, scenes, project_id, gate, demo_clip, demo_audio):
    if not gate["ok"]:
        print(f"[factory] skipping render: gate failed {gate['errors']}", file=sys.stderr)
        return None
    media_dir = Path(args.media_dir) if args.media_dir else None
    resolve = _resolve_media_factory(media_dir, demo_clip, demo_audio)
    result = diy_factory.render_factory_video(
        project_dir=args.project_dir, project_id=project_id, scenes=scenes,
        vertical=args.vertical, resolve_media=resolve,
    )
    export = result["render"].get("export", {})
    print(f"[factory] render state={result['render'].get('state')} export={export.get('path')}")
    return export.get("path")


if __name__ == "__main__":
    raise SystemExit(main())
