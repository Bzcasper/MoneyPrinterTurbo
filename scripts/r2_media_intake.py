"""Import a signed, private R2 media asset and verify bytes before n8n processing."""

from __future__ import annotations
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path("/home/bobby/Videos/scene-director/r2-intake")
HOSTS = frozenset(
    {
        "scene-continuity-relay.eternaleleganceemporium.workers.dev",
        "scene-media.aitoolpool.com",
    }
)
SAFE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
SHA = re.compile(r"^[a-f0-9]{64}$")
OBJECT = re.compile(
    r"^(?:projects|clips)/[A-Za-z0-9._-]{1,100}/(?:sources|audio|scenes|refs)/[A-Za-z0-9._-]{1,120}\.(?:wav|mp4|jpe?g|png|webp)$"
)
MAX_BYTES = 64 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_signed_url(value: str, expected_key: str) -> None:
    if not isinstance(value, str) or len(value) > 1800:
        raise ValueError("Invalid signed R2 media URL")
    u = urllib.parse.urlsplit(value)
    if (
        u.scheme != "https"
        or u.hostname not in HOSTS
        or u.port not in (None, 443)
        or u.username
        or u.password
        or u.fragment
    ):
        raise ValueError("R2 media must use the dedicated HTTPS Worker")
    if (
        not OBJECT.fullmatch(expected_key)
        or urllib.parse.unquote(u.path) != "/v1/media/" + expected_key
    ):
        raise ValueError("Signed media path must match approved object key")
    q = urllib.parse.parse_qs(u.query, keep_blank_values=True)
    if (
        set(q) != {"exp", "sig"}
        or any(len(v) != 1 for v in q.values())
        or not SHA.fullmatch(q["sig"][0])
    ):
        raise ValueError("R2 signature fields are invalid")
    try:
        exp = int(q["exp"][0])
    except ValueError as exc:
        raise ValueError("Invalid R2 expiry") from exc
    now = int(time.time())
    if exp < now or exp > now + 900:
        raise ValueError("R2 signed media URL is expired or exceeds TTL")


def fetch_verified_media(
    *,
    signed_url: str,
    asset_key: str,
    expected_sha256: str,
    project_id: str,
    clip_id: str,
    root: Path = ROOT,
) -> dict:
    if not all(SAFE.fullmatch(str(x)) for x in (project_id, clip_id)):
        raise ValueError("Invalid project or source clip identity")
    if not SHA.fullmatch(expected_sha256):
        raise ValueError("Missing expected SHA-256")
    validate_signed_url(signed_url, asset_key)
    relative = Path(asset_key)
    if relative.parts[0] == "projects" and relative.parts[1] != project_id:
        raise ValueError("R2 project key does not match request")
    if relative.parts[0] == "clips" and relative.parts[1] != clip_id:
        raise ValueError("R2 clip key does not match source")
    target_dir = (root.resolve() / project_id).resolve()
    if not target_dir.is_relative_to(root.resolve()):
        raise ValueError("Unsafe target location")
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / relative.name
    if (
        dest.is_file()
        and hashlib.sha256(dest.read_bytes()).hexdigest() == expected_sha256
    ):
        already = True
    else:
        already = False
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target_dir, prefix=".r2-", suffix=".partial", delete=False
            ) as f:
                tmp_path = Path(f.name)
                req = urllib.request.Request(
                    signed_url, headers={"User-Agent": "SceneContinuityDirector/1.0"}
                )
                with urllib.request.build_opener(NoRedirect()).open(
                    req, timeout=90
                ) as stream:
                    if stream.status != 200:
                        raise ValueError("R2 media response must be HTTP 200")
                    total = 0
                    dig = hashlib.sha256()
                    while True:
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_BYTES:
                            raise ValueError("R2 media exceeds size limit")
                        dig.update(chunk)
                        f.write(chunk)
                if total < 512 or dig.hexdigest() != expected_sha256:
                    raise ValueError("R2 media checksum/size mismatch")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, dest)
            tmp_path = None
        finally:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink()
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=codec_type,codec_name,sample_rate,channels,width,height,avg_frame_rate",
            "-of",
            "json",
            str(dest),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=35,
    )
    info = json.loads(proc.stdout)
    duration = float(info.get("format", {}).get("duration") or 0)
    kinds = {s.get("codec_type") for s in info.get("streams", [])}
    media_type = (
        "audio"
        if dest.suffix == ".wav"
        else "video"
        if dest.suffix == ".mp4"
        else "image"
    )
    if (
        duration <= 0
        or (media_type == "video" and "video" not in kinds)
        or (media_type == "audio" and "audio" not in kinds)
    ):
        raise ValueError("R2 media has invalid decoded streams")
    return {
        "success": True,
        "asset_key": asset_key,
        "source_clip_id": clip_id,
        "project_id": project_id,
        "sha256": expected_sha256,
        "bytes": dest.stat().st_size,
        "duration_seconds": round(duration, 6),
        "media_type": media_type,
        "streams": info["streams"],
        "local_path": str(dest),
        "previously_imported": already,
        "publishing_approved": False,
    }
