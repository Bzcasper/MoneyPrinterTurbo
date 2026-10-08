import pytest

from app.controllers.v1.video import _parse_json_cli_output


def test_accepts_single_json_result():
    assert _parse_json_cli_output('{"ok":true,"count":30}\n') == {'ok':True,'count':30}


def test_ignores_startup_log_before_json():
    text = '2026-10-08 | INFO | load config from file\n2026-10-08 | INFO | MoneyPrinterTurbo v1.3.8\n{"success":true,"project_id":"test"}\n'
    assert _parse_json_cli_output(text) == {'success':True,'project_id':'test'}


def test_does_not_accept_only_logs():
    with pytest.raises(ValueError,match='no JSON'):
        _parse_json_cli_output('INFO application initialized\n')
