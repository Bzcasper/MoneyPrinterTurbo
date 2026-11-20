"""Local Wan 2.2 image-first motion provider for MoneyPrinterTurbo.

STAGED ONLY until a production-resolution native-motion canary is verified.
Never falls back to cloud credentials or paid endpoints. Generates on
ROG ComfyUI using approved local stills; fails closed on any QA failure.
"""
from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
import uuid
from urllib.request import Request, urlopen
import fcntl

COMFY = "http://127.0.0.1:8188"
STAGING = Path("/home/bobby/media-studio/wan2.2-canary")
COMFY_HOME = Path("/home/bobby/projects/ComfyUI")
INPUTS = COMFY_HOME / "input"
OUTPUTS = COMFY_HOME / "output"
DEST = Path("/home/bobby/.cache/scene-continuity/local-wan22/videos")
HEALTH = Path("/home/bobby/.cache/scene-continuity/video-provider-health.json")
APPROVED_IMAGE_ROOTS = (
    Path("/home/bobby/Videos/scene-director/automatic"),
    Path("/home/bobby/projects/ComfyUI/input"),
)
# The 8 GB ROG requires the independently SHA256-verified Kijai FP8 checkpoint.
# Its production use remains blocked until native motion QA proves this host.
CHECKPOINT = "Wan2_2-TI2V-5B_fp8_e4m3fn_scaled_KJ.safetensors"
CHECKPOINT_SHA256 = "a83f54a2450d5471e5721e59ab556afa2d8793e30280713e3796b254c5286b48"
# Production routing never loads giant weights through SSHFS. A separate
# SHA256-verified local ROG SSD bundle is required for fast, deterministic load.
MODEL_MOUNT = Path("/home/bobby/.local/share/media-studio/wan2.2-verified-local")
MANIFEST = MODEL_MOUNT / "local-verified-manifest.json"
MIN_W = 1280
MIN_H = 704
SENTINEL = "wan22_local"

class WanUnavailable(RuntimeError):
    """No verified, no-fee, image-first production route is enabled."""


def _http_json(path: str, *, payload: dict | None = None, timeout: int = 20):
    url = COMFY + path
    if payload is None:
        with urlopen(url, timeout=timeout) as res:
            return json.load(res)
    req=Request(url, data=json.dumps(payload).encode(),
                headers={"Content-Type":"application/json"}, method="POST")
    with urlopen(req, timeout=timeout) as res:
        return json.load(res)


def production_ready(*, health_file: Path = HEALTH, now: float | None = None) -> bool:
    """Low-resolution GPU canary cannot approve a production motion backend."""
    now=time.time() if now is None else now
    try: e=json.loads(health_file.read_text()).get(SENTINEL)
    except (OSError,ValueError,TypeError):return False
    if not isinstance(e,dict):return False
    try:
        created=float(e["verified_at"])
        width=int(e["native_width"])
        height=int(e["native_height"])
        duration=float(e["duration_seconds"])
    except (KeyError,ValueError,TypeError,OverflowError):return False
    expected_flags={
        "provider":SENTINEL,
        "native_motion_verified":True,
        "motion_verified":True,
        "source_image_first_verified":True,
        "no_external_api_credits":True,
        "production_resolution_verified":True,
        "approved_free_route":True,
        "factory_publishing_approved":False,
    }
    return (
        all(e.get(k)==v for k,v in expected_flags.items())
        and 0 <= now-created < 86400
        and width>=MIN_W and height>=MIN_H
        and 4.7<=duration<=5.3
        and bool(re.fullmatch(r"[a-f0-9]{64}",str(e.get("sha256") or "")))
    )


def verified_local_model_bundle() -> bool:
    """Require the correctly identified, checksum-verified local FP8 bundle."""
    try:
        manifest=json.loads(MANIFEST.read_text())
        if not (manifest.get("sha256_verified") is True
                and manifest.get("model")=="Wan2.2 TI2V-5B FP8 Kijai"):
            return False
        files=manifest["files"]
        if len(files)!=3:return False
        for file in files:
            name=file.get("rel")
            digest=file.get("sha256")
            size=file.get("size")
            if not isinstance(name,str) or not isinstance(size,int):
                return False
            if len(str(digest))!=64 or size<=0:return False
            target=(MODEL_MOUNT / name).resolve()
            if not target.is_relative_to(MODEL_MOUNT.resolve()):
                return False
            if not target.is_file() or target.stat().st_size!=size:return False
            if name.endswith(CHECKPOINT) and digest!=CHECKPOINT_SHA256:
                return False
        return any(f.get("rel")=="diffusion_models/"+CHECKPOINT for f in files)
    except (OSError,ValueError,TypeError,KeyError,RuntimeError):
        return False


