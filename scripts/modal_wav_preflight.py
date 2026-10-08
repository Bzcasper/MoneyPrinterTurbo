"""Read-only Modal WAV catalog and path preflight before costly video generation."""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

MODAL = Path("/home/bobby/projects/suno-typebeat-foundation/.venv/bin/modal")
UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
SHA = re.compile(r"^[0-9a-f]{64}$")


def verify_song(clip_id: str, title: str, *, cli: Path = MODAL) -> dict:
    if not UUID.fullmatch(clip_id):
        raise ValueError("a valid lowercase Suno clip UUID is required")
    if not isinstance(title, str) or not title.strip() or len(title) > 120:
        raise ValueError("a canonical song title is required")
    with tempfile.TemporaryDirectory(prefix="modal-wav-catalog-") as folder:
        path = Path(folder) / "catalog.json"
        subprocess.run(
            [str(cli), "volume", "get", "suno-playback-library", "_library_catalog.json", str(path)],
            check=True, capture_output=True, text=True, timeout=120,
        )
        items = json.loads(path.read_text()).get("items", [])
    matches = [item for item in items if item.get("id") == clip_id]
    if len(matches) != 1:
        raise ValueError("selected song UUID missing or duplicated in Modal WAV catalog")
    item = matches[0]
    if str(item.get("title") or "").strip().casefold() != title.strip().casefold():
        raise ValueError("selected clip title does not match Modal WAV library")
    digest = str(item.get("_wav_sha256") or "")
    length = float(item.get("duration") or 0)
    size = int(item.get("wavBytes") or 0)
    if not SHA.fullmatch(digest) or length <= 0 or size <= 44:
        raise ValueError("Modal WAV metadata is incomplete")
    relative = f"clips/{clip_id}/audio/{clip_id}.wav"
    listed = subprocess.run(
        [str(cli), "volume", "ls", "suno-playback-library", f"clips/{clip_id}/audio"],
        check=True, capture_output=True, text=True, timeout=90,
    )
    if relative not in listed.stdout.splitlines():
        raise ValueError("Modal volume is missing the WAV file declared in catalog")
    return {
        "ok": True, "clip_id": clip_id, "title": str(item["title"]),
        "modal_volume": "suno-playback-library", "modal_path": relative,
        "catalog_sha256": digest, "duration_seconds": length,
        "wav_bytes": size, "source_type": "playback_derived_wav",
        "source_authoritative": "Modal catalog file+metadata; byte SHA verified at render",
        "publishing_approved": False,
    }
