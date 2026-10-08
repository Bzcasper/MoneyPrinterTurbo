"""Unpublished all-motion Suno library video worker.

A queued clip is verified against Modal; 30 native Firefly motion videos are
generated with independent QA, then rendered against the untouched official WAV.
Beat previews get BC producer tag. Vocal songs DO NOT get the producer tag.
Public posting cannot occur here: only the separate rights/QA gate can upload.
"""

from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request
from zipfile import ZIP_STORED, ZipFile

ROOT = Path("/home/bobby/Videos/scene-director/automatic")
REPO = Path("/home/bobby/projects/suno-typebeat-foundation")
MODAL = REPO / ".venv/bin/modal"
FIRE_URL = "http://10.0.0.242:5678/webhook/strictlybeats-unlimited-firefly-video"
PLAN_URL = "http://10.0.0.242:5678/webhook/strictlybeats-full-motion-wav"
MPT_URL = "http://127.0.0.1:8080/api/v1/internal/type-beat/autopilot/report"
UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
REMOTE_VIDEO = re.compile(
    r"^/srv/data/n8n-media/store/firefly/videos/fullmotion-[0-9]+-scene-(\d+)\.mp4$"
)
SHA = re.compile(r"^[a-f0-9]{64}$")

# This worker is executed by absolute script path from the daemon, not via
# `python -m`: explicitly make the repository package importable.
if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def call(args: list[str | Path], *, timeout: int = 150) -> str:
    r = subprocess.run(
        [str(x) for x in args], capture_output=True, text=True, timeout=timeout
    )
    if r.returncode:
        raise RuntimeError(
            f"subprocess {Path(str(args[0])).name} exited {r.returncode}: {r.stderr[-850:]}"
        )
    return r.stdout


def post(url: str, payload: dict, timeout: int = 120) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "curl/8.5.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def probe(path: Path) -> dict:
    return json.loads(
        call(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration,size:stream=codec_type,codec_name,width,height",
                "-of",
                "json",
                path,
            ],
            timeout=30,
        )
    )


def moving(path: Path) -> bool:
    info = probe(path)
    dur = float(info["format"]["duration"])
    if not (
        4.7 <= dur <= 5.3
        and any(
            x.get("codec_type") == "video"
            and x.get("width", 0) >= 1280
            and x.get("height", 0) >= 720
            for x in info["streams"]
        )
    ):
        return False
    frames = []
    for t in (0.25, 2.4, 4.6):
        p = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-ss",
                str(t),
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                "scale=80:45",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            capture_output=True,
            timeout=20,
            check=True,
        )
        if len(p.stdout) != 80 * 45 * 3:
            return False
        frames.append(p.stdout)
    diff = sum(abs(a - b) for a, b in zip(frames[0], frames[2])) / (
        len(frames[0]) * 255
    )
    return diff > 0.008


def generated_prompt(scene: dict, treatment: dict, slot: int, kind: str) -> str:
    fragments = [
        f"SCENE {slot:02d}/30; ACT {scene['master_beat']}/10.",
        f"EPISODE STORY: {str(treatment['thesis'])[:103]}.",
        f"SAME PHYSICAL CANON: {str(treatment['motif'])[:91]}.",
        f"PREVIOUS FRAME: {str(scene['opening_state'])[:95]}.",
        f"NATIVE VIDEO ACTION: {str(scene['action'])[:145]}.",
        f"SET: {str(scene['location'])[:90]}. CAMERA: {str(scene['camera'])[:95]}.",
        f"PHYSICAL RHYTHM: {str(scene.get('music_reaction', 'natural parallax and physical light response'))[:120]}.",
        f"NEXT FRAME: {str(scene['end_state'])[:95]}.",
        f"LIGHTING: {str(treatment.get('lighting', ''))[:92]}.",
        "Real moving 5-second shot; physical parallax, no freeze, montage, text, logo, WebGL or equalizer.",
        "Negative constraints: no face drift, no changing facial features, no hairstyle changes, no outfit changes, no age changes, no body proportion changes, no art style shift, no unintended photorealism/3D shift, no extra limbs, no distorted hands, no inconsistent colors, no random accessories, no changed eye color, no altered silhouette.",
    ]
    if kind == "song":
        fragments.insert(
            3,
            "BC TRAP GOD song. No invented performer or character identity.",
        )
    result = " ".join(fragments)
    if len(result) > 1485:
        raise ValueError(
            f"Scene {slot} prompt exceeds safe provider limit ({len(result)})"
        )
    return result


