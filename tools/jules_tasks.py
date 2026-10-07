#!/usr/bin/env python3
"""Submit the scoped MoneyPrinterTurbo tasks through the Jules REST API."""

import argparse
import json
import os
import re
import shlex
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
API = "https://jules.googleapis.com/v1alpha"


class APIError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_key(path):
    key = os.environ.get("JULES_API_KEY", "").strip()
    if key:
        return key
    if not path.exists():
        raise APIError(f"Set JULES_API_KEY or create {path} with mode 600.")
    if path.stat().st_mode & 0o077:
        raise APIError(f"Run chmod 600 {path} before using this credential file.")
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            match = re.match(r"\s*(?:export\s+)?JULES_API_KEY\s*=\s*(.*)$", line)
            if match:
                values = shlex.split(match.group(1), comments=True)
                if len(values) == 1 and values[0]:
                    return values[0]
                break
    raise APIError("JULES_API_KEY is missing or empty; never paste it into logs.")


def request(key, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        API + "/" + path,
        data=data,
        headers={"X-Goog-Api-Key": key, "Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(req, timeout=30) as response:
            body = response.read()
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as error:
        with error:
            try:
                status = json.load(error).get("error", {}).get("status", "")
            except (ValueError, AttributeError):
                status = ""
        safe_status = (
            status
            if isinstance(status, str) and re.fullmatch(r"[A-Z_]{1,64}", status)
            else "API_ERROR"
        )
        raise APIError(f"HTTP {error.code} {safe_status}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise APIError(
            f"Request failed ({type(error).__name__}); inspect session status before retrying."
        ) from None


def save_manifest(path, manifest):
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def submit(key, path, manifest, limit):
    submission = manifest["submission"]
    if submission.get("dispatching_task"):
        raise APIError(
            "An earlier submission has no confirmed receipt. Inspect Jules sessions "
            "and reconcile dispatching_task before retrying."
        )
    receipts = submission.setdefault("sessions", [])
    completed = {receipt["task_id"] for receipt in receipts}
    tasks = [task for task in manifest["tasks"] if task["id"] not in completed]
    for task in tasks[:limit]:
        submission["dispatching_task"] = task["id"]
        save_manifest(path, manifest)
        try:
            session = request(
                key,
                "sessions",
                {
                    "title": f"MPT {task['id']}: {task['title']}",
                    "prompt": task["prompt"],
                    "sourceContext": {
                        "source": f"sources/github/{manifest['repository']}",
                        "githubRepoContext": {
                            "startingBranch": manifest["base_ref"].removeprefix(
                                "origin/"
                            )
                        },
                    },
                    "requirePlanApproval": False,
                    "automationMode": "AUTO_CREATE_PR",
                },
            )
        except APIError as error:
            # A definitive rejected request did not create a session.
            if re.match(r"HTTP 4(?!08)[0-9]{2} ", str(error)):
                submission.pop("dispatching_task", None)
                save_manifest(path, manifest)
            raise
        session_id = str(
            session.get("id") or session.get("name", "").rsplit("/", 1)[-1]
        )
        if not re.fullmatch(r"[0-9]+", session_id):
            raise APIError(
                "The API returned no usable session ID; reconcile dispatching_task before retrying."
            )
        receipt = {
            "task_id": task["id"],
            "title": task["title"],
            "session_id": session_id,
            "url": f"https://jules.google.com/session/{session_id}",
            "transport": "rest_api",
        }
        receipts.append(receipt)
        submission.pop("dispatching_task", None)
        submission["submitted_sessions"] = len(receipts)
        submission["status"] = (
            "submitted" if len(receipts) == len(manifest["tasks"]) else "submitting"
        )
        save_manifest(path, manifest)
        print(json.dumps(receipt))
    print(json.dumps({"submitted": len(receipts), "total": len(manifest["tasks"])}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("check", "status", "submit", "activities", "message")
    )
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.jules")
    parser.add_argument("--manifest", type=Path, default=ROOT / "docs/JULES_TASKS.json")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--task", help="Task ID for activities or message")
    parser.add_argument("--message-file", type=Path)
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 10:
        parser.error("--limit must be between 1 and 10")
    try:
        key = load_key(args.env_file)
        with args.manifest.open(encoding="utf-8") as handle:
            manifest = json.load(handle)
        source = request(key, f"sources/github/{manifest['repository']}")
        if source.get("name") != f"sources/github/{manifest['repository']}":
            raise APIError("The API did not confirm the requested repository source.")
        if args.command == "check":
            print(
                json.dumps(
                    {"JULES_API_KEY": "present", "repository_access": "verified"}
                )
            )
        elif args.command == "status":
            for receipt in manifest["submission"]["sessions"]:
                session = request(key, f"sessions/{receipt['session_id']}")
                print(
                    json.dumps(
                        {
                            "task_id": receipt["task_id"],
                            "session_id": receipt["session_id"],
                            "state": session.get("state", "UNKNOWN"),
                        }
                    )
                )
        elif args.command in ("activities", "message"):
            receipt = next(
                (
                    row
                    for row in manifest["submission"]["sessions"]
                    if row["task_id"] == args.task
                ),
                None,
            )
            if receipt is None:
                raise APIError("Choose an existing submitted task with --task.")
            if args.command == "message":
                if args.message_file is None:
                    raise APIError(
                        "Provide --message-file; keep credentials out of task messages."
                    )
                prompt = args.message_file.read_text(encoding="utf-8")
                request(
                    key,
                    f"sessions/{receipt['session_id']}:sendMessage",
                    {"prompt": prompt},
                )
                print(json.dumps({"task_id": args.task, "message": "sent"}))
            else:
                activities = {"activities": []}
                page_token = ""
                seen_tokens = set()
                for _ in range(20):
                    query = {"pageSize": 100}
                    if page_token:
                        query["pageToken"] = page_token
                    page = request(
                        key,
                        f"sessions/{receipt['session_id']}/activities?{urlencode(query)}",
                    )
                    activities["activities"].extend(page.get("activities", []))
                    page_token = page.get("nextPageToken", "")
                    if not page_token:
                        break
                    if page_token in seen_tokens:
                        raise APIError("Activity pagination repeated a page token.")
                    seen_tokens.add(page_token)
                else:
                    raise APIError("Activity pagination exceeded 20 pages.")
                output = json.dumps(activities, indent=2).replace(key, "[REDACTED]")
                output = re.sub(r"[A-Za-z0-9_-]{32,}", "[REDACTED]", output)
                output = re.sub(
                    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[EMAIL]", output
                )
                destination = (
                    Path(tempfile.gettempdir())
                    / f"mpt-jules-{args.task}-activities.json"
                )
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(output + "\n")
                print(
                    json.dumps(
                        {"task_id": args.task, "activities_file": str(destination)}
                    )
                )
        else:
            submit(key, args.manifest, manifest, args.limit)
    except (APIError, OSError, KeyError, TypeError) as error:
        # Exception text from local filesystem/schema failures contains no credentials.
        print(
            f"Jules: {error}"
            if isinstance(error, APIError)
            else f"Jules: {type(error).__name__}"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
