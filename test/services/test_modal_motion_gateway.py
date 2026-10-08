import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.modal_motion_gateway import render_modal
from scripts.qa_modal_motion_project import verify


def test_modal_gateway_rejects_bad_project_and_size():
    with pytest.raises(ValueError, match="invalid project"):
        render_modal("../secret")
    with pytest.raises(ValueError, match="invalid even"):
        render_modal("valid-song", width=1279)


def test_modal_gateway_only_accepts_successful_unpublished_motion():
    data = {
        "status": "TECHNICAL_PASS",
        "project_id": "modal-gateway-fixture-927",
        "publishing_approved": False, "all_motion": True,
        "audio_volume": "suno-playback-library",
    }
    run = subprocess.CompletedProcess(
        [], 0, "RESULT_JSON:" + json.dumps(data) + "\n", ""
    )
    with patch("scripts.modal_motion_gateway.subprocess.run", return_value=run) as exec_mock:
        result = render_modal(data["project_id"])
    assert result["status"] == "TECHNICAL_PASS"
    assert "--execute" in exec_mock.call_args.args[0]
    bad = {**data, "publishing_approved": True}
    with patch("scripts.modal_motion_gateway.subprocess.run", return_value=subprocess.CompletedProcess(
        [], 0, "RESULT_JSON:" + json.dumps(bad), "")):
        with pytest.raises(RuntimeError, match="did not pass"):
            render_modal(data["project_id"])


def make_fixture(tmp_path: Path):
    pid = "qa-modal-fixture"
    folder = tmp_path / pid
    (folder / "qa").mkdir(parents=True)
    name = folder / "modal-motion-master.mp4"
    name.write_bytes(b"test-master")
    digest = hashlib.sha256(name.read_bytes()).hexdigest()
    uuid = "d512fc9b-5246-4488-a0fd-e5110247f1e9"
    wav = "a" * 64
    assets = [{"id": f"asset:{i}", "kind": "video"} for i in range(30)]
    scenes = [{"id": f"scene:{i}"} for i in range(30)]
    assignments = [{"sceneId": f"scene:{i}", "assetId": f"asset:{i}", "kind": "video"}
                   for i in range(30)]
    project = {"id": pid, "director": {"motionOnly": True},
               "scenes": scenes, "assets": assets,
               "audioSource": {"clipId": uuid, "sha256": wav},
               "analysis": {"durationSec": 91.8935}}
    result = {"status": "TECHNICAL_PASS", "project_id": pid,
              "clip_id": uuid, "audio_volume": "suno-playback-library",
              "source_wav_sha256": wav, "all_motion": True,
              "publishing_approved": False, "output_sha256": digest,
              "output_path": str(name)}
    audit = {"project_id": pid, "scene_count": 30, "boundary_count": 29,
             "video_file": str(name), "contact_sheet": str(folder / "qa" / "sheet.jpg"),
             "narrative_ready": False}
    for path, obj in [(folder / "project.json", project),
                      (folder / "assignments.json", assignments),
                      (folder / "result.json", result),
                      (folder / "qa/boundary-audit.json", audit)]:
        path.write_text(json.dumps(obj))
    probe = {"streams": [{"codec_type": "audio"}, {"codec_type": "video"}],
             "format": {"duration": "91.946667"}}
    return pid, folder, result, probe


def test_modal_qa_keeps_publish_hold_and_checks_source(tmp_path):
    pid, folder, result, probe = make_fixture(tmp_path)
    with patch("scripts.qa_modal_motion_project.subprocess.run", return_value=subprocess.CompletedProcess(
        [], 0, json.dumps(probe), "")), patch("scripts.qa_modal_motion_project.psql_rows",
        return_value=[[pid, "ASSETS_READY"]]), patch("scripts.qa_modal_motion_project.psql_exec") as sql:
        answer = verify(pid, workspace=folder)
    assert answer["technical_qa"] == "PASS"
    assert answer["video_scene_count"] == 30
    assert answer["image_scene_count"] == 0
    assert answer["publishing_approved"] is False
    assert answer["narrative_ready"] is False
    assert "status='QA_REVIEW'" in sql.call_args.args[0]
    assert '"publishing_approved": false' in sql.call_args.args[0]
    result["source_wav_sha256"] = "b" * 64
    (folder / "result.json").write_text(json.dumps(result))
    with pytest.raises(ValueError, match="source WAV digest"):
        verify(pid, workspace=folder, update_database=False)