def _checked_source(source: str | Path) -> Path:
    if not source:
        raise WanUnavailable("Approved local image-first anchor is required")
    try:
        p=Path(source).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WanUnavailable("Approved source image does not exist") from exc
    if p.suffix.lower() not in (".png",".jpg",".jpeg",".webp"):
        raise WanUnavailable("Source anchor must be a supported image")
    if not p.is_file():
        raise WanUnavailable("Source anchor is not a file")
    if p.stat().st_size>50*1024*1024:
        raise WanUnavailable("Source anchor exceeds bounded size")
    if not any(p.is_relative_to(root.resolve()) for root in APPROVED_IMAGE_ROOTS):
        raise WanUnavailable("Source anchor outside approved local asset directories")
    return p


def prepare_input(source: str | Path) -> str:
    """Copy/convert approved still into ComfyUI input; never replace source."""
    from PIL import Image,ImageOps
    p=_checked_source(source)
    digest=hashlib.sha256(p.read_bytes()).hexdigest()[:24]
    name="wan22-approved-"+digest+".png"
    output=INPUTS/name
    if output.is_file():return name
    with Image.open(p) as im:
        im.verify()
    with Image.open(p) as im:
        img=ImageOps.exif_transpose(im).convert("RGB")
        if img.width<512 or img.height<288:
            raise WanUnavailable("Anchor resolution insufficient")
        img.thumbnail((1920,1080),Image.Resampling.LANCZOS)
        output.parent.mkdir(parents=True,exist_ok=True)
        tmp=output.with_suffix(".partial.png")
        if tmp.is_file():raise WanUnavailable("Another anchor staging write exists")
        img.save(tmp,format="PNG",optimize=True)
        tmp.chmod(0o600)
        tmp.rename(output)
    return name


def build_plan(prompt: str, slot: int, image_input_name: str,
               *, steps: int = 20, width: int = MIN_W,
               height: int = MIN_H, length: int = 61) -> dict:
    if not isinstance(slot,int) or not (1<=slot<=50):
        raise ValueError("Scene slot out of range")
    text=" ".join(str(prompt).split())
    if not 12<=len(text)<=2000:
        raise ValueError("Prompt must contain 12-2000 characters")
    if not re.fullmatch(r"wan22-approved-[0-9a-f]{24}\.png", image_input_name):
        raise WanUnavailable("Unapproved image input filename")
    if width<MIN_W or height<MIN_H or width%32 or height%32:
        raise WanUnavailable("Native motion resolution must satisfy 720p source contract")
    if length<57 or (length-1)%4:
        raise WanUnavailable("Motion sequence must have sufficient native frames")
    if not 6<=steps<=40:
        raise WanUnavailable("Sampler must use 6-40 steps")
    graph=json.loads((STAGING/"api-prompt-fp8.json").read_text())
    assert graph["38"]["inputs"]["device"]=="cpu", "8GB GPU must use CPU text encoder"
    graph["37"]["inputs"]["unet_name"]=CHECKPOINT
    # Tiled video VAE decoding avoids the full-frame peak memory allocation.
    # This matches the live ComfyUI VAEDecodeTiled node schema.
    graph["8"]={
        "class_type":"VAEDecodeTiled",
        "inputs":{
            "samples":["3",0],"vae":["39",0],
            "tile_size":128,"overlap":64,
            "temporal_size":16,"temporal_overlap":4
        }
    }
    graph["56"]["inputs"]["image"]=image_input_name
    graph["55"]["inputs"].update(width=width,height=height,length=length,batch_size=1)
    graph["6"]["inputs"]["text"]=text
    graph["3"]["inputs"].update(steps=steps,seed=int.from_bytes(hashlib.sha256((text+str(slot)).encode()).digest()[:7],"big"))
    graph["57"]["inputs"]["fps"]=12
    graph["58"]["inputs"].update(filename_prefix="MediaStudio/Wan22_Factory_"+uuid.uuid4().hex[:12],format="mp4",codec="h264")
    return graph


def _video_from_history(history: dict) -> Path:
    if (history.get("status") or {}).get("status_str")!="success":
        raise WanUnavailable("ComfyUI execution failed: "+str(history.get("status",{}).get("messages",[]))[-550:])
    videos=[]
    for outputs in (history.get("outputs") or {}).values():
        for value in outputs.values():
            if not isinstance(value,list):continue
            for item in value:
                if isinstance(item,dict) and str(item.get("filename") or "").endswith(".mp4"):
                    videos.append(item)
    if len(videos)!=1:
        raise WanUnavailable("ComfyUI did not return exactly one native MP4")
    out=(OUTPUTS / str(videos[0].get("subfolder") or "") / videos[0]["filename"]).resolve()
    if not out.is_relative_to(OUTPUTS.resolve()) or not out.is_file():
        raise WanUnavailable("ComfyUI output path invalid")
    return out


def _probe(path: Path) -> dict:
    p=subprocess.run(["ffprobe","-v","error","-show_format","-show_streams","-of","json",str(path)],
                     check=True,capture_output=True,text=True,timeout=45)
    return json.loads(p.stdout)