def story(clip: dict, folder: Path) -> dict:
    output = folder / "story.json"
    if output.is_file():
        return json.loads(output.read_text())
    bpm = float(clip.get("bpm") or 120)
    title = clip["title"]
    payload = {
        "clip_id": clip["clip_id"],
        "title": title,
        "bpm": bpm,
        "scene_count": 30,
        "project_id": clip["project_id"],
        "media_kind": clip["media_kind"],
        "genre": clip.get("genre") or "original music",
        "mood": clip.get("mood") or "cinematic, narrative motion",
        "plan_only": True,
        "publishing_approved": False,
    }
    # Different media lanes must never share an instrumental's character/brand
    # bible. If OmniRoute is unavailable, preserve production progress with
    # an explicitly review-only deterministic storyboard. This does not claim
    # image-conditioned visual continuity.
    from scripts.music_factory_story import build_story

    treatment = None
    if (
        clip["media_kind"] == "beat"
        and os.getenv("MUSIC_FACTORY_REMOTE_DIRECTOR") == "1"
    ):
        try:
            result = post(PLAN_URL, payload, timeout=65)
            if result.get("success") is True and result.get("mode") == "PLAN_ONLY":
                treatment = result.get("director_treatment")
        except (OSError, TimeoutError, ValueError) as exc:
            print("DIRECTOR_FALLBACK", type(exc).__name__, flush=True)
    if not isinstance(treatment, dict):
        treatment = build_story(clip)

    scenes = treatment.get("scenes") or []
    if len(scenes) != 30 or sorted(set(x.get("master_beat") for x in scenes)) != list(
        range(1, 11)
    ):
        raise ValueError("Storyboard lacks 30 scenes / ten master beats")
    if any(
        scenes[i]["master_beat"] < scenes[i - 1]["master_beat"]
        or scenes[i]["opening_state"].strip() != scenes[i - 1]["end_state"].strip()
        for i in range(1, 30)
    ):
        raise ValueError("Storyboard resets motion handoff between adjacent scenes")
    for i, s in enumerate(scenes, 1):
        generated_prompt(s, treatment, i, clip["media_kind"])
    output.write_text(json.dumps(treatment, indent=2, ensure_ascii=False) + "\n")
    return treatment


def _normalize_video_length(path: Path) -> None:
    """Convert longer provider-native clips to the approved five-second edit unit."""
    data = probe(path)
    seconds = float(data.get("format", {}).get("duration") or 0)
    if 4.7 <= seconds <= 5.3:
        return
    if not 5.3 < seconds <= 20:
        raise ValueError("Video candidate has unsupported source duration")
    normalized = path.with_suffix(".normalized.mp4")
    call(
        [
            "ffmpeg", "-y", "-nostdin", "-v", "error", "-i", path,
            "-t", "5", "-an", "-vf",
            "scale=1280:720:force_original_aspect_ratio=decrease,"
            "pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=24",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", normalized,
        ],
        timeout=120,
    )
    normalized.replace(path)


