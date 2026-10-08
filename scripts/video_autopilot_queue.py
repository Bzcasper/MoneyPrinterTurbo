"""Claim one canonical Suno WAV for n8n production, with fail-closed release checks.

The NUC is the canonical PostgreSQL source. All reads/writes use the existing
LAN SSH hop and parameter-free, UUID-validated SQL. Do not store auth secrets
or infer ownership from a downloaded playback WAV.
"""

from __future__ import annotations

import json
import subprocess
import re
import urllib.request
from pathlib import Path
from typing import Any
from uuid import UUID

DB_HOST = "bobby-nuc"
REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_video_autopilot (
 clip_id uuid PRIMARY KEY,
 source_queue_id bigint NOT NULL UNIQUE,
 project_id text NOT NULL UNIQUE,
 media_kind text NOT NULL CHECK (media_kind IN ('beat','song')),
 channel text NOT NULL,
 stage text NOT NULL DEFAULT 'CLAIMED',
 attempts integer NOT NULL DEFAULT 1,
 started_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 lease_expires_at timestamptz NOT NULL DEFAULT now() + interval '3 hours',
 generation_result jsonb NOT NULL DEFAULT '{}'::jsonb,
 qa_result jsonb NOT NULL DEFAULT '{}'::jsonb,
 last_error text,
 external_video_id text UNIQUE,
 published_at timestamptz
);
CREATE INDEX IF NOT EXISTS ix_media_autopilot_stage ON media_video_autopilot(stage,updated_at);
"""


def psql(sql: str) -> list[str]:
    completed = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            DB_HOST,
            "docker",
            "exec",
            "-i",
            "ai-postgres",
            "psql",
            "-X",
            "-q",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            "music_intel",
        ],
        input=sql,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            "Canonical PostgreSQL unavailable: " + completed.stderr[-330:]
        )
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def json_one(query: str) -> dict[str, Any]:
    rows = psql(query)
    if not rows:
        return {}
    return json.loads(rows[-1])


CANDIDATE = """
WITH candidates AS (
 SELECT q.queue_id, q.clip_id, q.project_type, q.channel, q.priority,
        m.title, m.make_instrumental, m.duration_seconds,
        m.commercial_status, m.download_unlocked, features.bpm,
        af.audio_file_id, af.sha256, af.path AS modal_path, af.format AS source_format, af.modal_volume,
        af.is_commercial_provenance_copy,
        r.rights_status, r.permitted_download_obtained,
        r.permitted_download_at, r.audio_sha256 AS rights_audio_sha,
        CASE WHEN q.project_type = 'type_beat' AND m.make_instrumental IS TRUE
                 AND q.channel = 'Strictly Beats' THEN 'beat'
             WHEN q.project_type = 'song' AND m.make_instrumental IS FALSE
                 AND q.channel = 'BC TRAP GOD' THEN 'song'
             ELSE NULL END AS media_kind
 FROM media_library_video_queue q
 JOIN music_assets m ON m.clip_id = q.clip_id
 JOIN LATERAL (
    SELECT a.audio_file_id,a.sha256,a.path,a.format,a.modal_volume,
           a.is_commercial_provenance_copy
    FROM audio_files a
    WHERE a.clip_id=q.clip_id
      AND a.modal_volume IN ('suno-official-master','suno-songs-v2','suno-songs','suno-library','suno-current','suno-music')
      AND a.format IN ('wav','mp3')
      AND a.sha256 ~ '^[0-9a-f]{64}$'
    ORDER BY a.is_commercial_provenance_copy DESC,
      CASE WHEN a.format='wav' THEN 0 ELSE 1 END,a.size_bytes DESC
    LIMIT 1
 ) af ON TRUE
 LEFT JOIN LATERAL (
   SELECT af.bpm FROM audio_features af WHERE af.clip_id=q.clip_id
     AND af.bpm BETWEEN 60 AND 220
   ORDER BY af.analyzed_at DESC NULLS LAST LIMIT 1
 ) features ON TRUE
 LEFT JOIN commercial_rights r ON r.clip_id = q.clip_id
 WHERE q.status IN ('READY','RETRY')
   AND q.next_attempt_at IS NULL OR FALSE
),
eligible AS (
 SELECT c.*, (c.permitted_download_obtained IS TRUE AND
   c.rights_status IN ('APPROVED','CLEARED','OWNED_ORIGINAL') AND
   c.rights_audio_sha = c.sha256 AND c.is_commercial_provenance_copy IS TRUE
 ) AS commercial_ready
 FROM candidates c WHERE c.media_kind IS NOT NULL
)
SELECT row_to_json(e)::text FROM eligible e
WHERE NOT EXISTS (SELECT 1 FROM media_video_autopilot x WHERE x.clip_id=e.clip_id
 AND x.stage NOT IN ('FAILED_RETRYABLE'))
