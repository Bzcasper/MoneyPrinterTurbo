#!/usr/bin/env python3
"""Assemble a strictly Adobe-fair-use all-motion type-beat episode.

Unlike the legacy 10-motion + still model, every scene requires its own actual
Firefly-generated video. Never converts frames to static image-motion scenes.
"""
from __future__ import annotations

import argparse
import json
import math
import shlex
import subprocess
import uuid
from pathlib import Path

from scripts.assemble_typebeat_project import (
    MEDIA_HOST,
    _q, _safe_id, beat_aligned_boundaries, psql_exec, psql_rows, remote_file_exists,
)


def validate_payload(payload: dict) -> tuple[str, str, int, float, list[dict]]:
    if not isinstance(payload, dict):
        raise ValueError('payload must be object')
    project_id = _safe_id(payload.get('project_id'), 'project_id')
    clip_id = str(uuid.UUID(str(payload.get('clip_id'))))
    scenes = payload.get('scene_video_assets')
    if not isinstance(scenes, list) or not 30 <= len(scenes) <= 50:
        raise ValueError('30-50 individual generated video scenes required')
    count = int(payload.get('scene_count') or 0)
    if count != len(scenes):
        raise ValueError('scene count does not match videos')
    bpm = float(payload.get('bpm') or 0)
    if not math.isfinite(bpm) or not 60 <= bpm <= 220:
        raise ValueError('actual analyzed BPM required')
    title = str(payload.get('title') or '').strip()
    if not title or len(title) > 120:
        raise ValueError('valid custom beat title required')
    seen, paths = set(), set()
    normalized = []
    for item in scenes:
        if not isinstance(item, dict):
            raise ValueError('invalid scene asset')
        ordinal = item.get('ordinal')
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1 or ordinal > count or ordinal in seen:
            raise ValueError('invalid/duplicate scene ordinal')
        seen.add(ordinal)
        path = str(item.get('host_path') or '')
        if not path.startswith('/srv/data/n8n-media/store/firefly/videos/') or '..' in Path(path).parts or not path.endswith('.mp4') or path in paths:
            raise ValueError('untrusted/duplicate Firefly MP4 path')
        paths.add(path)
        if item.get('provider') != 'firefly' or item.get('model') != 'firefly-video' or item.get('firefly_fair_use') is not True:
            raise ValueError('every scene must be zero-credit verified Firefly video')
        prompt = str(item.get('prompt') or '').strip()
        if not prompt or len(prompt) > 1500:
            raise ValueError('each video scene requires a concise prompt')
        normalized.append({'ordinal': ordinal, 'path': path, 'prompt': prompt})
    if seen != set(range(1, count + 1)):
        raise ValueError('scene ordinals are incomplete')
    return project_id, clip_id, count, bpm, sorted(normalized, key=lambda a:a['ordinal'])


def probe_generated_motion(path: str) -> dict:
    """Verify remote Firefly MP4 has playable video and adequate duration."""
    args = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_name,width,height:format=duration',
            '-of', 'json', path]
    command = (['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', MEDIA_HOST,
                shlex.join(args)] if MEDIA_HOST else args)
    result = subprocess.run(command, capture_output=True, text=True, timeout=35, check=True)
    payload = json.loads(result.stdout)
    stream = next((x for x in payload.get('streams', []) if x.get('codec_name')), None)
    duration = float(payload.get('format', {}).get('duration') or 0)
    if not stream or stream.get('codec_name') != 'h264' or int(stream.get('width') or 0) < 720 or int(stream.get('height') or 0) < 400 or duration < 4.5 or duration > 11:
        raise ValueError('Firefly clip failed video codec/resolution/duration gate')
    return {'codec': stream['codec_name'], 'width': stream['width'], 'height': stream['height'], 'duration': duration}