def video_scene(
    slot: int, scene: dict, treatment: dict, folder: Path, kind: str
) -> dict:
    from scripts.music_factory_provider_router import (
        request_video, validated_path, scene_provider_order
    )

    target = folder / "scenes" / f"scene-{slot:03d}.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    receipt_path = folder / "scenes" / f"scene-{slot:03d}.json"
    if target.is_file() and moving(target):
        receipt = json.loads(receipt_path.read_text()) if receipt_path.is_file() else {}
        return {
            "slot": slot,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "already_present": True,
            "provider": receipt.get("provider", "previous_verified"),
        }
    prompt = generated_prompt(scene, treatment, slot, kind)
    errors = []
    for provider in scene_provider_order(slot):
        for attempt in range(2 if provider == "firefly" else 1):
            partial = target.with_suffix(".partial.mp4")
            try:
                result = request_video(provider, prompt, slot, timeout=380)
                remote = validated_path(provider, result, slot=slot)
                call(
                    ["scp", "-q", "-o", "BatchMode=yes", "bobby-nuc:" + remote, partial],
                    timeout=100,
                )
                if provider != "firefly":
                    _normalize_video_length(partial)
                if not moving(partial):
                    raise ValueError("Provider output failed decoded-frame motion QA")
                partial.replace(target)
                receipt = {
                    "slot": slot,
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "bytes": target.stat().st_size,
                    "provider": provider,
                    "model": str(result.get("model") or provider)[:95],
                    "zero_credits_verified": (
                        result.get("firefly_fair_use") is True
                        and result.get("credit_spending_allowed") is False
                        if provider == "firefly" else False
                    ),
                    "source_video": True,
                }
                receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
                return receipt
            except Exception as exc:
                partial.unlink(missing_ok=True)
                error = f"{provider}:{type(exc).__name__}:{str(exc)[:130]}"
                errors.append(error)
                print(f"VIDEO_PROVIDER_RETRY {slot} {error}", flush=True)
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"scene {slot} failed: {'; '.join(errors)[-350:]}")


def render(clip: dict, treatment: dict, folder: Path) -> dict:
    out = folder / "final"
    out.mkdir(parents=True, exist_ok=True)
    project = clip["project_id"]
    sources = [folder / "scenes" / f"scene-{n:03d}.mp4" for n in range(1, 31)]
    if not all(x.is_file() and moving(x) for x in sources):
        raise RuntimeError("30/30 real motion sources required")
    package = folder / (project + ".zip")
    with ZipFile(package, "w", compression=ZIP_STORED, allowZip64=True) as archive:
        for item in sources:
            archive.write(item, arcname=item.name)
    call(
        [
            MODAL,
            "volume",
            "put",
            "music-video-render-assets",
            package,
            "factory/" + package.name,
        ],
        timeout=420,
    )
    title = str(treatment.get("display_title") or clip["title"])
    title = re.sub(r"[^a-zA-Z0-9 '&-]", "", title).strip()
    if not title:
        title = "New Release"
    title = title[:64]
    output = project + "-review.mp4"
    args = [
        MODAL,
        "run",
        REPO / "scripts/modal_octane_editorial_master.py",
        "--pack-name",
        package.name,
        "--output-name",
        output,
        "--scene-count",
        "30",
        "--clip-id",
        clip["clip_id"],
        "--project-id",
        project,
        "--wav-sha256",
        clip["source_sha256"],
        "--media-kind",
        clip["media_kind"],
        "--source-volume",
        clip["source_volume"],
        "--source-relative-path",
        clip["source_modal_path"],
        "--asset-prefix",
        "factory",
        "--title",
        title,
        "--subtitle",
        str(treatment.get("thesis") or "Original music")[:58].replace(":", ""),
    ]
    report = call(args, timeout=3700)
    (folder / "modal-render.log").write_text(report[-8000:])
    file = out / output
    call(
        [
            MODAL,
            "volume",
            "get",
            "music-video-renders",
            f"factory/{project}/{output}",
            file,
        ],
        timeout=350,
    )
    info = probe(file)
    secs = float(info["format"]["duration"])
    if abs(secs - float(clip["duration_seconds"])) > 0.25 or not any(
        x.get("codec_type") == "audio" for x in info["streams"]
    ):
        raise ValueError("Final video duration/audio failed QA")
    payload = dict(
        project_id=project,
        source_clip_id=clip["clip_id"],
        media_kind=clip["media_kind"],
        native_motion_scenes=30,
        still_scenes=0,
        webgl_enabled=False,
        producer_tag_applied=clip["media_kind"] == "beat",
        title=str(treatment.get("display_title") or clip["title"]),
        original_wav_sha256=clip["source_sha256"],
        output_path=str(file),
        output_sha256=hashlib.sha256(file.read_bytes()).hexdigest(),
        output_bytes=file.stat().st_size,
        output_duration=secs,
        strict_first_frame_continuity_passed=False,
        human_approved=False,
        visual_approved=False,
        rights_approved=False,
        publishing_approved=False,
        result="QA_REVIEW",
    )
    return payload


