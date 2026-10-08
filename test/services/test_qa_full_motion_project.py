import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.qa_full_motion_project import verify_project


def test_rejects_unsafe_project_id():
    with pytest.raises(ValueError,match='invalid project'):
        verify_project('../outside')


def test_missing_master_rejected(tmp_path):
    with patch('scripts.qa_full_motion_project.TYPEBEAT_OUTPUT_ROOT',tmp_path):
        with pytest.raises(ValueError,match='missing'):
            verify_project('missing')


def test_visual_qa_sets_review_hold_not_publish(tmp_path):
    folder=tmp_path/'safe-test'/'final'
    folder.mkdir(parents=True)
    (folder/'moneyprinterturbo-master.mp4').write_bytes(b'video')
    (folder/'moneyprinterturbo-master.manifest.json').write_text('{}')
    technical={'technical_qa':'PASS','adobe_full_motion':True,'sha256':'a'*64,'scene_count':30,'motion_scenes':30}
    visual={'technical_motion_gate':'REVIEW','motion_warnings':1,'repetition_warnings':2,'review_flag_count':3,'contact_sheet':'contact.jpg'}
    result=[subprocess.CompletedProcess([],0,json.dumps(technical),''),subprocess.CompletedProcess([],0,json.dumps(visual),'')]
    with patch('scripts.qa_full_motion_project.TYPEBEAT_OUTPUT_ROOT',tmp_path),patch('scripts.qa_full_motion_project.subprocess.run',side_effect=result),patch('scripts.qa_full_motion_project.psql_rows',return_value=[['safe-test','ASSETS_READY']]),patch('scripts.qa_full_motion_project.psql_exec') as exec_sql:
        summary=verify_project('safe-test')
    assert summary['technical_qa']=='PASS'
    assert summary['motion_qa']=='REVIEW'
    assert summary['publishing_approved'] is False
    sql=exec_sql.call_args.args[0]
    assert "status='QA_REVIEW'" in sql
    assert 'publishing_approved' in sql
