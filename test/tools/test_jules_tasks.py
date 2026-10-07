"""Offline checks for authenticated Jules submissions and receipt recovery."""

import io
import json
import sys
import urllib.error

import pytest

from tools import jules_tasks


class FakeAPI:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.requests = []

    def open(self, request, timeout):
        assert timeout == 30
        self.requests.append(request)
        answer = next(self.answers)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, bytes):
            return io.BytesIO(answer)
        return io.BytesIO(json.dumps(answer).encode())


@pytest.fixture
def manifest(tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text(
        json.dumps(
            {
                "repository": "Bzcasper/MoneyPrinterTurbo",
                "base_ref": "origin/codex/firefly-enhancements",
                "submission": {
                    "status": "submitting",
                    "submitted_sessions": 1,
                    "sessions": [{"task_id": "T01", "session_id": "801"}],
                },
                "tasks": [
                    {"id": task_id, "title": task_id, "prompt": f"Implement {task_id}"}
                    for task_id in ("T01", "T02", "T03")
                ],
            }
        )
    )
    return path


def http_error(code):
    # Echoed authentication material must never appear in runner output.
    body = io.BytesIO(
        json.dumps(
            {
                "error": {
                    "status": "FAILED_PRECONDITION",
                    "message": "fixture-only-secret",
                }
            }
        ).encode()
    )
    return urllib.error.HTTPError(
        "https://jules.googleapis.com/v1alpha/sessions", code, "failure", {}, body
    )


def run(monkeypatch, manifest, answers, limit=10):
    api = FakeAPI(answers)
    monkeypatch.setenv("JULES_API_KEY", "fixture-only-secret")
    monkeypatch.setattr(jules_tasks.urllib.request, "build_opener", lambda *args: api)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "jules_tasks.py",
            "submit",
            "--manifest",
            str(manifest),
            "--limit",
            str(limit),
        ],
    )
    result = jules_tasks.main()
    return result, api


SOURCE = {"name": "sources/github/Bzcasper/MoneyPrinterTurbo"}


def test_confirmed_receipt_survives_capacity_rejection_and_retry(
    monkeypatch, manifest, capsys
):
    result, api = run(
        monkeypatch, manifest, [SOURCE, {"name": "sessions/901"}, http_error(429)]
    )
    assert result == 1
    saved = json.loads(manifest.read_text())
    assert [row["task_id"] for row in saved["submission"]["sessions"]] == ["T01", "T02"]
    assert "dispatching_task" not in saved["submission"]
    posts = [json.loads(req.data) for req in api.requests if req.data]
    assert [row["title"] for row in posts] == ["MPT T02: T02", "MPT T03: T03"]
    assert all(row["automationMode"] == "AUTO_CREATE_PR" for row in posts)
    assert (
        posts[0]["sourceContext"]["githubRepoContext"]["startingBranch"]
        == "codex/firefly-enhancements"
    )

    result, api = run(monkeypatch, manifest, [SOURCE, {"id": "902"}], limit=1)
    assert result == 0
    assert len([req for req in api.requests if req.data]) == 1
    saved = json.loads(manifest.read_text())
    assert saved["submission"]["status"] == "submitted"
    assert saved["submission"]["submitted_sessions"] == 3
    assert "fixture-only-secret" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "error, ambiguous",
    [
        (lambda: http_error(400), False),
        (lambda: http_error(500), True),
        (lambda: http_error(408), True),
        (lambda: urllib.error.URLError("fixture-only-secret"), True),
    ],
)
def test_ambiguous_creation_blocks_resubmission(
    monkeypatch, manifest, capsys, error, ambiguous
):
    result, _ = run(monkeypatch, manifest, [SOURCE, error()], limit=1)
    assert result == 1
    saved = json.loads(manifest.read_text())
    assert bool(saved["submission"].get("dispatching_task")) is ambiguous
    assert saved["submission"]["submitted_sessions"] == 1
    if ambiguous:
        result, api = run(monkeypatch, manifest, [SOURCE], limit=1)
        assert result == 1
        assert all(req.data is None for req in api.requests)
    assert "fixture-only-secret" not in capsys.readouterr().out


def test_wrong_account_never_submits(monkeypatch, manifest, capsys):
    before = manifest.read_bytes()
    result, api = run(monkeypatch, manifest, [http_error(404)], limit=1)
    assert result == 1
    assert all(req.data is None for req in api.requests)
    assert manifest.read_bytes() == before
    assert "fixture-only-secret" not in capsys.readouterr().out


def test_private_env_file_required(monkeypatch, tmp_path):
    monkeypatch.delenv("JULES_API_KEY", raising=False)
    env = tmp_path / ".env.jules"
    env.write_text("export JULES_API_KEY='fixture-only-secret'\n")
    env.chmod(0o644)
    with pytest.raises(jules_tasks.APIError, match="chmod 600"):
        jules_tasks.load_key(env)
    env.chmod(0o600)
    assert jules_tasks.load_key(env) == "fixture-only-secret"


def test_send_message_accepts_empty_success_response(monkeypatch):
    api = FakeAPI([b""])
    monkeypatch.setattr(jules_tasks.urllib.request, "build_opener", lambda *args: api)
    assert (
        jules_tasks.request(
            "fixture-only-secret", "sessions/801:sendMessage", {"prompt": "Continue"}
        )
        == {}
    )
    assert len(api.requests) == 1


def test_activities_fetches_all_pages_privately(
    monkeypatch, manifest, tmp_path, capsys
):
    api = FakeAPI(
        [
            SOURCE,
            {"activities": [{"id": "first"}], "nextPageToken": "next+page"},
            {"activities": [{"id": "last", "message": "fixture-only-secret"}]},
        ]
    )
    monkeypatch.setenv("JULES_API_KEY", "fixture-only-secret")
    monkeypatch.setattr(jules_tasks.urllib.request, "build_opener", lambda *args: api)
    monkeypatch.setattr(jules_tasks.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        sys,
        "argv",
        ["jules_tasks.py", "activities", "--manifest", str(manifest), "--task", "T01"],
    )
    assert jules_tasks.main() == 0
    output = tmp_path / "mpt-jules-T01-activities.json"
    assert output.stat().st_mode & 0o077 == 0
    assert [row["id"] for row in json.loads(output.read_text())["activities"]] == [
        "first",
        "last",
    ]
    assert "pageToken=next%2Bpage" in api.requests[-1].full_url
    assert "fixture-only-secret" not in output.read_text()
    assert "fixture-only-secret" not in capsys.readouterr().out
