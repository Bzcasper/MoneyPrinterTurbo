"""Rebase local customizations on a fresh branch, preserving published history."""

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path


def run(*args, capture=False):
    result = subprocess.run(
        args, check=True, text=True, stdout=subprocess.PIPE if capture else None
    )
    return result.stdout.strip() if capture else ""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Save backup and verified result to origin",
    )
    parser.add_argument(
        "--set-default",
        action="store_true",
        help="Make the published branch the fork default",
    )
    args = parser.parse_args()
    if args.set_default and not args.publish:
        parser.error("--set-default requires --publish")

    root = Path(run("git", "rev-parse", "--show-toplevel", capture=True))
    if Path.cwd().resolve() != root.resolve():
        parser.error("Run this command from the repository root")
    source = run("git", "symbolic-ref", "--short", "HEAD", capture=True)
    if not source.startswith("codex/"):
        parser.error("Switch to the codex/ branch containing your customizations first")
    if run("git", "status", "--porcelain", "--untracked-files=all", capture=True):
        parser.error(
            "Commit or stash your changes first; the working tree must be clean"
        )
    for marker in (
        "rebase-merge",
        "rebase-apply",
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
    ):
        if Path(run("git", "rev-parse", "--git-path", marker, capture=True)).exists():
            parser.error("Finish or abort the existing Git operation first")

    upstream = run("git", "remote", "get-url", "upstream", capture=True)
    official = "harry0703/MoneyPrinterTurbo"
    if upstream.rstrip("/").removesuffix(".git") not in (
        f"https://github.com/{official}",
        f"git@github.com:{official}",
    ):
        parser.error(
            "upstream must point to the official harry0703/MoneyPrinterTurbo repository"
        )
    origin = run("git", "remote", "get-url", "--push", "origin", capture=True)
    origin_match = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?/?",
        origin,
    )
    if not origin_match:
        parser.error("origin must point to your GitHub fork")
    fork_repo = origin_match.group(1)
    if fork_repo.lower() == official.lower():
        parser.error("origin must point to your own fork, not official upstream")

    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%d-%H%M%S-%f")
    backup = f"codex/backup-{stamp}"
    candidate = f"codex/upstream-{stamp}"
    run("git", "branch", backup, "HEAD")
    print(f"Saved {source} as {backup}", flush=True)
    if args.publish:
        # A unique ref saves the original commits without rewriting remote history.
        run("git", "push", "origin", f"{backup}:refs/heads/{backup}")
    run("git", "fetch", "--prune", "upstream")
    run("git", "switch", "-c", candidate)
    try:
        run("git", "rebase", "upstream/main")
    except subprocess.CalledProcessError:
        print(
            f"Rebase stopped. Resolve conflicts and run git rebase --continue, "
            f"or git rebase --abort and git switch {source}. Backup: {backup}",
            file=sys.stderr,
        )
        return 1

    try:
        run("git", "diff", "--check")
        run(
            "uv",
            "run",
            "--locked",
            "ruff",
            "check",
            "app",
            "cli.py",
            "main.py",
            "webui",
            "test",
            "docs/skill",
            "tools/sync_upstream.py",
        )
        run(
            "uv",
            "run",
            "--locked",
            "python",
            "-m",
            "compileall",
            "-q",
            "app",
            "cli.py",
            "main.py",
            "webui",
            "test",
            "docs/skill",
            "tools/sync_upstream.py",
        )
        run(
            "uv",
            "run",
            "--locked",
            "python",
            "-X",
            "utf8",
            "-m",
            "coverage",
            "run",
            "-m",
            "pytest",
            "-q",
            "test",
        )
        run("uv", "run", "--locked", "python", "-m", "coverage", "report")
        if run("git", "status", "--porcelain", "--untracked-files=all", capture=True):
            raise RuntimeError(
                "Checks changed the working tree; review changes before publishing"
            )
    except (subprocess.CalledProcessError, RuntimeError):
        print(
            f"Verification failed; {candidate} was not published. Backup: {backup}",
            file=sys.stderr,
        )
        return 1

    if args.publish:
        run("git", "push", "-u", "origin", candidate)
        if args.set_default:
            run("gh", "repo", "edit", fork_repo, "--default-branch", candidate)
    print(f"Ready: {candidate}. Original branch: {source}. Backup: {backup}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"Sync stopped: {exc}", file=sys.stderr)
        sys.exit(1)
