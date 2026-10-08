#!/usr/bin/env python3
"""Run independent media/visual QA, then hold for human and rights review."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from scripts.assemble_typebeat_project import _q, psql_exec, psql_rows
from scripts.render_typebeat_project import TYPEBEAT_OUTPUT_ROOT

FOUNDATION = Path(os.environ.get('SUNO_FOUNDATION_ROOT', '/home/bobby/projects/suno-typebeat-foundation'))


def verify_project(project_id: str) -> dict:
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,100}',str(project_id or '')):
        raise ValueError('invalid project ID')
    output = TYPEBEAT_OUTPUT_ROOT / project_id / 'final' / 'moneyprinterturbo-master.mp4'
    manifest = output.with_suffix('.manifest.json')
    if not output.is_file() or not manifest.is_file():
        raise ValueError('full-motion video or manifest missing')
    report_tool = FOUNDATION/'scripts'/'verify_mpt_output.py'
    visual_tool = FOUNDATION/'scripts'/'qa_all_motion_visuals.py'
    if not report_tool.is_file() or not visual_tool.is_file():
        raise ValueError('independent QA tools are unavailable')
    result=subprocess.run([sys.executable,str(report_tool),str(output)],capture_output=True,text=True,timeout=120,check=True)
    technical=json.loads(result.stdout)
    if technical.get('technical_qa')!='PASS' or technical.get('adobe_full_motion') is not True:
        raise ValueError('independent full-motion technical QA failed')
    still_dir=output.parent/'review-frames'
    result=subprocess.run([sys.executable,str(visual_tool),str(manifest),'--out',str(still_dir)],capture_output=True,text=True,timeout=900,check=True)
    visual=json.loads(result.stdout)
    record=psql_rows(f'select project_id,status from media_video_projects where project_id={_q(project_id)} limit 1;')
    if not record or record[0][1] not in ('ASSETS_READY','QA_REVIEW'):
        raise ValueError('project not ready for QA')
    metadata={
        'technical_qa':'PASS','motion_qa':visual.get('technical_motion_gate'),
        'motion_review_warnings':visual.get('motion_warnings',0),
        'visual_repetition_warnings':visual.get('repetition_warnings',0),
        'render_sha256':technical['sha256'],'source_video_mode':'adobe_video_all_scenes',
        'rights_review':'PENDING','visual_review':'PENDING','listening_review':'PENDING',
        'publishing_approved':False,
    }
    psql_exec(f'UPDATE media_video_projects SET status=\'QA_REVIEW\',metadata=coalesce(metadata,\'{{}}\'::jsonb)||{_q(json.dumps(metadata))}::jsonb,updated_at=now() WHERE project_id={_q(project_id)} AND status IN (\'ASSETS_READY\',\'QA_REVIEW\');')
    return {'success':True,'project_id':project_id,'technical_qa':'PASS','motion_qa':visual.get('technical_motion_gate'),
            'visual_warnings':visual.get('review_flag_count',0),'scene_count':technical['scene_count'],
            'video_scene_count':technical['motion_scenes'],'image_scene_count':0,
            'render_path':str(output),'sha256':technical['sha256'],
            'contact_sheet':visual.get('contact_sheet'),
            'visual_review':'PENDING','rights_review':'PENDING','publishing_approved':False}


def main():
    a=argparse.ArgumentParser();a.add_argument('--project-id',required=True)
    opts=a.parse_args()
    print(json.dumps(verify_project(opts.project_id)))


if __name__=='__main__':main()
