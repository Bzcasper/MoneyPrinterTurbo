"""Canonical Suno playback-library queue admission for the hourly music factory.

Draft generation is permitted without asserting commercial rights. Publication
remains governed by the separate, fail-closed release_check() in the NUC ledger.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
import re
import subprocess
import tempfile
import time
from uuid import UUID

from scripts.video_autopilot_queue import psql

MODAL = Path("/home/bobby/projects/suno-typebeat-foundation/.venv/bin/modal")
CATALOG = Path("/home/bobby/.cache/scene-continuity/modal-factory-catalog.json")
UUID_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
SHA = re.compile(r"^[a-f0-9]{64}$")
INSTRUMENTAL_LYRICS = re.compile(r"^(?:\[\s*instrumental\s*\]\s*)+$", re.I)


def _lyrics_compatible_with_instrumental(item: dict) -> bool:
    """Reject explicitly vocal lyrics even if database instrumental flag is set.

    Style tags such as 'male vocals' describe the requested genre and are not
    evidence that the produced audio contains vocals. Empty or [Instrumental]
    lyrics are compatible with the authoritative instrumental flag.
    """
    lyrics = str(item.get("lyrics") or "").strip()
    return not lyrics or lyrics.casefold() == "instrumental" or bool(
        INSTRUMENTAL_LYRICS.fullmatch(lyrics)
    )

SQL_SELECT = """
SELECT row_to_json(t)::text FROM (
 SELECT q.queue_id,q.clip_id::text,q.project_type,q.channel,
   q.priority,q.attempts,q.max_attempts,q.project_id as queue_project_id,
   m.title,m.duration_seconds,m.make_instrumental,m.style_tags,
   m.modal_audio_present,
   COALESCE(r.permitted_download_obtained,FALSE) AS permitted_download,
   COALESCE(r.rights_status,'') AS rights_status,
   CASE WHEN q.project_type='type_beat' AND m.make_instrumental IS TRUE AND q.channel='Strictly Beats' THEN 'beat'
     WHEN q.project_type='song' AND m.make_instrumental IS FALSE AND q.channel='BC TRAP GOD' THEN 'song'
     ELSE NULL END media_kind
 FROM media_library_video_queue q
 JOIN music_assets m ON m.clip_id=q.clip_id
 LEFT JOIN commercial_rights r ON r.clip_id=q.clip_id
 WHERE q.status IN ('READY','RETRY')
 AND q.attempts<q.max_attempts
 AND q.project_type='type_beat' AND q.channel='Strictly Beats' AND m.make_instrumental IS TRUE
 AND (q.next_attempt_at IS NULL OR q.next_attempt_at<=now())
 AND m.modal_audio_present IS TRUE
 AND m.duration_seconds BETWEEN 60 AND 360
 AND length(trim(COALESCE(m.title,''))) BETWEEN 3 AND 120
 AND NOT EXISTS (SELECT 1 FROM media_video_autopilot x WHERE x.clip_id=q.clip_id
    AND x.stage NOT IN ('FAILED_RETRYABLE'))
 ORDER BY q.priority DESC,q.created_at,q.queue_id
 LIMIT 100
) t
"""

SQL_MP3_SONGS = """
SELECT row_to_json(t)::text FROM (
 SELECT q.queue_id,q.clip_id::text,q.project_type,q.channel,
   q.priority,m.title,m.duration_seconds,m.make_instrumental,m.style_tags,
   a.path AS source_modal_path,a.format AS source_format,
   a.modal_volume AS source_volume,a.sha256 AS source_sha256,
   a.size_bytes AS source_bytes,
   COALESCE(r.permitted_download_obtained,FALSE) AS permitted_download,
   COALESCE(r.rights_status,'') AS rights_status
 FROM media_library_video_queue q
 JOIN music_assets m ON m.clip_id=q.clip_id
 JOIN LATERAL (
   SELECT path,format,modal_volume,sha256,size_bytes FROM audio_files
   WHERE clip_id=q.clip_id AND modal_volume='suno-songs-v2' AND format='mp3'
    AND sha256 ~ '^[a-f0-9]{64}$' AND size_bytes>100000
   ORDER BY size_bytes DESC LIMIT 1
 ) a ON TRUE
 LEFT JOIN commercial_rights r ON r.clip_id=q.clip_id
 WHERE q.status IN ('READY','RETRY')
  AND q.attempts<q.max_attempts
  AND (q.next_attempt_at IS NULL OR q.next_attempt_at<=now())
  AND q.project_type='song' AND q.channel='BC TRAP GOD'
  AND m.make_instrumental IS FALSE
  AND m.duration_seconds BETWEEN 60 AND 360
  AND length(trim(COALESCE(m.title,''))) BETWEEN 3 AND 120
  AND NOT EXISTS (SELECT 1 FROM media_video_autopilot x WHERE x.clip_id=q.clip_id
    AND x.stage NOT IN ('FAILED_RETRYABLE'))
 ORDER BY q.priority DESC,q.created_at,q.queue_id
 LIMIT 250
) t
"""


def _mp3_song_candidate(*, catalog: dict | None = None) -> dict | None:
    from scripts.music_factory_lyrics_story import (
        qualified_catalog_lyrics, lyric_lines, _choose_lyrics, _lyric_stage,
    )

    catalog = _catalog() if catalog is None else catalog
    best = None
    best_grounded = 0
    for line in psql(SQL_MP3_SONGS):
        row = json.loads(line)
        ident = str(row.get("clip_id") or "")
        if not UUID_RE.fullmatch(ident):
            continue
        relative = f"{ident}/{ident}.mp3"
        if (
            row.get("source_modal_path") != relative
            or row.get("source_format") != "mp3"
            or row.get("source_volume") != "suno-songs-v2"
            or not SHA.fullmatch(str(row.get("source_sha256") or ""))
            or int(row.get("source_bytes") or 0) <= 100000
        ):
            continue
        if (
            row.get("project_type") != "song"
            or row.get("make_instrumental") is not False
        ):
            continue
        # Never spend 30 clips on a song without real source lyrics.
        # Style tags or the title must not be substituted for the narrative.
        lyrics = qualified_catalog_lyrics(catalog, ident, str(row.get("title") or ""))
        if not lyrics:
            continue
        lyric_evidence = lyric_lines(lyrics)
        cues = [
            text for act in range(10)
            for _, text in _choose_lyrics(lyric_evidence, act)
        ]
        grounded = sum(
            not _lyric_stage(text)[0].startswith(
                "pauses in a physically believable reflective gesture"
            )
            for text in cues
        )
        # Preference ranking is bounded to the existing queued catalog window.
        # Do not spend thirty expensive motion jobs on mostly generic scenes.
        if grounded < 15 or grounded <= best_grounded:
            continue
        best_grounded = grounded
        best = {
            "has_work": True,
            "lyric_grounded_shots": grounded,
            "lyric_director_status": "DETERMINISTIC_PARTIAL_GROUNDING",
            "clip_id": ident,
            "queue_id": int(row["queue_id"]),
            "title": str(row["title"]).strip(),
            "media_kind": "song",
            "channel": "BC TRAP GOD",
            "apply_producer_tag": False,
            "source_format": "mp3",
            "source_type": "original_modal_song_mp3",
            "source_volume": "suno-songs-v2",
            "source_modal_path": relative,
            "source_sha256": str(row["source_sha256"]),
            "source_bytes": int(row["source_bytes"]),
            "duration_seconds": float(row["duration_seconds"]),
            "genre": str(row.get("style_tags") or "")[:450],
            "project_id": "autovideo_" + ident.replace("-", ""),
            "source_rights_verified": bool(row.get("permitted_download"))
            and row.get("rights_status") in {"APPROVED", "CLEARED", "OWNED_ORIGINAL"},
            "generation_preflight_ready": True,
            "generation_policy": "UNPUBLISHED_REVIEW_ONLY",
            "publishing_approved": False,
            "public_post_allowed": False,
            "status": "READY_FOR_DRAFT",
        }
    return best


# Strict direction: q.project_type and music_assets.make_instrumental must agree.


def _catalog(*, force_refresh: bool = False) -> dict:
    CATALOG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (
        force_refresh
        or not CATALOG.is_file()
        or time.time() - CATALOG.stat().st_mtime > 1200
    ):
        with tempfile.TemporaryDirectory(prefix="music-factory-catalog-") as tmp:
            download = Path(tmp) / "catalog.json"
            subprocess.run(
                [
                    str(MODAL),
                    "volume",
                    "get",
                    "suno-playback-library",
                    "_library_catalog.json",
                    str(download),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=150,
            )
            source = json.loads(download.read_text())
            if not isinstance(source.get("items"), list) or len(source["items"]) < 10:
                raise ValueError(
                    "Modal playback catalog does not contain the expected entries"
                )
            # Modal CLI writes under /tmp, which is a different filesystem from
            # /home on ROG. Stage on the same destination mount before rename.
            staged = CATALOG.with_suffix(".incoming")
            shutil.copyfile(download, staged)
            staged.chmod(0o600)
            staged.replace(CATALOG)
        CATALOG.chmod(0o600)
    data = json.loads(CATALOG.read_text())
    return {str(x.get("id")): x for x in data.get("items", []) if isinstance(x, dict)}


def _rows() -> list[dict]:
    lines = psql(SQL_SELECT)
    return [json.loads(line) for line in lines]


def _classify(row: dict, item: dict) -> dict | None:
    kind = row.get("media_kind")
    clip = str(row.get("clip_id") or "")
    if kind not in {"beat", "song"} or not UUID_RE.fullmatch(clip):
        return None
    if row.get("project_type") != ("type_beat" if kind == "beat" else "song"):
        return None
    if bool(row.get("make_instrumental")) is not (kind == "beat"):
        return None
    if kind == "beat" and not _lyrics_compatible_with_instrumental(item):
        return None
    if (
        str(item.get("title") or "").strip().casefold()
        != str(row.get("title") or "").strip().casefold()
    ):
        return None
    digest = str(item.get("_wav_sha256") or "")
    relative = f"clips/{clip}/audio/{clip}.wav"
    if not SHA.fullmatch(digest) or int(item.get("wavBytes") or 0) < 1024:
        return None
    if not 60 <= float(item.get("duration") or 0) <= 360:
        return None
    # Catalog/warehouse drift > 5 sec blocks automatic admission.
    if abs(float(item["duration"]) - float(row["duration_seconds"])) > 5:
        return None
    return {
        "has_work": True,
        "clip_id": clip,
        "queue_id": int(row["queue_id"]),
        "title": str(item["title"]).strip(),
        "media_kind": kind,
        "channel": row["channel"],
        "apply_producer_tag": kind == "beat",
        "source_format": "wav",
        "source_type": "playback_derived_wav",
        "source_volume": "suno-playback-library",
        "source_modal_path": relative,
        "source_sha256": digest,
        "source_bytes": int(item["wavBytes"]),
        "duration_seconds": float(item["duration"]),
        "genre": str(row.get("style_tags") or "")[:450],
        "project_id": "autovideo_" + clip.replace("-", ""),
        "source_rights_verified": bool(row.get("permitted_download"))
        and row.get("rights_status") in {"APPROVED", "CLEARED", "OWNED_ORIGINAL"},
        "generation_preflight_ready": True,
        "generation_policy": "UNPUBLISHED_REVIEW_ONLY",
        "publishing_approved": False,
        "public_post_allowed": False,
        "status": "READY_FOR_DRAFT",
    }


def next_candidate() -> dict:
    catalog = _catalog()
    playable = []
    for row in _rows():
        item = catalog.get(str(row.get("clip_id")))
        if not item:
            continue
        result = _classify(row, item)
        if result:
            playable.append(result)
    mp3_song = _mp3_song_candidate(catalog=catalog)
    if mp3_song:
        playable.append(mp3_song)
    # Deterministically alternate beat/song preference hourly so either lane
    # receives work without starving the much larger vocal-song inventory.
    preferred = "song" if time.gmtime().tm_hour % 2 == 0 else "beat"
    for kind in (preferred, "beat" if preferred == "song" else "song"):
        for item in playable:
            if item["media_kind"] == kind:
                return item
    return {
        "has_work": False,
        "reason": "no_verified_modal_audio_in_both_media_lanes",
        "publishing_approved": False,
    }


def claim_draft(clip_id: str) -> dict:
    ident = str(UUID(str(clip_id)))
    # Re-evaluate the queue decision immediately and run an independent Modal
    # catalog file+volume listing check before leasing expensive generation.
    item = next_candidate()
    if item.get("clip_id") != ident:
        return {"claimed": False, "reason": "candidate_changed_or_not_next"}
    if item["source_format"] == "wav":
        from scripts.modal_wav_preflight import verify_song

        verified = verify_song(ident, item["title"])
        if not (
            verified.get("ok") is True
            and verified.get("catalog_sha256") == item["source_sha256"]
            and verified.get("modal_path") == item["source_modal_path"]
        ):
            raise ValueError("Canonical Modal WAV preflight failed")
    elif (
        item["media_kind"] == "song"
        and item["source_format"] == "mp3"
        and item["source_volume"] == "suno-songs-v2"
    ):
        # Never trust a PostgreSQL row alone: download from original Modal
        # volume during claim and compare actual bytes to its stored SHA.
        import hashlib

        with tempfile.TemporaryDirectory(prefix="music-factory-mp3-") as temp:
            dest = Path(temp) / f"{ident}.mp3"
            subprocess.run(
                [
                    str(MODAL),
                    "volume",
                    "get",
                    "suno-songs-v2",
                    item["source_modal_path"],
                    str(dest),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=180,
            )
            if (
                not dest.is_file()
                or dest.stat().st_size != item["source_bytes"]
                or hashlib.sha256(dest.read_bytes()).hexdigest()
                != item["source_sha256"]
            ):
                raise ValueError("Canonical song MP3 checksum/size verification failed")
            media = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration:stream=codec_type",
                    "-of",
                    "json",
                    str(dest),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=20,
            )
            checked = json.loads(media.stdout)
            if abs(
                float(checked.get("format", {}).get("duration") or 0)
                - item["duration_seconds"]
            ) > 5 or not any(
                st.get("codec_type") == "audio" for st in checked.get("streams", [])
            ):
                raise ValueError(
                    "Canonical song MP3 decode/duration verification failed"
                )
    else:
        raise ValueError("Unapproved catalog audio format")
    queue_id = int(item["queue_id"])
    project = item["project_id"]
    kind = item["media_kind"]
    channel = item["channel"]
    # Atomic: SKIP LOCKED + primary key prevents duplicate clip claims
    # across manual and scheduled n8n executions. Queue claim and registry
    # insert occur in one NUC PostgreSQL transaction.
    sql = f"""
    BEGIN;
    WITH one AS (
     SELECT q.queue_id,q.clip_id FROM media_library_video_queue q
     WHERE q.queue_id={queue_id} AND q.clip_id='{ident}'::uuid
       AND q.status IN ('READY','RETRY')
       AND q.attempts<q.max_attempts
       AND (q.next_attempt_at IS NULL OR q.next_attempt_at<=now())
     FOR UPDATE SKIP LOCKED
    ), reserve AS (
      INSERT INTO media_video_autopilot
      (clip_id,source_queue_id,project_id,media_kind,channel,stage)
      SELECT clip_id,queue_id,'{project}','{kind}','{channel}','CLAIMED'
      FROM one WHERE TRUE
      ON CONFLICT (clip_id) DO UPDATE
        SET stage='CLAIMED',
            attempts=media_video_autopilot.attempts+1,
            generation_result='{{}}'::jsonb,
            last_error=NULL,
            started_at=now(),updated_at=now(),
            lease_expires_at=now()+interval '5 hours'
      WHERE media_video_autopilot.stage='FAILED_RETRYABLE'
        AND media_video_autopilot.project_id=EXCLUDED.project_id
        AND media_video_autopilot.source_queue_id=EXCLUDED.source_queue_id
        AND media_video_autopilot.media_kind=EXCLUDED.media_kind
        AND media_video_autopilot.channel=EXCLUDED.channel
      RETURNING clip_id
    )
    UPDATE media_library_video_queue q SET status='CLAIMED',
      claimed_by='n8n-music-factory-v1',
      claimed_at=now(),lease_expires_at=now()+interval '5 hours',
      updated_at=now(),attempts=attempts+1,
      last_action='VIDEO_DRAFT_CLAIMED'
    FROM reserve r WHERE q.clip_id=r.clip_id
      AND q.queue_id={queue_id} AND q.status IN ('READY','RETRY')
    RETURNING q.queue_id;
    COMMIT;"""
    rows = psql(sql)
    claimed = str(queue_id) in rows
    if not claimed:
        return {"claimed": False, "reason": "already_reserved"}
    return {
        "claimed": True,
        **item,
        "status": "CLAIMED_FOR_UNPUBLISHED_GENERATION",
        "publishing_approved": False,
    }
