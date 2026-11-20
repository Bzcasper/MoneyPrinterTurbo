#!/usr/bin/env python3
"""Controlled 1280x704 Wan local production-feasibility benchmark.

Not an automatic factory-admission tool. Does not touch n8n job queues,
publishing rights, provider-health ledger or commercial entitlement checks.
Requires a real, independently verified FP8 smoke MP4 before GPU submission.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import time
import uuid

from scripts import music_factory_wan22_local as wan

SMOKE=wan.STAGING / 'ssd-smoke-native-motion-canary-evidence.json'
BENCHMARK=wan.STAGING / 'fp8-production-benchmark'
SOURCE=wan.INPUTS / 'media_studio_wan_canary_20261120.png'
PROMPT=(
 'A single continuous shot traveling slowly forward through a rain-slick '
 'blue-hour city street. Wet reflections ripple subtly under amber lamps, '
 'mist drifts between stable concrete buildings, and the camera gently '
 'pushes forward without cutting. Genuine visible temporal motion, '
 'cinematic realism, no people, no captions, no title, no logos.'
)
MIN_SECONDS=4.7
MAX_SECONDS=5.3


def checked_smoke() -> dict:
    if not SMOKE.is_file():
        raise wan.WanUnavailable('FP8 smoke output has not passed decoded-motion QA')
    evidence=json.loads(SMOKE.read_text())
    if not (
        evidence.get('success') is True
        and evidence.get('model_manifest_verified') is True
        and evidence.get('decoded_motion_qa',{}).get('motion_detected') is True
        and evidence.get('provider')=='comfyui_wan22_local_fp8'
        and evidence.get('factory_publishing_approved') is False
    ):
        raise wan.WanUnavailable('FP8 smoke evidence is incomplete or rejected')
    video=Path(str(evidence.get('file') or '')).resolve()
    if (not video.is_relative_to(wan.OUTPUTS.resolve())
            or not video.is_file() or wan._probe(video)['format'].get('duration') is None):
        raise wan.WanUnavailable('Smoke video file or duration missing')
    if wan.hashlib.sha256(video.read_bytes()).hexdigest()!=evidence.get('sha256'):
        raise wan.WanUnavailable('Smoke video changed since QA')
    return evidence


def plan() -> dict:
    if not wan.verified_local_model_bundle():
        raise wan.WanUnavailable('Verified FP8 model files missing')
    if not SOURCE.is_file():
        raise wan.WanUnavailable('Controlled benchmark image missing')
    source=wan.prepare_input(SOURCE)
    graph=wan.build_plan(PROMPT, 1, source, steps=6,
                         width=1280,height=704,length=61)
    assert graph['37']['inputs']['unet_name']==wan.CHECKPOINT
    assert graph['38']['inputs']['device']=='cpu'
    assert graph['8']['class_type']=='VAEDecodeTiled'
    assert graph['55']['inputs']['length']==61
    assert graph['57']['inputs']['fps']==12
    graph['58']['inputs']['filename_prefix']='MediaStudio/Wan22_FP8_full_resolution_feasibility_'+uuid.uuid4().hex[:12]
    return graph


def submit(timeout: int) -> int:
    if not 1800<=timeout<=14400:
        raise ValueError('A long-running native benchmark needs 1800-14400s timeout')
    smoke=checked_smoke()
    graph=plan()
    queue=wan._http_json('/queue')
    if queue['queue_running'] or queue['queue_pending']:
        raise wan.WanUnavailable('ComfyUI queue must be empty before benchmark')
    if BENCHMARK.exists():
        raise wan.WanUnavailable('Existing benchmark artifacts; refuse duplicate run')
    BENCHMARK.mkdir(parents=True,mode=0o700)
    (BENCHMARK/'graph.json').write_text(json.dumps(graph,indent=2)+'\n')
    (BENCHMARK/'smoke-evidence-sha256.txt').write_text(
        hashlib.sha256(SMOKE.read_bytes()).hexdigest()+'\n')
    print('PRODUCTION_BENCHMARK_SUBMIT_NATIVE_1280x704_61_FRAMES',flush=True)
    result=wan._http_json('/prompt',payload={
        'prompt':graph,'client_id':'wan22-production-benchmark-'+uuid.uuid4().hex
    },timeout=40)
    if result.get('node_errors') or not result.get('prompt_id'):
        raise wan.WanUnavailable('ComfyUI rejected benchmark: '+str(result)[:280])
    pid=result['prompt_id']
    (BENCHMARK/'prompt-id.txt').write_text(pid+'\n')
    print('PROMPT_ID',pid,flush=True)
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        history=wan._http_json('/history/'+pid,timeout=20).get(pid)
        if history:
            (BENCHMARK/'comfy-history.json').write_text(json.dumps(history,indent=2)+'\n')
            try:
                video=wan._video_from_history(history)
                motion=wan._moving_native(video)
                result={
                    'status':'technical_motion_verified',
                    'provider':'wan22_local',
                    'model':wan.CHECKPOINT,
                    'native_width':motion['native_width'],
                    'native_height':motion['native_height'],
                    'duration_seconds':motion['native_duration'],
                    'motion_qa':motion,
                    'file':str(video),
                    'sha256':wan.hashlib.sha256(video.read_bytes()).hexdigest(),
                    'input_sha256':wan.hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
                    'source_image_first_verified':False,
                    'semantic_qa_passed':False,
                    'approved_free_route':False,
                    'production_resolution_verified':True,
                    'native_motion_verified':True,
                    'no_external_api_credits':True,
                    'factory_publishing_approved':False,
                    'factory_provider_enrolled':False,
                    'comfy_prompt_id':pid,
                }
                (BENCHMARK/'technical-evidence.json').write_text(json.dumps(result,indent=2)+'\n')
                print(json.dumps(result,indent=2),flush=True)
                return 0
            except Exception as exc:
                (BENCHMARK/'error.txt').write_text(type(exc).__name__+': '+str(exc)+'\n')
                raise
        time.sleep(15)
    (BENCHMARK/'timeout.txt').write_text('ComfyUI may continue; check saved prompt ID and history.\n')
    raise wan.WanUnavailable('Benchmark runner timeout; preserved GPU queue')


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--check',action='store_true',help='Static graph check, no GPU job submitted')
    ap.add_argument('--run',action='store_true',help='Submit once only after smoke QA passes')
    ap.add_argument('--timeout',type=int,default=7200)
    opts=ap.parse_args()
    if opts.check:
        g=plan()
        print(json.dumps({
            'graph_nodes':len(g),'model':g['37']['inputs']['unet_name'],
            'resolution':[g['55']['inputs']['width'],g['55']['inputs']['height']],
            'frames':g['55']['inputs']['length'],
            'fps':g['57']['inputs']['fps'],
            'sampler_steps':g['3']['inputs']['steps'],
            'tiled_decode':g['8']['class_type']=='VAEDecodeTiled',
            'gpu_job_submitted':False,
        },indent=2))
    elif opts.run:
        return submit(opts.timeout)
    else:ap.error('Pass --check or --run explicitly')
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print(type(exc).__name__+': '+str(exc),flush=True)
        raise SystemExit(2)
