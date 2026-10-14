# -*- coding: utf-8 -*-
"""
Long-form "factory" planner + native renderer bridge.

This is the MoneyPrinterTurbo replacement for the n8n "DIY Longform Factory"
(workflow 6WLXL0NItkqrm7CI). It keeps the two things that workflow is genuinely
good at -- *scene planning / chapter writing* and *run data saving* -- and hands
the actual video assembly to this repository's own deterministic renderer
(``app.services.video_project``) instead of the external Modal service.

Flow::

    plan_structure()       -> chapters + visual bible       (n8n: Structure Agent)
    write_chapter_scenes() -> per-chapter scenes            (n8n: Create Chapter Scenes
                                                             + Chapter Media Enhancer)
    aggregate_scenes()     -> flat, numbered scene manifest (n8n: Aggregate Chapters)
    readiness_gate()       -> blockers/warnings             (n8n: Production Readiness Gate)
    to_project_spec()      -> video_project manifest        (n8n: Build Job Payload -> Modal)
    save_run()             -> planning JSON + run log       (n8n: ~20 dataTable nodes)

The planner is LLM-backed but every entry point accepts an injectable
``generate`` callable so tests run fully offline with deterministic fixtures.
No provider, network, or credential is touched by this module; prepared media
(narration audio, images, beat) is produced elsewhere and only *referenced* by
path in the project spec the renderer consumes.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.services import verticals


# A planner "generate" callable: given a fully-formed prompt string, return the
# model's raw text. Production passes an LLM wrapper; tests pass a stub.
Generate = Callable[[str], str]


class FactoryError(ValueError):
    """Raised when a plan cannot be produced or fails the readiness gate."""


_PLACEHOLDER_RE = re.compile(
    r"\[NEEDS CLARIFICATION:|\bTBD\b|\bTODO\b|\[.*?placeholder.*?\]", re.I
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_json(text: str) -> dict:
    """Recover the first JSON object from a possibly chatty model response."""
    if isinstance(text, dict):
        return text
    cleaned = str(text).replace("```json", "").replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise FactoryError("planner response contained no JSON object")
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise FactoryError(f"planner response was not valid JSON: {exc}") from exc


def plan_structure(
    *,
    topic: str,
    skill_level: str = "",
    target_minutes: int | None = None,
    extra_context: str = "",
    vertical: str = "diy",
    generate: Generate,
) -> dict:
    """Produce the chaptered outline + visual bible for a topic.

    Mirrors the n8n Structure Agent: one LLM call returns ``chapters``,
    ``visual_bible``, ``hook``, ``pacing_notes`` and ``materials_list``. The
    preset's ``production.art_style`` is injected so the outline stays on-brand.
    """
    if not str(topic or "").strip():
        raise FactoryError("a non-empty topic is required")
    preset = verticals.get_vertical(vertical)
    production = preset.get("production") or {}
    planning = production.get("planning", {})
    art = production.get("art_style", {})
    chapters_target = target_minutes or planning.get("target_minutes") or 8

    prompt = (
        "<Task>Write a structured long-form plan for this DIY project.</Task>\n"
        f"<Topic>{topic}</Topic>\n"
        f"<SkillLevel>{skill_level or 'beginner'}</SkillLevel>\n"
        f"<TargetMinutes>{chapters_target}</TargetMinutes>\n"
        f"<ExtraContext>{extra_context}</ExtraContext>\n"
        f"<ArtStyle>{json.dumps(art)}</ArtStyle>\n"
        "Return ONLY JSON with keys: hook (string), optimized_brief (string), "
        "pacing_notes (string), materials_list (array of strings), "
        "visual_bible (object), chapters (array of objects each with "
        "chapter_number, title, summary, target_seconds)."
    )
    outline = _extract_json(generate(prompt))
    chapters = outline.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise FactoryError("planner returned no chapters")
    outline["chapters"] = chapters
    outline.setdefault("visual_bible", {})
    outline.setdefault("materials_list", [])
    return outline


def write_chapter_scenes(
    *,
    chapter: dict,
    chapter_index: int,
    total_chapters: int,
    outline: dict,
    vertical: str = "diy",
    scenes_per_chapter: int | None = None,
    generate: Generate,
) -> dict:
    """Write one chapter's scenes (narration + image_prompt per scene).

    Mirrors the n8n "Create Chapter Scenes" + "Chapter Media Enhancer" pair:
    the scene agent writes narration and action, then every scene gets a
    Firefly-ready ``image_prompt`` built from the shared visual bible.
    """
    preset = verticals.get_vertical(vertical)
    production = preset.get("production") or {}
    per_chapter = scenes_per_chapter or production.get("planning", {}).get(
        "scenes_per_chapter", 6
    )
    art = production.get("art_style", {})
    prompt = (
        "<ChapterToWrite>\n"
        f"{json.dumps(chapter)}\n"
        "</ChapterToWrite>\n"
        f"<ChapterPosition>{chapter_index + 1} of {total_chapters}</ChapterPosition>\n"
        f"<Hook>{outline.get('hook', '')}</Hook>\n"
        f"<VisualBible>{json.dumps(outline.get('visual_bible', {}))}</VisualBible>\n"
        f"<ArtStyle>{json.dumps(art)}</ArtStyle>\n"
        f"<SceneCount>{per_chapter}</SceneCount>\n"
        "Write ONLY this chapter. Return ONLY JSON with keys chapter_title "
        "(string) and scenes (array of objects each with scene_number, "
        "narration, image_prompt, image_action, camera_hint, "
        "duration_hint_seconds)."
    )
    parsed = _extract_json(generate(prompt))
    scenes = parsed.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise FactoryError(f"chapter {chapter_index + 1} produced no scenes")
    parsed.setdefault("chapter_title", chapter.get("title", f"Chapter {chapter_index + 1}"))
    return parsed


def aggregate_scenes(
    *,
    outline: dict,
    chapter_outputs: list[dict],
    vertical: str = "diy",
) -> list[dict]:
    """Flatten per-chapter scenes into a single numbered manifest.

    Mirrors the n8n "Aggregate Chapters" node: prepend a title card per chapter
    (no narration), then number every scene sequentially across the whole run.
    """
    preset = verticals.get_vertical(vertical)
    production = preset.get("production") or {}
    title_cards = production.get("planning", {}).get("title_cards", True)
    palette = (outline.get("visual_bible", {}) or {}).get(
        "color_palette", "muted blues and warm wood tones"
    )

    scenes: list[dict] = []
    number = 0
    for output in chapter_outputs:
        chapter_title = output.get("chapter_title", "Chapter")
        if title_cards:
            number += 1
            scenes.append(
                {
                    "scene_number": number,
                    "chapter": chapter_title,
                    "is_title_card": True,
                    "narration": "",
                    "ken_burns": False,
                    "image_prompt": (
                        f"Clean instructional title card, bold sans-serif white "
                        f'text reading "{chapter_title}", soft dark gradient '
                        f"background, palette {palette}"
                    ),
                }
            )
        for scene in output.get("scenes", []):
            number += 1
            scenes.append(
                {
                    "scene_number": number,
                    "chapter": chapter_title,
                    "is_title_card": False,
                    "narration": str(scene.get("narration", "")).strip(),
                    "ken_burns": scene.get("ken_burns", True),
                    "image_prompt": str(scene.get("image_prompt", "")),
                    "image_action": scene.get("image_action", ""),
                    "camera_hint": scene.get("camera_hint", ""),
                    "duration_hint_seconds": scene.get("duration_hint_seconds"),
                }
            )
    return scenes


def readiness_gate(*, scenes: list[dict], vertical: str = "diy") -> dict:
    """Validate the manifest before render.

    Mirrors the n8n "Production Readiness Gate": returns blockers (which stop
    the run) and warnings (which do not). Deterministic, no I/O.
    """
    preset = verticals.get_vertical(vertical)
    qa = (preset.get("production") or {}).get("qa", {})
    planning = (preset.get("production") or {}).get("planning", {})

    errors: list[str] = []
    warnings: list[str] = []
    if len(scenes) < planning.get("min_scenes", 1):
        errors.append(f"too_few_scenes:{len(scenes)}")
    if len(scenes) > planning.get("max_scenes", 1000):
        errors.append(f"too_many_scenes:{len(scenes)}")

    seen: set[int] = set()
    for scene in scenes:
        number = int(scene.get("scene_number", 0))
        if number in seen:
            errors.append(f"duplicate_scene_number:{number}")
        seen.add(number)
        if scene.get("is_title_card"):
            continue
        narration = str(scene.get("narration", ""))
        prompt = str(scene.get("image_prompt", ""))
        if qa.get("require_narration") and not narration:
            errors.append(f"missing_narration:{number}")
        if qa.get("require_image_prompt") and not prompt:
            errors.append(f"missing_image_prompt:{number}")
        if qa.get("reject_placeholder_text") and _PLACEHOLDER_RE.search(
            narration + " " + prompt
        ):
            warnings.append(f"placeholder_text:{number}")

    return {"ok": not errors, "errors": errors, "warnings": warnings}


def to_project_spec(
    *,
    project_id: str,
    scenes: list[dict],
    vertical: str = "diy",
    resolve_media: Callable[[dict], dict],
) -> dict:
    """Build an ``app.services.video_project`` manifest from the manifest.

    ``resolve_media`` maps each planned scene to a ``{"footage", "audio",
    "duration"}`` dict of prepared local paths (image clip + narration audio).
    This keeps this module free of any provider/asset generation; the caller
    supplies the media. Settings come from the preset's ``production.render``.
    """
    preset = verticals.get_vertical(vertical)
    render = (preset.get("production") or {}).get(
        "render",
        {
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "narration_volume": 1.0,
            "bgm_volume": 0.15,
        },
    )
    settings = {
        "width": render["width"],
        "height": render["height"],
        "fps": render["fps"],
        "narration_volume": render.get("narration_volume", 1.0),
        "bgm_volume": render.get("bgm_volume", 0.15),
    }
    project_scenes = []
    for scene in scenes:
        media = resolve_media(scene)
        project_scenes.append(
            {
                "id": f"s{int(scene['scene_number']):03d}",
                "narration": str(scene.get("narration", "")),
                "duration": float(
                    media.get("duration", scene.get("duration_hint_seconds") or 5.0)
                ),
                "footage": media["footage"],
                "audio": media.get("audio"),
            }
        )
    return {
        "project_id": project_id,
        "scenes": project_scenes,
        "settings": settings,
        "bgm": None,
    }


def save_run(
    *,
    directory: str | Path,
    run_id: str,
    topic: str,
    vertical: str,
    outline: dict,
    scenes: list[dict],
    gate: dict,
    extra: dict | None = None,
) -> dict:
    """Persist the full planning record + append a run-log line.

    Replaces the ~20 n8n dataTable logging/archive nodes with a single
    deterministic JSON snapshot per run plus an append-only ``runs.jsonl``.
    Returns the written paths. Never writes media or credentials.
    """
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": run_id,
        "topic": topic,
        "vertical": vertical,
        "created_at": _now_iso(),
        "outline": outline,
        "scenes": scenes,
        "readiness_gate": gate,
        "scene_count": len(scenes),
        "extra": extra or {},
    }
    run_path = root / f"{run_id}.json"
    run_path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log_path = root / "runs.jsonl"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "run_id": run_id,
                    "topic": topic,
                    "vertical": vertical,
                    "created_at": record["created_at"],
                    "scene_count": len(scenes),
                    "gate_ok": gate.get("ok"),
                    "errors": gate.get("errors", []),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    return {"run": str(run_path), "log": str(log_path)}


def new_run_id() -> str:
    return uuid.uuid4().hex


def render_factory_video(
    *,
    project_dir: str | Path,
    project_id: str,
    scenes: list[dict],
    vertical: str = "diy",
    resolve_media: Callable[[dict], dict],
) -> dict:
    """Plan-to-render glue: build a project spec and render it natively.

    This is the drop-in replacement for the n8n workflow's
    ``Build Job Payload -> Submit Job -> Modal /jobs`` hop. It converts the
    planned scene manifest into an ``app.services.video_project`` revision and
    runs this repository's own deterministic FFmpeg renderer (no external
    service, no credential). Returns the render status including the exported
    master path. Raises ``FactoryError`` if the readiness gate fails so a bad
    plan never reaches the renderer.
    """
    from app.services.video_project import VideoProject

    gate = readiness_gate(scenes=scenes, vertical=vertical)
    if not gate["ok"]:
        raise FactoryError(f"plan failed readiness gate: {gate['errors']}")

    spec = to_project_spec(
        project_id=project_id, scenes=scenes, vertical=vertical, resolve_media=resolve_media
    )
    project = VideoProject(project_dir)
    project.create_revision(spec)
    status = project.render()
    return {"gate": gate, "spec": spec, "render": status}

