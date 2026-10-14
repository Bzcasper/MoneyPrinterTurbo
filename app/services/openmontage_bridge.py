# -*- coding: utf-8 -*-
"""
Optional OpenMontage bridge for volume output.

MoneyPrinterTurbo renders one long-form master per run. OpenMontage
(https://github.com/calesthio/OpenMontage, AGPL-3.0) is an agentic production
system that multiplies one source into many derivatives -- exactly the "squeeze
more video out" step. It is deliberately kept behind a thin, optional adapter:

* ``export_project`` writes an OpenMontage ``projects/<name>/`` scaffold from a
  finished MPT plan so the OpenMontage agent can pick it up (idea/script/scene
  plan already done; it takes over assets/edit/compose).
* ``plan_shorts`` proposes short-form derivatives (clip windows + captions) from
  the master scene manifest, mapped to OpenMontage's ``clip-factory`` pipeline.

Nothing here shells out, installs, or runs OpenMontage. It only produces the
files/plan OpenMontage's own agent consumes, so MPT stays in control of the
gate and no media is published automatically.
"""

from __future__ import annotations

import json
from pathlib import Path


class OpenMontageError(ValueError):
    """Raised when a scaffold cannot be written."""


def _om_config(vertical_key: str) -> dict:
    from app.services import verticals

    production = (verticals.get_vertical(vertical_key).get("production") or {}).get(
        "openmontage", {}
    )
    return production or {}


def export_project(
    *,
    openmontage_root: str | Path,
    project_name: str,
    topic: str,
    vertical: str,
    outline: dict,
    scenes: list[dict],
    master_video: str | Path | None = None,
) -> dict:
    """Write an OpenMontage ``projects/<name>/`` scaffold from a finished plan.

    Produces ``artifacts/script.json`` and ``artifacts/scene_plan.json`` plus a
    ``project.json`` pointing at the OpenMontage pipeline the preset selects.
    Returns the written paths. Safe to call even if OpenMontage is not running;
    the files are inert until an OpenMontage agent acts on them.
    """
    if not str(project_name or "").strip():
        raise OpenMontageError("a non-empty project_name is required")
    om = _om_config(vertical)
    root = Path(openmontage_root) / "projects" / project_name
    artifacts = root / "artifacts"
    assets = root / "assets"
    artifacts.mkdir(parents=True, exist_ok=True)
    (assets / "video").mkdir(parents=True, exist_ok=True)

    (artifacts / "script.json").write_text(
        json.dumps(
            {
                "topic": topic,
                "hook": outline.get("hook", ""),
                "brief": outline.get("optimized_brief", ""),
                "chapters": outline.get("chapters", []),
                "materials_list": outline.get("materials_list", []),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (artifacts / "scene_plan.json").write_text(
        json.dumps(
            {"scene_count": len(scenes), "scenes": scenes},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    project_meta = {
        "name": project_name,
        "source": "moneyprinterturbo",
        "vertical": vertical,
        "pipeline": om.get("pipeline", "animated-explainer"),
        "art_style": (outline.get("visual_bible") or {}),
        "master_video": str(master_video) if master_video else None,
    }
    (root / "project.json").write_text(
        json.dumps(project_meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return {
        "project_dir": str(root),
        "script": str(artifacts / "script.json"),
        "scene_plan": str(artifacts / "scene_plan.json"),
        "meta": str(root / "project.json"),
    }


def plan_shorts(
    *,
    scenes: list[dict],
    vertical: str,
    target_seconds: int = 30,
    max_shorts: int = 10,
) -> dict:
    """Propose short-form derivatives from the master scene manifest.

    Walks the numbered scenes, grouping consecutive non-title scenes until each
    group reaches ~``target_seconds`` (using narration duration hints or a
    per-scene default), so an OpenMontage ``clip-factory`` run can cut the
    master into vertical/short clips. Deterministic; no media touched.
    """
    om = _om_config(vertical)
    if not om.get("shorts", True):
        return {"enabled": False, "shorts": []}

    default_scene = 5.0
    shorts: list[dict] = []
    current: list[int] = []
    current_len = 0.0
    for scene in scenes:
        if scene.get("is_title_card"):
            continue
        dur = float(scene.get("duration_hint_seconds") or default_scene)
        current.append(int(scene["scene_number"]))
        current_len += dur
        if current_len >= target_seconds:
            shorts.append(
                {
                    "scene_numbers": list(current),
                    "approx_seconds": round(current_len, 1),
                    "start_scene": current[0],
                    "end_scene": current[-1],
                }
            )
            current, current_len = [], 0.0
            if len(shorts) >= max_shorts:
                break
    if current and len(shorts) < max_shorts:
        shorts.append(
            {
                "scene_numbers": list(current),
                "approx_seconds": round(current_len, 1),
                "start_scene": current[0],
                "end_scene": current[-1],
            }
        )
    return {
        "enabled": True,
        "pipeline": om.get("multiplier_pipeline", "clip-factory"),
        "target_seconds": target_seconds,
        "shorts": shorts,
    }