ORDER BY e.commercial_ready DESC,e.priority DESC,e.queue_id
LIMIT 1;
"""
# Correct the precedence of optional queue retry scheduling:
CANDIDATE = CANDIDATE.replace(
    "AND q.next_attempt_at IS NULL OR FALSE",
    "AND (q.next_attempt_at IS NULL OR q.next_attempt_at <= now())",
)


def status() -> dict[str, Any]:
    psql(REGISTRY_SCHEMA)
    sql = """SELECT row_to_json(t)::text FROM (
 SELECT (SELECT count(*) FROM media_library_video_queue WHERE status IN ('READY','RETRY')) AS queued,
 (SELECT count(*) FROM media_video_autopilot WHERE stage IN ('CLAIMED','GENERATING','QA_REVIEW')) AS in_progress,
 (SELECT count(*) FROM commercial_rights WHERE permitted_download_obtained IS TRUE
     AND rights_status IN ('APPROVED','CLEARED','OWNED_ORIGINAL')) AS rights_cleared,
 (SELECT count(*) FROM media_video_autopilot WHERE stage='PUBLISHED') AS published
 ) t;"""
    return json_one(sql)


def candidate(*, dry_run: bool = True) -> dict[str, Any]:
    # DDL is deliberate and backward-compatible; the queue is not rewritten.
    psql(REGISTRY_SCHEMA)
    row = json_one(CANDIDATE)
    if not row:
        return {"has_work": False, "reason": "no_canonical_wav_candidate"}
    eligible = bool(row.get("commercial_ready"))
    result = dict(
        has_work=True,
        clip_id=str(UUID(str(row["clip_id"]))),
        queue_id=int(row["queue_id"]),
        title=str(row["title"])[:120],
        media_kind=row["media_kind"],
        apply_producer_tag=row["media_kind"] == "beat",
        channel=row["channel"],
        source_sha256=row["sha256"],
        source_modal_path=row["modal_path"],
        source_format=row["source_format"],
        source_modal_volume=row["modal_volume"],
        audio_file_id=row["audio_file_id"],
        duration_seconds=row["duration_seconds"],
        bpm=row.get("bpm"),
        generation_preflight_ready=(
            row["source_format"] == "wav"
            and row["modal_volume"] == "suno-playback-library"
            and row.get("bpm") is not None
        ),
        rights_ready=eligible,
        project_id="autovideo_" + str(row["clip_id"]).replace("-", ""),
        publishing_approved=False,
        auto_publish_allowed=False,
        status="READY_FOR_DRAFT" if dry_run else "CLAIMED",
        reason="commercial_provenance_missing"
        if not eligible
        else "qa_and_channel_signoff_required",
    )
    if dry_run:
        return result
    if result["rights_ready"] is not True:
        return {"has_work": False, "reason": "commercial_rights_not_cleared"}
    if result["generation_preflight_ready"] is not True:
        return {"has_work": False, "reason": "WAV_BPM_or_source_adapter_not_ready"}
    ident = result["clip_id"]
    project = result["project_id"]
    kind = result["media_kind"]
    channel = result["channel"]
    queue_id = result["queue_id"]
    # PK prevents duplicate jobs even if two scheduler invocations race.
    insert = f"""INSERT INTO media_video_autopilot(clip_id,source_queue_id,project_id,media_kind,channel)
 VALUES ('{ident}'::uuid,{queue_id},'{project}','{kind}','{channel}')
 ON CONFLICT DO NOTHING
 RETURNING clip_id;"""
    got = psql(insert)
    if len(got) != 1:
        return {"has_work": False, "reason": "already_claimed"}
    return result


def record_result(
    clip_id: str, *, stage: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    ident = str(UUID(str(clip_id)))
    if stage not in {"GENERATING", "QA_REVIEW", "FAILED_RETRYABLE", "FAILED_FINAL"}:
        raise ValueError("Autopilot stage not accepted")
    body = json.dumps(details or {}, separators=(",", ":"))
    if len(body) > 25000:
        raise ValueError("Metadata too large")
    escaped = body.replace("'", "''")
    statement = f"""UPDATE media_video_autopilot