def assemble(payload: dict) -> dict:
    pid, clip, count, bpm, videos = validate_payload(payload)
    if psql_rows(f'select project_id from media_video_projects where project_id={_q(pid)} limit 1;'):
        raise ValueError('project already exists: do not overwrite')
    source = psql_rows('select a.title,a.make_instrumental,a.duration_seconds,m.canonical_source_path,m.canonical_source_kind '
                       'from music_assets a join v_canonical_track_media m using(clip_id) '
                       f'where a.clip_id={_q(clip)}::uuid and m.media_present=true limit 1;')
    if not source or len(source[0]) < 5:
        raise ValueError('actual custom beat missing from music warehouse')
    library_title, instrumental, length, wav, provenance = source[0]
    if instrumental != 't' or provenance not in {'official_master','wav_only'} or not wav.lower().endswith('.wav'):
        raise ValueError('original instrumental WAV required (playback-derived WAV prohibited)')
    if str(payload['title']).strip() != library_title.strip():
        raise ValueError('title must match actual music library record')
    duration = float(length)
    if not math.isfinite(duration) or duration < 60:
        raise ValueError('invalid real beat length')
    for item in videos:
        if not remote_file_exists(item['path']):
            raise ValueError(f'video scene {item["ordinal"]} is missing from shared media store')
        probe_generated_motion(item['path'])
    cuts = beat_aligned_boundaries(duration,bpm,count)
    style = str(payload.get('visual_style') or 'one coherent cinematic four-act story, no people')[:1300]
    projmeta = {'source_kind':provenance,'every_scene_video':True,'provider':'firefly-video','strict_unlimited':True,
                'credit_spending_allowed':False,'title_card':'animated_over_scene_1','bpm':bpm,'scene_count':count,
                'motion_scene_count':count,'publishing_approved':False}
    sql = ['BEGIN;','INSERT INTO media_video_projects (project_id,project_type,source_clip_id,title,status,commercial_gate_required,continuity_pack,visual_direction,metadata) VALUES '
           f'({_q(pid)},\'type_beat\',{_q(clip)}::uuid,{_q(library_title)},\'ASSETS_READY\',true,\'{{}}\'::jsonb,{_q(json.dumps({"style":style}))}::jsonb,{_q(json.dumps(projmeta))}::jsonb);']
    for item in videos:
        ordinal=item['ordinal']; sid=f'{pid}-scene-{ordinal:03d}'; aid=f'{pid}-video-{ordinal:03d}'
        start,end=cuts[ordinal-1:ordinal+1]
        prompt=item['prompt']
        scene='INSERT INTO media_video_scenes (scene_id,project_id,ordinal,section_name,source_text,start_seconds,end_seconds,timeline_mode,location_key,asset_preference,visual_prompt,motion_prompt,continuity_refs,qa_requirements,status,director_brief,prompt_optimized,prompt_engine,scene_type,provider_strategy,continuity_priority,motion_priority,visual_priority,needs_anchor,scene_state,max_candidates,negative_constraints) VALUES '
        scene+=f'({_q(sid)},{_q(pid)},{ordinal},\'type-beat\',{_q("actual Adobe-generated motion")},{start:.6f},{end:.6f},\'present\',\'cinematic-four-act\',\'video\',{_q(prompt)},{_q(prompt)},\'[]\'::jsonb,\'{{}}\'::jsonb,\'PLANNED\',\'original continuous motion\',\'{{}}\'::jsonb,\'{{}}\'::jsonb,\'motion\',\'firefly-unlimited-video\',\'high\',\'high\',\'high\',true,\'READY\',1,\'no people or random text\');'
        meta={'billing_mode':'firefly_fair_use','reference':'generated_video','image_motion':False,'scene_ordinal':ordinal}
        asset='INSERT INTO media_video_assets (asset_id,project_id,scene_id,asset_type,provider,model,uri,local_path,prompt,metadata,status,asset_role,qa_status,lock_status) VALUES '
        asset+=f'({_q(aid)},{_q(pid)},{_q(sid)},\'video\',\'firefly\',\'firefly-video\',{_q(item["path"])},{_q(item["path"])},{_q(prompt)},{_q(json.dumps(meta))}::jsonb,\'APPROVED\',\'locked_video\',\'APPROVED\',\'LOCKED\');'
        sql.extend([scene,asset,f'UPDATE media_video_scenes SET locked_video_asset_id={_q(aid)},scene_state=\'LOCKED\' WHERE scene_id={_q(sid)};'])
    sql.append('COMMIT;')
    psql_exec('\n'.join(sql))
    return {'ok':True,'project_id':pid,'source_clip_id':clip,'title':library_title,'duration_seconds':duration,
            'scene_count':count,'video_scene_count':count,'image_scene_count':0,'bpm':bpm,'tag_required':True,
            'publishing_approved':False}


def main() -> None:
    p=argparse.ArgumentParser();p.add_argument('--payload',required=True);a=p.parse_args()
    print(json.dumps(assemble(json.loads(Path(a.payload).read_text(encoding='utf-8')))))


if __name__=='__main__':main()
