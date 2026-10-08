"""Strict local/NUC bridge: approved canonical still to deterministic motion file."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from scripts.render_canonical_still_motion import render_canonical_motion

ROOT = '/srv/data/n8n-media/store/'


def render_to_media(host_path: str, project_id: str, slot: int, duration: float) -> dict:
    if not host_path.startswith(ROOT) or '..' in Path(host_path).parts or Path(host_path).suffix.lower() not in {'.png','.jpg','.jpeg','.webp'}:
        raise ValueError('untrusted input image path')
    if not re.fullmatch('[A-Za-z0-9._-]{1,100}',project_id) or isinstance(slot,bool) or not 1<=slot<=10:
        raise ValueError('invalid project or slot')
    if not 2<=duration<=10:
        raise ValueError('invalid duration')
    media_host=os.environ.get('TYPEBEAT_REMOTE_MEDIA_HOST','').strip()
    if not media_host:
        raise ValueError('remote media host must be configured')
    work=Path.home()/'.cache'/'moneyprinterturbo'/'canon-motion'/project_id
    work.mkdir(parents=True,exist_ok=True)
    source=work/f'canonical-{slot:02d}{Path(host_path).suffix.lower()}'
    dest=work/f'canonical-motion-{slot:02d}.mp4'
    remote_dest=f'{ROOT}strictlybeats/canonical-motion/{project_id}/scene-{slot:02d}.mp4'
    if dest.exists() or source.exists():
        raise ValueError('refusing to overwrite an existing canonical-motion slot')
    subprocess.run(['scp','-q','-o','BatchMode=yes','-o','ConnectTimeout=10',f'{media_host}:{host_path}',str(source)],check=True,timeout=120)
    result=render_canonical_motion(source,dest,duration)
    subprocess.run(['ssh','-o','BatchMode=yes',media_host,'mkdir -p '+shlex.quote(str(Path(remote_dest).parent))],check=True,timeout=25)
    subprocess.run(['scp','-q','-o','BatchMode=yes',str(dest),f'{media_host}:{remote_dest}'],check=True,timeout=120)
    return {**result,'provider':'moneyprinter-image-motion','model':'canonical-ffmpeg-dolly-v1','host_path':remote_dest,'success':True,'reference_image_path':host_path}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--host-path',required=True)
    parser.add_argument('--project-id',required=True)
    parser.add_argument('--slot',type=int,required=True)
    parser.add_argument('--duration',type=float,default=5)
    args=parser.parse_args()
    print(json.dumps(render_to_media(args.host_path,args.project_id,args.slot,args.duration)))


if __name__=='__main__':main()


def derive_scene_still(host_path: str, project_id: str, ordinal: int, variant: int = 0) -> dict:
    """Compose a distinct camera crop from a canon frame; preserve objects exactly."""
    if not host_path.startswith(ROOT) or '..' in Path(host_path).parts or Path(host_path).suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp'}:
        raise ValueError('untrusted input image path')
    if not re.fullmatch('[A-Za-z0-9._-]{1,100}', project_id) or not isinstance(ordinal, int) or isinstance(ordinal, bool) or not 1 <= ordinal <= 50 or not isinstance(variant, int) or not 0 <= variant <= 9:
        raise ValueError('invalid project/ordinal/variant')
    media_host = os.environ.get('TYPEBEAT_REMOTE_MEDIA_HOST', '').strip()
    if not media_host:
        raise ValueError('remote media host must be configured')
    work = Path.home() / '.cache' / 'moneyprinterturbo' / 'canon-coverage' / project_id
    work.mkdir(parents=True, exist_ok=True)
    input_path = work / f'canon-{ordinal:03d}{Path(host_path).suffix.lower()}'
    output_path = work / f'scene-{ordinal:03d}.png'
    remote_output = f'{ROOT}strictlybeats/canonical-coverage/{project_id}/scene-{ordinal:03d}.png'
    if input_path.exists() or output_path.exists():
        raise ValueError('refusing to overwrite canonical coverage scene')
    subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', f'{media_host}:{host_path}', str(input_path)], check=True, timeout=120)
    # Nine stable crops: 3 zoom factors × 3 lateral offsets. No generative fill.
    zoom = [1.04, 1.10, 1.16][variant % 3]
    horizontal = [-0.7, 0.0, 0.7][(variant // 3) % 3]
    width = int(round(1920 * zoom))
    height = int(round(1080 * zoom))
    crop_x = f'(in_w-out_w)/2+({horizontal:.2f})*(in_w-out_w)/3'
    crop_y = '(in_h-out_h)/2'
    vf = f'scale={width}:{height}:force_original_aspect_ratio=increase,crop=1920:1080:x={crop_x}:y={crop_y},setsar=1'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', str(input_path), '-vf', vf, '-frames:v', '1', str(output_path)], check=True, timeout=90)
    if not output_path.is_file() or output_path.stat().st_size <= 10000:
        raise RuntimeError('derived coverage still is missing')
    subprocess.run(['ssh', '-o', 'BatchMode=yes', media_host, 'mkdir -p ' + shlex.quote(str(Path(remote_output).parent))], check=True, timeout=25)
    subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', str(output_path), f'{media_host}:{remote_output}'], check=True, timeout=120)
    return {'success': True, 'ordinal': ordinal, 'slot': ordinal, 'host_path': remote_output,
            'reference_image_path': host_path, 'model': 'canonical-ffmpeg-coverage-v1',
            'provider': 'moneyprinter-image-motion', 'generative': False}
