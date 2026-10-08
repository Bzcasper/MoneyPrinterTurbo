"""Locked synchronous Modal final-render gateway for trusted ROG n8n bridge."""
from __future__ import annotations

import fcntl
import json
import re
import subprocess
from pathlib import Path

REPO = Path("/home/bobby/projects/suno-typebeat-foundation")
PYTHON = REPO / ".venv/bin/python"
PROJECT = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def render_modal(project_id: str, *, width: int = 1920, height: int = 1080, timeout: int = 7200,
                 repo: Path = REPO) -> dict:
    if not PROJECT.fullmatch(str(project_id or "")):
        raise ValueError("invalid project ID")
    if width % 2 or height % 2 or not 640 <= width <= 3840 or not 360 <= height <= 2160:
        raise ValueError("invalid even render dimensions")
    lockdir = Path("/home/bobby/Videos/scene-director/modal-production/.locks")
    lockdir.mkdir(parents=True, exist_ok=True)
    with (lockdir / (project_id + ".lock")).open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Modal video render already running for project") from exc
        process = subprocess.run(
            [str(repo / ".venv/bin/python"), str(repo / "scripts/run_modal_motion_from_nuc.py"),
             "--project-id",project_id,"--width",str(width),"--height",str(height),"--execute"],
            cwd=repo,capture_output=True,text=True,timeout=timeout,check=False,
        )
        if process.returncode:
            raise RuntimeError("Modal render worker failed; inspect ROG render log: "
                               + (process.stderr or process.stdout)[-1000:])
        found = [line[len("RESULT_JSON:"):] for line in process.stdout.splitlines()
                 if line.startswith("RESULT_JSON:")]
        if len(found) != 1:
            raise RuntimeError("Modal renderer did not return exactly one final manifest")
        result = json.loads(found[0])
        if (result.get("status") != "TECHNICAL_PASS"
                or result.get("project_id") != project_id
                or result.get("publishing_approved") is not False
                or not result.get("all_motion")
                or result.get("audio_volume") != "suno-playback-library"):
            raise RuntimeError("Modal technical master did not pass source/motion gates")
        return result