def upload_to_r2(clip: dict, report: dict) -> dict:
    from scripts.music_factory_delivery import prepare_delivery

    original_file = Path(report["output_path"])
    try:
        file, delivery = prepare_delivery(
            original_file, duration_seconds=float(report["output_duration"])
        )
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        return {
            **report,
            "r2_upload_status": "HOLD_DELIVERY_TRANSCODE_FAILED",
            "r2_delivery_error": type(exc).__name__,
        }
    if file.stat().st_size > 64 * 1024 * 1024:
        return {**report, "r2_upload_status": "HOLD_FILE_EXCEEDS_WORKER_CAP"}
    token_file = Path("/home/bobby/.config/scene-continuity-relay/media-token")
    if not token_file.is_file():
        return {**report, "r2_upload_status": "HOLD_MISSING_WORKER_TOKEN"}
    key = f"projects/{clip['project_id']}/scenes/{clip['project_id']}-review.mp4"
    url = "https://scene-media.aitoolpool.com/v1/media/" + key
    req = urllib.request.Request(
        url,
        data=file.read_bytes(),
        method="PUT",
        headers={
            "Authorization": "Bearer " + token_file.read_text().strip(),
            "X-Content-SHA256": delivery["delivery_sha256"],
            "X-Clip-ID": clip["clip_id"],
            "X-Origin": "modal_auto_video",
            "Content-Type": "video/mp4",
            "User-Agent": "curl/8.5.0",
        },
    )
    with urllib.request.urlopen(req, timeout=170) as response:
        body = json.load(response)
    if body.get("ok") is not True or body.get("sha256") != delivery["delivery_sha256"]:
        raise RuntimeError("R2 final video checksum failed")
    return {
        **report,
        **delivery,
        "r2_asset_key": key,
        "r2_sha256": delivery["delivery_sha256"],
        "r2_upload_status": "VERIFIED_PRIVATE",
        "youtube_title": report["title"],
        "youtube_description": f"{report['title']} — Original music video. Channel: {clip['channel']}. AI-generated cinematic footage.",
    }


def update(clip: dict, stage: str, details: dict) -> None:
    try:
        result = post(
            MPT_URL,
            {"clip_id": clip["clip_id"], "stage": stage, "details": details},
            timeout=35,
        )
        if not (result.get("data") or result).get("updated"):
            raise RuntimeError("NUC autopilot report rejected")
    except Exception as exc:
        print(
            f"REPORT_WARNING {stage}: {type(exc).__name__}:{str(exc)[:170]}", flush=True
        )


def main(request_file: Path) -> None:
    clip = json.loads(request_file.read_text())
    assert UUID.fullmatch(clip["clip_id"])
    assert clip["media_kind"] in {"beat", "song"}
    assert clip["apply_producer_tag"] is (clip["media_kind"] == "beat")
    assert SHA.fullmatch(clip["source_sha256"])
    folder = ROOT / clip["project_id"]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "work-request.json").write_text(json.dumps(clip, indent=2) + "\n")
    try:
        update(clip, "GENERATING", {"stage": "PLANNING", "publishing_approved": False})
        treatment = story(clip, folder)
        print("STORY_ACCEPTED", clip["clip_id"], clip["media_kind"], flush=True)
        results = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                pool.submit(video_scene, i, s, treatment, folder, clip["media_kind"]): i
                for i, s in enumerate(treatment["scenes"], 1)
            }
            for future in as_completed(futures):
                v = future.result()
                results.append(v)
                print(
                    "MOTION_SCENE_VERIFIED", v["slot"], len(results), "/30", flush=True
                )
        report = render(clip, treatment, folder)
        report = upload_to_r2(clip, report)
        (folder / "final" / "AUTOMATED_TECHNICAL_QA.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
        update(clip, "QA_REVIEW", report)
        print(
            "AUTOMATED_REVIEW_MASTER_COMPLETE",
            clip["project_id"],
            report["output_path"],
            flush=True,
        )
    except BaseException as exc:
        update(
            clip,
            "FAILED_RETRYABLE",
            {"error": str(exc)[:400], "publishing_approved": False},
        )
        raise


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: music_factory_worker.py REQUEST_JSON")
    main(Path(sys.argv[1]))
