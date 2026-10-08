from unittest.mock import patch

import pytest

from scripts.assemble_full_motion_project import validate_payload, assemble

CLIP = 'd512fc9b-5246-4488-a0fd-e5110247f1e9'
TITLE = 'VAMPMIN-VAR-003-R04-06954'


def sample(n=30):
    return {
        'project_id':'fullmotion-test', 'clip_id':CLIP, 'title':TITLE,
        'bpm':150, 'scene_count':n,
        'scene_video_assets':[
            {'ordinal':i,'host_path':f'/srv/data/n8n-media/store/firefly/videos/fullmotion-{i}.mp4',
             'provider':'firefly','model':'firefly-video','firefly_fair_use':True,
             'prompt':f'Scene {i}: physically continuous cinematic motion'}
            for i in range(1,n+1)],
    }


@pytest.mark.parametrize('n',[30,33,50])
def test_accepts_all_unique_video_scenes(n):
    pid,clip,count,bpm,assets=validate_payload(sample(n))
    assert (pid,clip,count,bpm)==('fullmotion-test',CLIP,n,150)
    assert len(assets)==n

@pytest.mark.parametrize('corrupt',[
    lambda a:a['scene_video_assets'].pop(),
    lambda a:a['scene_video_assets'][5].update({'ordinal':1}),
    lambda a:a['scene_video_assets'][0].update({'provider':'moneyprinter-image-motion'}),
    lambda a:a['scene_video_assets'][0].update({'firefly_fair_use':False}),
    lambda a:a['scene_video_assets'][0].update({'host_path':'/tmp/scene.mp4'}),
    lambda a:a['scene_video_assets'][0].update({'host_path':'/srv/data/n8n-media/store/firefly/videos/fullmotion-2.mp4'}),
])
def test_invalid_or_non_free_assets_fail_closed(corrupt):
    v=sample();corrupt(v)
    with pytest.raises(ValueError):validate_payload(v)


def test_rejects_non_wav_library_beat_before_writing():
    with patch('scripts.assemble_full_motion_project.psql_rows', side_effect=[[], [[TITLE,'t','91.88','/music/audio.mp3','source_mp3']]]):
        with pytest.raises(ValueError,match='original instrumental WAV'):
            assemble(sample())


def test_rejects_incorrect_track_identity_before_writing():
    with patch('scripts.assemble_full_motion_project.psql_rows', side_effect=[[], [['Other Beat','t','91.88','/music/audio.wav','wav_only']]]):
        with pytest.raises(ValueError,match='title must match'):
            assemble(sample())


def test_rejects_corrupt_or_short_generated_mp4():
    import json
    import subprocess
    from scripts.assemble_full_motion_project import probe_generated_motion
    sample = {'streams':[{'codec_name':'h264','width':1280,'height':720}],
              'format':{'duration':'0.8'}}
    with patch('scripts.assemble_full_motion_project.subprocess.run',return_value=subprocess.CompletedProcess([],0,json.dumps(sample),'')):
        with pytest.raises(ValueError,match='codec/resolution/duration'):
            probe_generated_motion('/srv/data/n8n-media/store/firefly/videos/short.mp4')


def test_accepts_valid_generated_video_probe():
    import json
    import subprocess
    from scripts.assemble_full_motion_project import probe_generated_motion
    sample={'streams':[{'codec_name':'h264','width':1280,'height':720}],
            'format':{'duration':'5.041667'}}
    with patch('scripts.assemble_full_motion_project.subprocess.run',return_value=subprocess.CompletedProcess([],0,json.dumps(sample),'')):
        assert probe_generated_motion('/srv/data/n8n-media/store/firefly/videos/good.mp4')['codec']=='h264'