def _moving_native(path: Path) -> dict:
    """Check actual source frames, before padding/transcoding to 720p."""
    p=_probe(path)
    streams=[s for s in p["streams"] if s.get("codec_type")=="video"]
    if len(streams)!=1:raise WanUnavailable("Expected one native video stream")
    s=streams[0]
    w,h=int(s["width"]),int(s["height"])
    if w<MIN_W or h<MIN_H:raise WanUnavailable("Native resolution below production minimum")
    dur=float(p["format"]["duration"])
    if not 4.7<=dur<=5.3:raise WanUnavailable("Native duration not five seconds")
    frames=subprocess.check_output(["ffmpeg","-nostdin","-v","error","-i",str(path),
        "-vf","fps=3,scale=48:32,format=gray","-f","rawvideo","-"],timeout=100)
    pieces=[frames[i:i+1536] for i in range(0,len(frames),1536) if len(frames[i:i+1536])==1536]
    if len(pieces)<10:raise WanUnavailable("Not enough decoded native frames")
    diffs=[sum(abs(a-b) for a,b in zip(x,y))/1536 for x,y in zip(pieces,pieces[1:])]
    if max(diffs or [0])<0.7 or len({hashlib.sha256(x).digest() for x in pieces})<5:
        raise WanUnavailable("No verifiable native video motion")
    return {"native_width":w,"native_height":h,"native_duration":dur,
            "decoded_sampled_frames":len(pieces),"max_mean_abs_luma":round(max(diffs),4)}


def run_approved_scene(prompt: str, slot: int, *, image_path: str | Path,
                       timeout: int = 1800) -> dict:
    """Generate one approved source-based motion clip, never publish."""
    if not production_ready():raise WanUnavailable("Production Wan motion provider is not verified")
    if not verified_local_model_bundle():
        raise WanUnavailable("Local FP8 model bundle missing or unverified")
    if not 240<=timeout<=2400:raise WanUnavailable("Timeout outside 240-2400 seconds")
    lock_path=STAGING/"production-wan-job.lock"
    lock_path.parent.mkdir(parents=True,exist_ok=True)
    with lock_path.open("a+") as f:
        try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:raise WanUnavailable("Another Wan job is running") from exc
        queue=_http_json("/queue")
        if queue["queue_running"] or queue["queue_pending"]:
            raise WanUnavailable("GPU queue busy")
        image_name=prepare_input(image_path)
        graph=build_plan(prompt,slot,image_name)
        result=_http_json("/prompt",payload={"prompt":graph,"client_id":"mpt-wan22-"+uuid.uuid4().hex},timeout=35)
        if result.get("node_errors") or not result.get("prompt_id"):
            raise WanUnavailable("ComfyUI rejected native video prompt")
        pid=result["prompt_id"]
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            histories=_http_json("/history/"+pid,timeout=18)
            history=histories.get(pid)
            if history is not None:break
            time.sleep(6)
        else:
            raise WanUnavailable("Wan video generation timed out; queue left for inspection")
        native=_video_from_history(history)
        quality=_moving_native(native)
        DEST.mkdir(parents=True,exist_ok=True)
        target=DEST/("scene-%03d-"%slot+pid+".mp4")
        if target.exists():raise WanUnavailable("Destination collision")
        partial=target.with_suffix(".partial.mp4")
        if partial.exists():raise WanUnavailable("Partial destination collision")
        try:
            cmd=["ffmpeg","-nostdin","-hide_banner","-loglevel","error","-i",str(native),
                 "-t","5","-an","-vf",
                 "scale=1280:720:force_original_aspect_ratio=decrease,"
                 "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black,fps=24",
                 "-c:v","libx264","-preset","veryfast","-crf","19",
                 "-pix_fmt","yuv420p","-movflags","+faststart","-y",str(partial)]
            subprocess.run(cmd,check=True,timeout=180,capture_output=True,text=True)
            final=_probe(partial)
            stream=next(x for x in final["streams"] if x.get("codec_type")=="video")
            if (int(stream["width"]),int(stream["height"]))!=(1280,720):
                raise WanUnavailable("Final output dimensions incorrect")
            if not 4.7<=float(final["format"]["duration"])<=5.3:
                raise WanUnavailable("Final output duration incorrect")
            partial.rename(target)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        digest=hashlib.sha256(target.read_bytes()).hexdigest()
        receipt={"success":True,"provider":SENTINEL,
                 "model":"Wan2.2 TI2V-5B FP16 (local)", "host_path":str(target),
                 "sha256":digest,"bytes":target.stat().st_size,
                 "no_external_api_credits":True,
                 "native_motion_verified":True,
                 "input_image_sha256":hashlib.sha256(Path(image_path).read_bytes()).hexdigest(),
                 "native_qa":quality, "duration_seconds":float(final["format"]["duration"]),
                 "factory_publishing_approved":False, "comfy_prompt_id":pid}
        target.with_suffix(".receipt.json").write_text(json.dumps(receipt,indent=2)+"\n")
        return receipt
