"""Safe LAN-only asynchronous start/status for the canonical music-video factory."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import time
from uuid import UUID

from scripts.video_autopilot_queue import psql

ROOT = Path("/home/bobby/Videos/scene-director/automatic")
RUNFILE = ROOT / "current-job.json"
WORKER = Path(__file__).with_name("music_factory_worker.py")
SAFE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
SHA = re.compile(r"^[a-f0-9]{64}$")


def _alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        cmd = (Path(f"/proc/{pid}/cmdline")).read_bytes().replace(b"\0", b" ")
        return b"music_factory_worker.py" in cmd
    except (OSError, ValueError):
        return False


def status() -> dict:
    try:
        info = json.loads(RUNFILE.read_text())
    except (OSError, ValueError):
        return {"running": False, "status": "IDLE"}
    pid = int(info.get("pid") or 0)
    running = _alive(pid)
    return {
        "running": running,
        "status": "RUNNING" if running else "IDLE",
        "project_id": info.get("project_id"),
        "clip_id": info.get("clip_id"),
        "started_at": info.get("started_at"),
        "pid": pid if running else None,
    }


def start_job(item: dict, *, dry_run: bool = False) -> dict:
    if not isinstance(item, dict) or item.get("claimed") is not True:
        raise ValueError("Factory requires a verified claimed queue receipt")
    ident = str(UUID(str(item.get("clip_id") or "")))
    project = str(item.get("project_id") or "")
    if not SAFE.fullmatch(project) or project != "autovideo_" + ident.replace("-", ""):
        raise ValueError("Factory project ID must match canonical clip identity")
    if item.get("media_kind") not in ("beat", "song"):
        raise ValueError("Invalid studio lane")
    if item.get("channel") != (
        "Strictly Beats" if item["media_kind"] == "beat" else "BC TRAP GOD"
    ):
        raise ValueError("Media classification/channel mismatch")
    if item.get("apply_producer_tag") is not (item["media_kind"] == "beat"):
        raise ValueError("Beat tag policy mismatch")
    if not SHA.fullmatch(str(item.get("source_sha256") or "")):
        raise ValueError("Missing verified Modal WAV SHA")
    if not (60 <= float(item.get("duration_seconds") or 0) <= 360):
        raise ValueError("Unsupported recording duration")
    active = status()
    if active["running"]:
        return {"started": False, "reason": "factory_busy", **active}
    rows = psql(f"""SELECT clip_id::text FROM media_video_autopilot
      WHERE clip_id='{ident}'::uuid AND project_id='{project}'
        AND media_kind='{item["media_kind"]}' AND channel='{item["channel"]}'
        AND stage='CLAIMED' AND external_video_id IS NULL LIMIT 1;""")
    if ident not in rows:
        raise ValueError("No corresponding newly claimed NUC queue item")
    if dry_run:
        return {
            "started": False,
            "dry_run": True,
            "ready": True,
            "clip_id": ident,
            "project_id": project,
            "media_kind": item["media_kind"],
            "publishing_approved": False,
        }
    if not WORKER.is_file():
        raise RuntimeError("Factory media generation worker missing")
    folder = ROOT / project
    folder.mkdir(parents=True, exist_ok=True)
    request = folder / "input-claimed.json"
    temp = request.with_suffix(".tmp")
    temp.write_text(json.dumps(item, indent=2) + "\n")
    temp.chmod(0o600)
    temp.replace(request)
    log = folder / "worker.log"
    with log.open("a") as output:
        proc = subprocess.Popen(
            [sys.executable, str(WORKER), str(request)],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            cwd="/home/bobby/projects/MoneyPrinterTurbo",
        )
    payload = {
        "project_id": project,
        "clip_id": ident,
        "pid": proc.pid,
        "started_at": int(time.time()),
    }
    stage = RUNFILE.with_suffix(".tmp")
    stage.write_text(json.dumps(payload) + "\n")
    stage.chmod(0o600)
    stage.replace(RUNFILE)
    return {
        "started": True,
        "project_id": project,
        "clip_id": ident,
        "pid": proc.pid,
        "stage": "ASYNC_VIDEO_GENERATION",
        "media_kind": item["media_kind"],
        "publishing_approved": False,
    }
