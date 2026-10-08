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