SET stage='{stage}',generation_result='{escaped}'::jsonb,updated_at=now(),
last_error=CASE WHEN '{stage}' LIKE 'FAILED%' THEN left('{escaped}',1000) ELSE NULL END
WHERE clip_id='{ident}'::uuid AND stage IN ('CLAIMED','GENERATING','QA_REVIEW','FAILED_RETRYABLE','UPLOADING_PRIVATE')
RETURNING clip_id;"""
    rows = psql(statement)
    updated = len(rows) == 1
    if updated and stage == "FAILED_RETRYABLE":
        # Return failed claims to a bounded delayed retry queue. Exactly the
        # failed canonical clip and its original queue ID may be reopened.
        psql(f"""UPDATE media_library_video_queue q
          SET status=CASE WHEN q.attempts>=q.max_attempts THEN 'BLOCKED' ELSE 'RETRY' END,
              next_attempt_at=CASE WHEN q.attempts>=q.max_attempts THEN NULL
                   ELSE now()+interval '15 minutes' END,
              lease_expires_at=NULL,claimed_by=NULL,claimed_at=NULL,
              updated_at=now(),last_action='VIDEO_DRAFT_RETRY'
          FROM media_video_autopilot a
          WHERE a.clip_id='{ident}'::uuid AND q.queue_id=a.source_queue_id
            AND q.clip_id=a.clip_id AND a.stage='FAILED_RETRYABLE'
            AND q.status='CLAIMED';""")
    return {"updated": updated, "clip_id": ident, "stage": stage}


def release_check(clip_id: str) -> dict[str, Any]:
    ident = str(UUID(str(clip_id)))
    # Never accept approval flags from a webhook or rendering response.
    sql = f"""SELECT row_to_json(t)::text FROM (
 SELECT a.clip_id, a.project_id, a.media_kind, a.channel, a.stage,
   a.generation_result,
   COALESCE(m.download_unlocked,FALSE) AS download_unlocked,
   COALESCE(r.permitted_download_obtained,FALSE) AS permitted_download,
   COALESCE(r.rights_status,'') AS rights_status,
   (r.audio_sha256 = f.sha256 AND f.is_commercial_provenance_copy IS TRUE) AS provenance_sha_match,
   COALESCE(a.qa_result->>'human_approved','false') = 'true' AS human_approved,
   COALESCE(a.qa_result->>'technical_passed','false') = 'true' AS technical_passed,
   COALESCE(a.qa_result->>'visual_passed','false') = 'true' AS visual_passed,
   COALESCE(a.qa_result->>'originality_passed','false') = 'true' AS originality_passed,
   COALESCE(a.qa_result->>'channel_id_verified','false') = 'true' AS channel_verified
 FROM media_video_autopilot a
 JOIN music_assets m ON m.clip_id=a.clip_id
 LEFT JOIN commercial_rights r ON r.clip_id=a.clip_id
 LEFT JOIN LATERAL (
  SELECT sha256,is_commercial_provenance_copy FROM audio_files
  WHERE clip_id=a.clip_id AND modal_volume IN ('suno-official-master','suno-songs-v2','suno-songs','suno-library','suno-current','suno-music')
    AND format IN ('wav','mp3') AND sha256 ~ '^[0-9a-f]{{64}}$'
  ORDER BY is_commercial_provenance_copy DESC,size_bytes DESC LIMIT 1
 ) f ON TRUE
 WHERE a.clip_id='{ident}'::uuid
 ) t;"""
    row = json_one(sql)
    can = bool(
        row
        and row["stage"] in ("QA_REVIEW", "UPLOADING_PRIVATE")
        and row["download_unlocked"]
        and row["permitted_download"]
        and row["rights_status"] in {"APPROVED", "CLEARED", "OWNED_ORIGINAL"}
        and row["provenance_sha_match"]
        and all(
            row.get(k) is True
            for k in (
                "human_approved",
                "technical_passed",
                "visual_passed",
                "originality_passed",
                "channel_verified",
            )
        )
    )
    # Another YouTube channel needs an individually verified destination OAuth.
    if row.get("channel") != "Strictly Beats":
        can = False
    result = {
        "clip_id": ident,
        "release_ready": can,
        "posting_status": "HOLD" if not can else "APPROVED_FOR_PRIVATE_UPLOAD",
        "checks": {
            k: row.get(k)
            for k in (
                "stage",
                "download_unlocked",
                "permitted_download",
                "rights_status",
                "provenance_sha_match",
                "human_approved",
                "technical_passed",
                "visual_passed",
                "originality_passed",
                "channel_verified",
            )
        },
    }
    if not can:
        return result
    media = row.get("generation_result") or {}
    key = str(media.get("r2_asset_key") or "")
    digest = str(media.get("r2_sha256") or "")
    if not (
        key.startswith("projects/" + row["project_id"] + "/scenes/")
        and key.endswith(".mp4")
        and re.fullmatch("[a-f0-9]{64}", digest)
    ):
        result.update(
            release_ready=False, posting_status="HOLD_MISSING_VERIFIED_RENDER"
        )
        return result
    token_file = Path("/home/bobby/.config/scene-continuity-relay/media-token")
    if not token_file.is_file():
        result.update(
            release_ready=False, posting_status="HOLD_MEDIA_SIGNER_UNAVAILABLE"
        )
        return result
    request = urllib.request.Request(
        "https://scene-media.aitoolpool.com/v1/sign",
        data=json.dumps({"key": key, "ttlSec": 900}).encode(),
        headers={
            "Authorization": "Bearer " + token_file.read_text().strip(),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            receipt = json.load(response)
    except (OSError, ValueError):
        result.update(release_ready=False, posting_status="HOLD_MEDIA_SIGNING_FAILED")
        return result
    if receipt.get("sha256") != digest or not str(receipt.get("url") or "").startswith(
        "https://scene-media.aitoolpool.com/v1/media/" + key + "?"
    ):
        result.update(release_ready=False, posting_status="HOLD_MEDIA_HASH_MISMATCH")
        return result
    result.update(
        download_url=receipt["url"],
        target_channel_id="UCl9yINCZs9M2qItUXAnJwVg",
        youtube_title=str(media.get("youtube_title") or row["project_id"])[:100],
        youtube_description=str(
            media.get("youtube_description")
            or "Original production by BC / Strictly Beats."
        )[:4000],
        visibility="private",
        claimed_for_upload=row["stage"] == "UPLOADING_PRIVATE",
    )
    return result


def private_upload_candidates(limit: int = 10) -> dict[str, Any]:
    """Read-only selector; approvals are checked again during atomic upload claim."""
    count = max(1, min(int(limit), 10))
    rows = psql(f"""SELECT clip_id::text FROM media_video_autopilot
      WHERE stage='QA_REVIEW' AND external_video_id IS NULL
        AND qa_result @> '{{"human_approved":true,"technical_passed":true,
           "visual_passed":true,"originality_passed":true,"channel_id_verified":true}}'::jsonb
      ORDER BY updated_at ASC LIMIT {count};""")
    for clip in rows:
        gate = release_check(clip)
        if gate.get("release_ready") and gate.get("visibility") == "private":
            return {
                "has_work": True,
                "clip_id": clip,
                "channel": "Strictly Beats",
                "target_channel_id": gate.get("target_channel_id"),
                "status": "READY_FOR_PRIVATE_UPLOAD",
            }
    return {"has_work": False, "status": "RELEASE_HOLD", "checked": len(rows)}


def claim_private_upload(clip_id: str) -> dict[str, Any]:
    """Exactly-once private-upload reservation. Never changes visibility to public."""
    ident = str(UUID(str(clip_id)))
    gate = release_check(ident)
    if (
        gate.get("release_ready") is not True
        or gate.get("visibility") != "private"
        or gate.get("claimed_for_upload") is True
    ):
        return {"claimed": False, "reason": "release_gate_hold", "clip_id": ident}
    rows = psql(f"""UPDATE media_video_autopilot
    SET stage='UPLOADING_PRIVATE',updated_at=now()
    WHERE clip_id='{ident}'::uuid AND stage='QA_REVIEW' AND external_video_id IS NULL
    RETURNING clip_id;""")
    if len(rows) != 1:
        return {"claimed": False, "reason": "already_reserved", "clip_id": ident}
    return {
        "claimed": True,
        "clip_id": ident,
        "download_url": gate["download_url"],
        "title": gate["youtube_title"],
        "description": gate["youtube_description"],
        "visibility": "private",
        "channel": "Strictly Beats",
        "target_channel_id": gate["target_channel_id"],
    }


def record_private_upload(clip_id: str, upload_id: str) -> dict[str, Any]:
    ident = str(UUID(str(clip_id)))
    if not re.fullmatch(r"[A-Za-z0-9_-]{11,30}", str(upload_id)):
        raise ValueError("Invalid YouTube video identifier")
    # Only an upload with a previously reserved QA-passed token can be recorded.
    # Never recheck the release gate now: the reservation moved the stage away
    # from QA_REVIEW, preventing another scheduler from double-uploading.
    rows = psql(f"""UPDATE media_video_autopilot SET stage='UPLOADED_PRIVATE',
        external_video_id='{upload_id}',updated_at=now()
        WHERE clip_id='{ident}'::uuid AND stage='UPLOADING_PRIVATE' AND external_video_id IS NULL
        RETURNING clip_id;""")
    if len(rows) != 1:
        raise ValueError("Duplicate YouTube upload or invalid state")
    return {
        "recorded": True,
        "clip_id": ident,
        "youtube_video_id": upload_id,
        "visibility": "private",
        "publishing_approved": False,
    }


def claim_next_private_upload() -> dict[str, Any]:
    """Only a QA-approved, rights-cleared private render can enter the uploader.

    Claim occurs *before* exposing a signed download link to prevent parallel
    schedulers from uploading the same video twice.
    """
    rows = psql("""SELECT clip_id FROM media_video_autopilot
       WHERE stage='QA_REVIEW'
         AND qa_result @> '{"human_approved":true,"technical_passed":true,
            "visual_passed":true,"originality_passed":true,"channel_id_verified":true}'::jsonb
       ORDER BY updated_at ASC LIMIT 12;""")
    for ident in rows:
        try:
            preview = release_check(ident)
        except (ValueError, RuntimeError):
            continue
        if preview.get("release_ready") is not True:
            continue
        clip = str(UUID(ident))
        claimed = psql(f"""UPDATE media_video_autopilot
          SET stage='UPLOADING_PRIVATE',updated_at=now()
          WHERE clip_id='{clip}'::uuid AND stage='QA_REVIEW'
          RETURNING clip_id;""")
        if len(claimed) != 1:
            continue
        ready = release_check(clip)
        if ready.get("release_ready") and ready.get("claimed_for_upload"):
            ready["has_work"] = True
            return ready
        # Never allow a partial or missing asset to proceed after the claim.
        record_result(
            clip, stage="FAILED_FINAL", details={"reason": "signed_media_not_ready"}
        )
        break
    return {"has_work": False, "reason": "no_release_approved_private_video"}
