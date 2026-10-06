"""Exercise upstream sync against disposable local Git remotes."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def sync_lab(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "sync_upstream", Path(__file__).resolve().parents[2] / "tools/sync_upstream.py"
    )
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    def git(*args, cwd=None):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, text=True, capture_output=True
        ).stdout.strip()

    official, fork, seed, work = [
        tmp_path / name for name in ("upstream.git", "origin.git", "seed", "work")
    ]
    git("init", "--bare", str(official))
    git("init", "--bare", str(fork))
    git("init", "-b", "main", str(seed))
    git("config", "user.name", "Fixture", cwd=seed)
    git("config", "user.email", "fixture@example.invalid", cwd=seed)
    (seed / "base.txt").write_text("base\n")
    git("add", "base.txt", cwd=seed)
    git("commit", "-m", "base", cwd=seed)
    git("remote", "add", "upstream", str(official), cwd=seed)
    git("push", "upstream", "main", cwd=seed)
    git("clone", "-b", "main", str(official), str(work))
    git("config", "user.name", "Fixture", cwd=work)
    git("config", "user.email", "fixture@example.invalid", cwd=work)
    git("remote", "rename", "origin", "upstream", cwd=work)
    git("remote", "add", "origin", str(fork), cwd=work)
    git("switch", "-c", "codex/custom", cwd=work)
    (work / "custom.txt").write_text("ours\n")
    git("add", "custom.txt", cwd=work)
    git("commit", "-m", "custom", cwd=work)
    saved = git("rev-parse", "HEAD", cwd=work)
    (seed / "new.txt").write_text("upstream update\n")
    git("add", "new.txt", cwd=seed)
    git("commit", "-m", "upstream update", cwd=seed)
    git("push", "upstream", "main", cwd=seed)
    original_run = helper.run
    calls = []
    state = {"fail_checks": False}

    def routed(*args, capture=False):
        calls.append(args)
        if args[:3] == ("git", "remote", "get-url"):
            if args[-1] == "upstream":
                return "https://github.com/harry0703/MoneyPrinterTurbo.git"
            return "git@github.com:Bzcasper/MoneyPrinterTurbo.git"
        if args[0] == "uv":
            if state["fail_checks"]:
                raise subprocess.CalledProcessError(1, args)
            return ""
        if args[0] == "gh":
            assert args[3] == "Bzcasper/MoneyPrinterTurbo"
            return ""
        return original_run(*args, capture=capture)

    monkeypatch.chdir(work)
    monkeypatch.setattr(helper, "run", routed)
    monkeypatch.setattr(sys, "argv", ["sync", "--publish", "--set-default"])
    return helper, git, work, seed, fork, saved, calls, state


def test_sync_preserves_backup_and_publishes_fresh_branch(sync_lab):
    helper, git, work, _, fork, saved, calls, _ = sync_lab
    assert helper.main() == 0
    first = git("branch", "--show-current")
    first_head = git("rev-parse", "HEAD")
    assert (work / "custom.txt").read_text() == "ours\n"
    assert (work / "new.txt").is_file()
    assert git("rev-parse", f"refs/heads/{first}", cwd=fork) == first_head
    backups = git(
        "for-each-ref", "--format=%(objectname)", "refs/heads/codex/backup-*", cwd=fork
    )
    assert saved in backups.splitlines()
    assert helper.main() == 0
    assert git("branch", "--show-current") != first
    assert git("rev-parse", f"refs/heads/{first}", cwd=fork) == first_head
    assert any(call[0] == "gh" for call in calls)
    assert not any("--force" in part for call in calls for part in call)


def test_sync_refuses_unsaved_work(sync_lab):
    helper, git, work, _, _, saved, calls, _ = sync_lab
    (work / "dirty.txt").write_text("unsaved")
    with pytest.raises(SystemExit) as exc:
        helper.main()
    assert exc.value.code == 2
    assert git("rev-parse", "HEAD") == saved
    assert not any(call[:2] == ("git", "push") for call in calls)


def test_sync_refuses_main_instead_of_the_custom_branch(sync_lab):
    helper, git, _, _, _, _, calls, _ = sync_lab
    git("switch", "main")
    saved = git("rev-parse", "HEAD")
    with pytest.raises(SystemExit) as exc:
        helper.main()
    assert exc.value.code == 2
    assert git("rev-parse", "HEAD") == saved
    assert not any(call[:2] == ("git", "push") for call in calls)


def test_failed_checks_preserve_backup_without_publishing_result(sync_lab):
    helper, git, _, _, fork, saved, calls, state = sync_lab
    state["fail_checks"] = True
    assert helper.main() == 1
    candidate = git("branch", "--show-current")
    remote_branches = git("for-each-ref", "--format=%(refname)", "refs/heads", cwd=fork)
    assert f"refs/heads/{candidate}" not in remote_branches.splitlines()
    assert (
        saved
        in git(
            "for-each-ref", "--format=%(objectname)", "refs/heads", cwd=fork
        ).splitlines()
    )
    assert not any(call[0] == "gh" for call in calls)


def test_conflict_can_be_aborted_without_losing_original_work(sync_lab, monkeypatch):
    helper, git, work, seed, _, _, calls, _ = sync_lab
    (seed / "base.txt").write_text("upstream conflict\n")
    git("add", "base.txt", cwd=seed)
    git("commit", "-m", "upstream conflict", cwd=seed)
    git("push", "upstream", "main", cwd=seed)
    (work / "base.txt").write_text("local conflict\n")
    git("add", "base.txt")
    git("commit", "-m", "local conflict")
    source = git("branch", "--show-current")
    saved = git("rev-parse", "HEAD")
    monkeypatch.setattr(sys, "argv", ["sync"])
    assert helper.main() == 1
    assert Path(git("rev-parse", "--git-path", "rebase-merge")).is_dir()
    git("rebase", "--abort")
    git("switch", source)
    assert git("rev-parse", "HEAD") == saved
    assert (work / "base.txt").read_text() == "local conflict\n"
    assert not any(call[0] in ("uv", "gh") for call in calls)
