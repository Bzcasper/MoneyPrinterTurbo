"""Allocate varied Jules work and merge only tracked, protected, fully checked PRs.

`plan` is offline. `tick --dry-run` reads remote state without writing it. Live
ticks must be serialized by the caller; the ledger additionally uses GitHub's
expected content SHA so a competing writer cannot silently overwrite history.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from tools.jules_tasks import APIError, load_key

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"COMPLETED", "FAILED"}
RETIRED = {"merged", "closed", "abandoned"}
SHA = re.compile(r"^[0-9a-f]{40}$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_PAGES = 1000
MAX_RESPONSE_BYTES = 4_000_000
MAX_STATE_BYTES = 950_000
CATEGORY_PATTERNS = {
    "hardening": r"requests\.|httpx\.|urlopen|Path\(|open\(|validate|raise ValueError",
    "stability": r"except |finally:|timeout|retry|cancel|close\(",
    "enhancement": r"^\s*(async\s+)?def [A-Za-z_]\w*\(|st\.|add_argument\(",
    "performance": r"for .* in |sorted\(|cache|read\(|\.json\(|list\(",
    "tests": r"^\s*(async\s+)?def [A-Za-z_]\w*\(",
}
OBJECTIVES = {
    "hardening": "Verify one evidenced input, path, network or resource boundary. "
    "Fix a reproduced validation, redaction, redirect or resource-limit defect, "
    "or add a failure-sensitive boundary regression if the behavior is sound.",
    "stability": "Inject deterministic failure, cancellation or retry faults in "
    "this function. Fix one proven recovery or cleanup defect and demonstrate "
    "consistent terminal state without repeating uncertain provider work.",
    "enhancement": "Implement one small compatible user-facing improvement "
    "supported by the function, its callers or an authorized Linear issue. "
    "Prefer actionable validation, useful controls or clear progress/error "
    "behavior. Prove the new behavior; do not scaffold a replacement app.",
    "performance": "Measure the evidenced work with synthetic inputs. Bound "
    "or remove one demonstrated memory/complexity hotspot and add a scaling "
    "regression. Use operation counts or bounds, not flaky elapsed-time tests.",
    "tests": "Find one externally observable branch in this function that lacks "
    "a failure-sensitive regression. Add deterministic coverage and fix any "
    "reproduced defect. Do not write assertions that mirror the implementation.",
}
RESTORE_CHECK = (
    "Preserve the existing successful VM setup/snapshot; do not rerun setup or "
    "resnapshot. In each restored shell, verify only the fixture port and Redis "
    "PING. Disable shell tracing. If fixture env is missing, source "
    "$HOME/.mpt-jules-env when present; otherwise export the known fixture "
    "constants MPT_RUN_INTEGRATION_TESTS=0, MPT_TEST_REDIS_HOST=127.0.0.1, "
    "MPT_TEST_REDIS_PORT=6389 and MPT_TEST_REDIS_DB=15. Require these isolated "
    "constants before testing. Print only MPT_TEST_REDIS_PORT, then run "
    "redis-cli -h 127.0.0.1 -p 6389 ping. If Redis is absent, use mpt-redis-up "
    "when available, otherwise run redis-server --bind 127.0.0.1 --port 6389 "
    "--save '' --appendonly no --daemonize yes --pidfile /tmp/mpt-jules-redis.pid "
    "--dir /tmp, then verify PING again. Never print other env values."
)


class ControllerError(RuntimeError):
    """Safe error codes, never provider response text or credential values."""

    def __init__(self, code, status=None):
        super().__init__(code)
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class JSONClient:
    def __init__(self, base, headers, opener=None):
        self.base = base.rstrip("/")
        self.headers = {**headers, "Content-Type": "application/json"}
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def request(self, method, path, payload=None):
        if not path.startswith("/") or path.startswith("//") or "#" in path:
            raise ControllerError("invalid_api_path")
        request = urllib.request.Request(
            self.base + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers=self.headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise ControllerError(f"http_{status}", status) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ControllerError("transport_uncertain") from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise ControllerError("response_too_large")
        try:
            return json.loads(body) if body.strip() else {}
        except (ValueError, UnicodeError):
            raise ControllerError("invalid_json_response") from None


def escaped(value):
    return urllib.parse.quote(str(value), safe="")


def safe_text(value, limit=500):
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\b[A-Za-z0-9_-]{32,}\b", "[REDACTED]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", text)
    text = re.sub(
        r"(?i)((?:api[_-]?key|password|secret|token)\s*[:=]\s*)[\"'][^\"']*[\"']",
        r"\1[REDACTED]",
        text,
    )
    return text[:limit]


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError):
        return None


def iso(now):
    return now.astimezone(UTC).isoformat().replace("+00:00", "Z")


def session_id(session):
    if not isinstance(session, dict):
        raise ControllerError("invalid_session_resource")
    value = str(session.get("id") or session.get("name", "").rsplit("/", 1)[-1])
    if not IDENTIFIER.fullmatch(value):
        raise ControllerError("invalid_session_identifier")
    return value


class Jules:
    def __init__(self, client):
        self.client = client

    def sessions(self):
        result, tokens, token = {}, set(), ""
        for _ in range(MAX_PAGES):
            path = "/sessions?pageSize=100"
            if token:
                path += "&pageToken=" + escaped(token)
            page = self.client.request("GET", path)
            if not isinstance(page, dict) or not isinstance(
                page.get("sessions", []), list
            ):
                raise ControllerError("invalid_session_page")
            for session in page.get("sessions", []):
                result[session_id(session)] = session
            token = page.get("nextPageToken", "")
            if not token:
                return list(result.values())
            if not isinstance(token, str) or token in tokens:
                raise ControllerError("session_pagination_cycle")
            tokens.add(token)
        raise ControllerError("session_pagination_limit")

    def get(self, identifier):
        return self.client.request("GET", "/sessions/" + escaped(identifier))

    def create(self, policy, task, prompt):
        return self.client.request(
            "POST",
            "/sessions",
            {
                "title": task["title"],
                "prompt": prompt,
                "sourceContext": {
                    "source": "sources/github/" + policy["repository"],
                    "githubRepoContext": {"startingBranch": policy["base_branch"]},
                },
                "requirePlanApproval": False,
                "automationMode": "AUTO_CREATE_PR",
            },
        )

    def message(self, identifier, prompt):
        return self.client.request(
            "POST",
            "/sessions/" + escaped(identifier) + ":sendMessage",
            {"prompt": prompt},
        )

    def approve(self, identifier):
        return self.client.request(
            "POST", "/sessions/" + escaped(identifier) + ":approvePlan", {}
        )


class GitHub:
    def __init__(self, client, repository):
        self.client = client
        self.prefix = "/repos/" + repository

    def request(self, method, path, payload=None):
        return self.client.request(method, self.prefix + path, payload)

    def pages(self, path, key=None):
        items = []
        for page_number in range(1, MAX_PAGES + 1):
            separator = "&" if "?" in path else "?"
            response = self.request(
                "GET", f"{path}{separator}per_page=100&page={page_number}"
            )
            if key and not isinstance(response, dict):
                raise ControllerError("invalid_github_page")
            page = response.get(key, []) if key else response
            if not isinstance(page, list):
                raise ControllerError("invalid_github_page")
            items.extend(page)
            if len(page) < 100:
                return items
        raise ControllerError("github_pagination_limit")

    def branch_sha(self, branch):
        response = self.request("GET", "/git/ref/heads/" + escaped(branch))
        value = response.get("object", {}).get("sha", "")
        if not SHA.fullmatch(value):
            raise ControllerError("invalid_branch_sha")
        return value


def new_state(repository):
    return {"version": 1, "repository": repository, "tasks": [], "seen_keys": []}


class Ledger:
    def __init__(self, github, policy):
        self.github, self.policy, self.revision = github, policy, None
        self.path = "/contents/" + escaped(policy["state_path"])

    def load(self):
        try:
            response = self.github.request(
                "GET", self.path + "?ref=" + escaped(self.policy["state_branch"])
            )
        except ControllerError as error:
            if error.status == 404:
                return new_state(self.policy["repository"])
            raise
        try:
            content = base64.b64decode(
                response["content"].replace("\n", ""), validate=True
            )
            if len(content) > MAX_STATE_BYTES:
                raise ControllerError("ledger_too_large")
            state = json.loads(content)
            self.revision = response["sha"]
            if (
                state.get("version") != 1
                or state.get("repository") != self.policy["repository"]
                or not isinstance(state.get("tasks"), list)
                or not isinstance(state.get("seen_keys"), list)
                or not SHA.fullmatch(self.revision)
            ):
                raise ControllerError("invalid_ledger")
            return state
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise ControllerError("invalid_ledger") from None

    def save(self, state):
        # Keep recent outcomes for prompts and every work key for durable dedup.
        live = [task for task in state["tasks"] if task.get("outcome") not in RETIRED]
        retired = [task for task in state["tasks"] if task.get("outcome") in RETIRED]
        state["tasks"] = retired[-300:] + live
        state["seen_keys"] = sorted(set(state["seen_keys"]))
        content = json.dumps(state, separators=(",", ":"), sort_keys=True).encode()
        if len(content) > MAX_STATE_BYTES:
            raise ControllerError("ledger_archive_required")
        if self.revision is None:
            try:
                self.github.branch_sha(self.policy["state_branch"])
            except ControllerError as error:
                if error.status != 404:
                    raise
                self.github.request(
                    "POST",
                    "/git/refs",
                    {
                        "ref": "refs/heads/" + self.policy["state_branch"],
                        "sha": self.github.branch_sha(self.policy["base_branch"]),
                    },
                )
        payload = {
            "message": "Update Jules autopilot state",
            "content": base64.b64encode(content).decode(),
            "branch": self.policy["state_branch"],
        }
        if self.revision:
            payload["sha"] = self.revision
        response = self.github.request("PUT", self.path, payload)
        self.revision = response.get("content", {}).get("sha", "")
        if not SHA.fullmatch(self.revision):
            raise ControllerError("ledger_receipt_missing")


def account_capacity(sessions, now, policy):
    cutoff = now - timedelta(hours=24)
    recent = sum(
        timestamp(session.get("createTime")) is None
        or timestamp(session.get("createTime")) > cutoff
        for session in sessions
    )
    active = sum(session.get("state") not in TERMINAL for session in sessions)
    return {
        "rolling_24h_used": recent,
        "active": active,
        "available": max(
            0, min(policy["daily_limit"] - recent, policy["max_concurrent"] - active)
        ),
    }


def protected(path, policy):
    return any(
        fnmatch.fnmatchcase(path, pattern)
        or (pattern.endswith("/") and path.startswith(pattern))
        for pattern in policy["protected_paths"]
    )


def valid_path(path):
    return (
        isinstance(path, str)
        and bool(path)
        and not path.startswith("/")
        and "\\" not in path
        and all(part not in {"", ".", ".."} for part in path.split("/"))
    )


def command(root, args):
    try:
        response = subprocess.run(
            args, cwd=root, check=False, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ControllerError("local_inspection_failed") from None
    if response.returncode not in {0, 1}:
        raise ControllerError("local_inspection_failed")
    return response.stdout


def checkout_sha(root):
    value = command(root, ["git", "rev-parse", "HEAD"]).strip()
    if not SHA.fullmatch(value):
        raise ControllerError("invalid_checkout_sha")
    return value


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def source_candidates(root, policy):
    files = command(root, ["rg", "--files", "--glob", "*.py", *policy["scope_roots"]])
    tracked = set(
        command(root, ["git", "ls-files", "--", *policy["scope_roots"]]).splitlines()
    )
    candidates = []
    for filename in sorted(files.splitlines())[:300]:
        if (
            filename not in tracked
            or not valid_path(filename)
            or protected(filename, policy)
            or filename.endswith("/__init__.py")
        ):
            continue
        blob = command(root, ["git", "hash-object", "--", filename]).strip()
        if not SHA.fullmatch(blob):
            raise ControllerError("invalid_source_blob")
        headers = command(
            root,
            ["rg", "-n", "-m", "150", r"^\s*(async\s+)?def [A-Za-z_]\w*\(", filename],
        )
        functions = []
        for line in headers.splitlines():
            number, _, text = line.partition(":")
            match = re.search(r"\bdef ([A-Za-z_]\w*)\(", text)
            if number.isdigit() and match:
                functions.append((int(number), match.group(1)))
        for category in policy["categories"]:
            matches = command(
                root,
                [
                    "rg",
                    "-n",
                    "-m",
                    "24",
                    "--max-columns",
                    "350",
                    CATEGORY_PATTERNS[category],
                    filename,
                ],
            )
            anchors = set()
            for line in matches.splitlines():
                number, _, text = line.partition(":")
                if not number.isdigit():
                    continue
                preceding = [name for start, name in functions if start <= int(number)]
                anchor = preceding[-1] if preceding else "module initialization"
                semantic = digest(f"{category}|{filename}|{anchor}")
                if semantic in anchors:
                    continue
                anchors.add(semantic)
                key = digest(f"{semantic}|{blob}")
                stem = PurePosixPath(filename).stem
                candidates.append(
                    {
                        "key": key,
                        "semantic_key": semantic,
                        "category": category,
                        "title": f"{category.capitalize()}: {anchor} in {filename}",
                        "owned_files": [
                            filename,
                            f"test/services/test_jules_{stem}_{category}_{key[:8]}.py",
                        ],
                        "evidence": f"{filename}:{number}: {safe_text(text, 350)}",
                        "anchor": anchor,
                        "blob": blob,
                    }
                )
    return candidates


def seed_task(task):
    category = {
        "T01": "stability",
        "T02": "tests",
        "T03": "hardening",
        "T04": "performance",
        "T05": "hardening",
        "T06": "hardening",
        "T07": "performance",
        "T08": "stability",
        "T09": "enhancement",
        "T10": "hardening",
    }.get(task["id"], "stability")
    key = digest("seed|" + task["id"])
    return {
        "key": key,
        "semantic_key": key,
        "seed_id": task["id"],
        "category": category,
        "title": safe_text(task["title"], 140),
        "owned_files": task["owned_files"],
        "evidence": safe_text(task.get("evidence", ""), 1200),
        "deliverable": safe_text(task.get("deliverable", ""), 1200),
        "acceptance": [safe_text(item, 600) for item in task.get("acceptance", [])],
        "validation": safe_text(task.get("validation", ""), 800),
    }


def adopt_manifest(state, manifest):
    tasks = {task["id"]: task for task in manifest["tasks"]}
    known = {task.get("session_id") for task in state["tasks"]}
    for receipt in manifest.get("submission", {}).get("sessions", []):
        # Historical receipts use session_id rather than the API's id field.
        identifier = session_id({"id": receipt.get("session_id")})
        if not IDENTIFIER.fullmatch(identifier) or receipt["task_id"] not in tasks:
            raise ControllerError("invalid_seed_receipt")
        if (
            identifier not in known
            and digest("seed|" + receipt["task_id"]) not in state["seen_keys"]
        ):
            task = seed_task(tasks[receipt["task_id"]])
            state["tasks"].append(
                {**task, "session_id": identifier, "state": "UNKNOWN"}
            )
            state["seen_keys"].append(task["key"])
            known.add(identifier)


def choose_tasks(candidates, state, manifest, policy, now, limit):
    seen = set(state["seen_keys"])
    semantics = {task.get("semantic_key") for task in state["tasks"]}
    busy = {
        path
        for task in state["tasks"]
        if task.get("outcome") not in RETIRED
        for path in task["owned_files"]
    }
    seeds = deque(
        seed_task(task)
        for task in manifest["tasks"]
        if digest("seed|" + task["id"]) not in seen
    )
    recent = now - timedelta(days=policy["cooldown_days"])
    recent_semantics = {
        task.get("semantic_key")
        for task in state["tasks"]
        if (timestamp(task.get("started_at")) or now) >= recent
    }
    queues = {category: deque() for category in policy["categories"]}
    for task in sorted(candidates, key=lambda item: item["key"]):
        if (
            task["key"] not in seen
            and task["semantic_key"] not in seen
            and task["semantic_key"] not in semantics
            and task["semantic_key"] not in recent_semantics
        ):
            queues[task["category"]].append(task)
    counts = Counter(task.get("category") for task in state["tasks"][-100:])
    categories = sorted(
        policy["categories"], key=lambda category: (counts[category], category)
    )
    selected = []
    while len(selected) < limit and (seeds or any(queues.values())):
        choices = (
            [seeds.popleft()]
            if seeds
            else [
                queues[category].popleft()
                for category in categories
                if queues[category]
            ]
        )
        for task in choices:
            paths = task["owned_files"]
            if set(paths) & busy or any(
                not valid_path(path) or protected(path, policy) for path in paths
            ):
                continue
            selected.append(task)
            busy.update(paths)
            if len(selected) >= limit:
                break
    return selected


def task_prompt(task, state, policy):
    previous = [
        f"{safe_text(record['title'], 160)} -> {record.get('outcome', record.get('state', 'unknown'))}"
        for record in state["tasks"]
        if set(record["owned_files"]) & set(task["owned_files"])
    ][-6:]
    mcp = policy.get("mcp", {})
    services = "\n".join(
        f"{name}: {safe_text(mcp.get('usage', {}).get(name, ''), 1000)}"
        for name in mcp.get("connected", [])
    )
    return (
        f"Task marker: {task['marker']}. Work in {policy['repository']} from current "
        f"origin/{policy['base_branch']}. Preserve other work and existing interfaces. "
        f"Own ONLY: {', '.join(task['owned_files'])}. Include the marker in the PR body.\n\n"
        "Use fast rg --files and targeted bounded rg patterns and short excerpts; "
        "never read full files or dump logs. Never print secrets, personal data or "
        "configuration values. Do not call paid providers, publish media, access "
        "production Redis, change CI/automation files, or mutate production services. "
        "Use local deterministic mocks and synthetic fixtures. "
        f"{RESTORE_CHECK}\n\n"
        f"Evidence to verify on current main (a candidate, not an assumed defect):\n{task['evidence']}\n\n"
        f"Required outcome:\n{task.get('deliverable') or OBJECTIVES[task['category']]}\n"
        + "\n".join(f"- {item}" for item in task.get("acceptance", []))
        + "\n\nPrior work in this scope; do not repeat its result:\n"
        + ("\n".join(previous) or "No recorded prior task in this scope.")
        + "\n\nUse connected MCPs only when directly relevant:\n"
        + services
        + "\nConnected v0/Stitch provide UI context, not Vercel deployment authority. "
        "Linear context is read-only unless an explicit task authorizes communication. "
        "Do not invent a Supabase/Tinybird integration absent from this repository.\n\n"
        f"Run meaningful focused regression tests ({task.get('validation') or '.venv/bin/python -m pytest -q ' + task['owned_files'][-1]}), "
        "Ruff on changed Python, and the repository CI gate. Record commands and "
        "outcomes in the PR. Keep the change below "
        f"{policy['max_changed_files']} files and {policy['max_diff_lines']} added/deleted lines. "
        "Do not make cosmetic churn or a no-op PR. If evidence is sound, select a "
        "different useful behavior within the owned function/scope and explain it; "
        "if no worthwhile change exists, report that explicitly. Create a focused PR "
        f"targeting {policy['base_branch']}; never merge it yourself."
    )


def reconcile_intent(state, sessions, policy):
    intent = state.get("dispatch_intent")
    if not intent:
        return False
    matches = [
        session for session in sessions if intent["marker"] in session.get("title", "")
    ]
    if len(matches) > 1:
        raise ControllerError("multiple_dispatch_receipts")
    if not matches:
        return False
    session = matches[0]
    if (
        session.get("sourceContext", {}).get("source")
        != "sources/github/" + policy["repository"]
    ):
        raise ControllerError("dispatch_repository_mismatch")
    intent["session_id"], intent["state"] = (
        session_id(session),
        session.get("state", "UNKNOWN"),
    )
    state["tasks"].append(intent)
    state["seen_keys"].extend([intent["key"], intent["semantic_key"]])
    del state["dispatch_intent"]
    return True


def pr_number(session, repository):
    numbers = set()
    for output in session.get("outputs", []):
        url = output.get("pullRequest", {}).get("url", "")
        if not url:
            continue
        parsed = urllib.parse.urlsplit(url)
        match = re.fullmatch(
            "/" + re.escape(repository) + r"/pull/([1-9][0-9]*)/?", parsed.path
        )
        if (
            parsed.scheme != "https"
            or parsed.netloc != "github.com"
            or parsed.query
            or parsed.fragment
            or not match
        ):
            raise ControllerError("foreign_pull_request_output")
        numbers.add(int(match.group(1)))
    if len(numbers) > 1:
        raise ControllerError("multiple_pull_request_outputs")
    return next(iter(numbers), None)


def merge_gate(github, policy, task, number):
    pull = github.request("GET", f"/pulls/{number}")
    if pull.get("merged"):
        return "already_merged", pull
    if pull.get("state") != "open":
        return "closed", pull
    base, head = pull.get("base", {}), pull.get("head", {})
    repository = policy["repository"]
    if (
        base.get("ref") != policy["base_branch"]
        or base.get("repo", {}).get("full_name") != repository
        or head.get("repo", {}).get("full_name") != repository
        or head.get("ref") in {policy["base_branch"], policy["state_branch"]}
        or not SHA.fullmatch(head.get("sha", ""))
    ):
        return "repository_or_base_mismatch", pull
    try:
        protection = github.request(
            "GET", "/branches/" + escaped(policy["base_branch"]) + "/protection"
        )
    except ControllerError as error:
        if error.status == 404:
            return "unprotected_base", pull
        raise
    checks_policy = protection.get("required_status_checks") or {}
    contexts = set(checks_policy.get("contexts", [])) | {
        check.get("context") for check in checks_policy.get("checks", [])
    }
    if (
        checks_policy.get("strict") is not True
        or not set(policy["required_checks"]) <= contexts
        or protection.get("enforce_admins", {}).get("enabled") is not True
    ):
        return "protection_mismatch", pull
    files = github.pages(f"/pulls/{number}/files")
    if not files or len(files) > policy["max_changed_files"]:
        return "file_count", pull
    if (
        sum(file.get("additions", 0) + file.get("deletions", 0) for file in files)
        > policy["max_diff_lines"]
    ):
        return "diff_limit", pull
    owned = set(task["owned_files"])
    for file in files:
        for path in [
            file.get("filename", ""),
            file.get("previous_filename", file.get("filename", "")),
        ]:
            if not valid_path(path) or path not in owned or protected(path, policy):
                return "scope_violation", pull
    reviews = github.pages(f"/pulls/{number}/reviews")
    latest_reviews = {}
    for review in reviews:
        if review.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest_reviews[review.get("user", {}).get("id")] = review
    if any(
        review.get("state") == "CHANGES_REQUESTED" for review in latest_reviews.values()
    ):
        return "changes_requested", pull
    head_sha = head["sha"]
    runs = github.pages(f"/commits/{head_sha}/check-runs?filter=latest", "check_runs")
    required = set(policy["required_checks"])
    successful = set()
    for run in runs:
        if run.get("head_sha") != head_sha or run.get("status") != "completed":
            return "checks_pending", pull
        conclusion = run.get("conclusion")
        if conclusion not in {"success", "neutral", "skipped"}:
            return "checks_failed", pull
        if run.get("name") in required:
            if (
                conclusion != "success"
                or run.get("app", {}).get("slug") != "github-actions"
            ):
                return "required_check_not_successful", pull
            successful.add(run["name"])
    if successful != required:
        return "required_checks_missing", pull
    statuses = github.pages(f"/commits/{head_sha}/statuses")
    latest_statuses = {}
    for status in statuses:
        latest_statuses.setdefault(status.get("context"), status)
    if any(status.get("state") != "success" for status in latest_statuses.values()):
        return "status_not_successful", pull
    base_sha = github.branch_sha(policy["base_branch"])
    comparison = github.request("GET", f"/compare/{base_sha}...{head_sha}")
    if comparison.get("behind_by") != 0 or comparison.get("status") != "ahead":
        return "needs_refresh", pull
    if pull.get("mergeable") is not True:
        return "needs_refresh", pull
    pull["validated_base_sha"] = base_sha
    if pull.get("draft"):
        return "draft_ready", pull
    if pull.get("mergeable_state") != "clean":
        return "needs_refresh", pull
    return "ready", pull


def same_pull_identity(pull, validated, policy):
    return (
        pull.get("state") == "open"
        and pull.get("base", {}).get("ref") == policy["base_branch"]
        and pull.get("base", {}).get("repo", {}).get("full_name")
        == policy["repository"]
        and pull.get("head", {}).get("repo", {}).get("full_name")
        == policy["repository"]
        and pull.get("head", {}).get("sha") == validated["head"]["sha"]
    )


def promote_ready(github, policy, task, number, pull, persist, dry_run, now=None):
    """Publish a verified completed draft; merging waits for a fresh full gate."""
    now = now or datetime.now(UTC)
    if dry_run:
        return "would_publish_ready"
    if github.branch_sha(policy["base_branch"]) != pull["validated_base_sha"]:
        return "base_changed"
    latest = github.request("GET", f"/pulls/{number}")
    if not same_pull_identity(latest, pull, policy):
        return "pull_request_changed"
    if not latest.get("draft"):
        if task.pop("ready_attempt_sha", None):
            task.pop("ready_attempt_at", None)
            persist()
        return "already_ready"
    node = latest.get("node_id", "")
    if not isinstance(node, str) or not re.fullmatch(r"[A-Za-z0-9_=:-]{4,256}", node):
        return "ready_node_missing"
    head_sha = pull["head"]["sha"]
    history = task.setdefault("ready_attempts", [])
    if task.get("ready_attempt_sha") and not timestamp(task.get("ready_attempt_at")):
        # Older ledgers had no clock on an uncertain POST. Start a conservative
        # observation window; a later fresh draft read can reconcile it safely.
        task["ready_attempt_at"] = iso(now)
        history.append({"requested_at": iso(now)})
        persist()
        return "ready_promotion_pending"
    history[:] = [
        attempt
        for attempt in history
        if timestamp(attempt.get("requested_at")) is None
        or timestamp(attempt.get("requested_at")) > now - timedelta(hours=24)
    ]
    recent = [timestamp(attempt.get("requested_at")) for attempt in history]
    if any(attempt is None or now - attempt < timedelta(hours=1) for attempt in recent):
        return (
            "ready_promotion_pending"
            if task.get("ready_attempt_sha")
            else "ready_retry_backoff"
        )
    if len(history) >= 3:
        return "ready_retry_limit"
    task["ready_attempt_sha"] = head_sha
    task["ready_attempt_at"] = iso(now)
    history.append(
        {
            "requested_at": iso(now),
            "head_sha": head_sha,
            "base_sha": pull["validated_base_sha"],
        }
    )
    persist()
    try:
        response = github.client.request(
            "POST",
            "/graphql",
            {
                "query": "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){pullRequest{id isDraft headRefOid}}}",
                "variables": {"id": node},
            },
        )
    except ControllerError as error:
        if error.status and 400 <= error.status < 500 and error.status != 408:
            task.pop("ready_attempt_sha", None)
            task.pop("ready_attempt_at", None)
            persist()
            return "ready_rejected_" + str(error)
        return "ready_promotion_uncertain"
    data = response.get("data") or {}
    mutation = data.get("markPullRequestReadyForReview")
    errors = response.get("errors") or []
    if (
        mutation is None
        and errors
        and all(error.get("type") == "FORBIDDEN" for error in errors)
    ):
        task.pop("ready_attempt_sha", None)
        task.pop("ready_attempt_at", None)
        persist()
        return "ready_rejected_graphql"
    published = mutation or {}
    published = published.get("pullRequest", {}) or {}
    if (
        response.get("errors")
        or published.get("id") != node
        or published.get("isDraft") is not False
    ):
        return "ready_promotion_unconfirmed"
    if published.get("headRefOid") != head_sha:
        return "head_changed_during_readiness"
    latest = github.request("GET", f"/pulls/{number}")
    if not same_pull_identity(latest, pull, policy) or latest.get("draft"):
        return "pull_request_changed"
    task.pop("ready_attempt_sha", None)
    task.pop("ready_attempt_at", None)
    persist()
    return "draft_promoted"


def recover(
    jules, task, policy, capacity, persist, dry_run, reason, now=None, revision=None
):
    now = now or datetime.now(UTC)
    if task.get("recovery_intent"):
        return "recovery_pending"
    attempts = task.setdefault("recovery_attempts", {})
    pr_recovery = reason in {"needs_refresh", "checks_failed", "status_not_successful"}
    attempt_key = reason
    if pr_recovery:
        if (
            not revision
            or len(revision) != 2
            or not all(isinstance(sha, str) and SHA.fullmatch(sha) for sha in revision)
        ):
            return "recovery_revision_unverified"
        attempt_key = ":".join((reason, *revision))
        history = task.setdefault("pr_recovery_attempts", [])
        history[:] = [
            attempt
            for attempt in history
            if timestamp(attempt.get("requested_at")) is None
            or timestamp(attempt.get("requested_at")) > now - timedelta(hours=24)
        ]
        if len(history) >= 3:
            return "recovery_retry_limit"
    elif sum(count for key, count in attempts.items() if ":" not in key) >= 3:
        return "recovery_limit"
    if attempts.get(attempt_key, 0) >= 1:
        return "recovery_limit"
    needs_slot = task.get("state") in TERMINAL
    if needs_slot and capacity["active"] >= policy["max_concurrent"]:
        return "recovery_waiting_for_capacity"
    prompt = (
        f"Controller recovery ({reason}); original task scope remains ONLY "
        f"{', '.join(task['owned_files'])}. Continue using bounded rg, mocked providers "
        "and isolated local fixtures. Never request or expose credentials, production "
        "data or paid services, and never change CI/protected files. Use the repository "
        "snapshot restore checks; do not rerun setup or resnapshot. "
        f"{RESTORE_CHECK} Resolve routine implementation choices conservatively "
        "within the original acceptance criteria. If a PR is behind/conflicted or CI "
        "failed, refresh it against current main and repair only the owned files, then "
        "rerun all CI checks. If work is complete, publish its existing change as one "
        "PR targeting main. If blocked by production access or no worthwhile change, "
        "report the blocker; do not invent another task or repeat uncertain provider work."
    )
    if dry_run:
        return "would_request_recovery"
    # Persist before the POST, so a lost response cannot cause repeated messages.
    attempts[attempt_key] = attempts.get(attempt_key, 0) + 1
    if pr_recovery:
        history.append({"requested_at": iso(now), "revision": list(revision)})
    if needs_slot:
        task["recovery_intent"] = {
            "reason": reason,
            "previous_state": task["state"],
            "requested_at": iso(now),
            "baseline_update_time": task.get("session_update_time"),
        }
    persist()
    if needs_slot:
        capacity["active"] += 1
        capacity["available"] = max(0, capacity["available"] - 1)
    try:
        if reason == "plan_approval":
            jules.approve(task["session_id"])
        else:
            jules.message(task["session_id"], prompt)
    except ControllerError as error:
        if error.status and 400 <= error.status < 500 and error.status != 408:
            task.pop("recovery_intent", None)
            if needs_slot:
                capacity["active"] -= 1
                capacity["available"] = max(
                    0,
                    min(
                        policy["daily_limit"] - capacity["rolling_24h_used"],
                        policy["max_concurrent"] - capacity["active"],
                    ),
                )
            if error.status == 429:
                attempts[attempt_key] -= 1
                if pr_recovery:
                    history.pop()
            persist()
            return "recovery_rejected_" + str(error)
        return "recovery_uncertain"
    if needs_slot:
        task["recovery_intent"]["acknowledged_at"] = iso(now)
        persist()
    return "recovery_requested"


def reconcile_recovery(task, session, now):
    """Release a resume reservation only after authoritative progress evidence."""
    intent = task.get("recovery_intent")
    if not intent:
        return
    if task["state"] != intent["previous_state"] and task["state"] in {
        "QUEUED",
        "PLANNING",
        "AWAITING_PLAN_APPROVAL",
        "AWAITING_USER_FEEDBACK",
        "IN_PROGRESS",
        "PAUSED",
        *TERMINAL,
    }:
        task.pop("recovery_intent")
        return
    requested = timestamp(intent.get("requested_at"))
    if (
        task["state"] not in TERMINAL
        or requested is None
        or now - requested < timedelta(minutes=10)
    ):
        return
    baseline = timestamp(intent.get("baseline_update_time"))
    updated = timestamp(session.get("updateTime"))
    acknowledged = timestamp(intent.get("acknowledged_at"))
    progressed = (
        baseline is not None
        and updated is not None
        and updated > baseline
        and updated > requested
    )
    fresh_terminal = baseline is None or (updated is not None and updated >= baseline)
    if progressed or (acknowledged is not None and fresh_terminal):
        # The consumed attempt remains: reconciliation is not replay permission.
        task.pop("recovery_intent")


def dispatch(jules, ledger, state, policy, task, now):
    if state.get("dispatch_intent"):
        raise ControllerError("unreconciled_dispatch")
    intent = {
        **task,
        "marker": f"MPT-auto-{task['key'][:12]}-{secrets.token_hex(5)}",
        "started_at": iso(now),
    }
    intent["title"] = f"[{intent['marker']}] {safe_text(task['title'], 120)}"
    state["dispatch_intent"] = intent
    ledger.save(state)
    try:
        session = jules.create(policy, intent, task_prompt(intent, state, policy))
    except ControllerError as error:
        if error.status and 400 <= error.status < 500 and error.status != 408:
            del state["dispatch_intent"]
            ledger.save(state)
        raise
    identifier = session_id(session)
    intent["session_id"], intent["state"] = identifier, session.get("state", "QUEUED")
    state["tasks"].append(intent)
    state["seen_keys"].extend([intent["key"], intent["semantic_key"]])
    del state["dispatch_intent"]
    ledger.save(state)
    return identifier


def tick(root, policy, manifest, jules, github, ledger, now, dry_run=False):
    source = jules.client.request("GET", "/sources/github/" + policy["repository"])
    if source.get("name") != "sources/github/" + policy["repository"]:
        raise ControllerError("source_access_unverified")
    sessions = jules.sessions()
    indexed = {session_id(session): session for session in sessions}
    state = ledger.load()
    adopt_manifest(state, manifest)
    events = []
    if reconcile_intent(state, sessions, policy):
        events.append({"action": "reconciled_dispatch"})
    capacity = account_capacity(sessions, now, policy)

    def persist():
        if not dry_run:
            ledger.save(state)

    for task in state["tasks"]:
        if task.get("outcome") in RETIRED:
            continue
        identifier = task["session_id"]
        session = indexed.get(identifier)
        authoritative_read = session is None
        if session is None:
            session = jules.get(identifier)
            sessions.append(session)
            indexed[identifier] = session
            extra = account_capacity([session], now, policy)
            capacity["rolling_24h_used"] += extra["rolling_24h_used"]
            capacity["active"] += extra["active"]
            capacity["available"] = max(
                0,
                min(
                    policy["daily_limit"] - capacity["rolling_24h_used"],
                    policy["max_concurrent"] - capacity["active"],
                ),
            )
        if (
            session.get("sourceContext", {}).get("source")
            != "sources/github/" + policy["repository"]
        ):
            raise ControllerError("tracked_repository_mismatch")
        if (
            session.get("state") in TERMINAL or task.get("recovery_intent")
        ) and not authoritative_read:
            # A stale list snapshot must never authorize publishing or merging.
            authoritative = jules.get(identifier)
            if authoritative.get("sourceContext", {}).get("source") != source["name"]:
                raise ControllerError("tracked_repository_mismatch")
            capacity["active"] += int(authoritative.get("state") not in TERMINAL) - int(
                session.get("state") not in TERMINAL
            )
            capacity["available"] = max(
                0,
                min(
                    policy["daily_limit"] - capacity["rolling_24h_used"],
                    policy["max_concurrent"] - capacity["active"],
                ),
            )
            session = authoritative
        task["state"] = session.get("state", "UNKNOWN")
        task["session_update_time"] = session.get("updateTime")
        task.setdefault("started_at", session.get("createTime", iso(now)))
        reconcile_recovery(task, session, now)
        if task.get("recovery_intent"):
            # Listed/authoritative active sessions already occupy one slot.
            if task["state"] in TERMINAL:
                capacity["active"] += 1
                capacity["available"] = max(0, capacity["available"] - 1)
            action = "recovery_pending"
        elif task["state"] == "AWAITING_PLAN_APPROVAL":
            action = recover(
                jules, task, policy, capacity, persist, dry_run, "plan_approval", now
            )
        elif task["state"] in {"AWAITING_USER_FEEDBACK", "FAILED", "PAUSED"}:
            action = recover(
                jules,
                task,
                policy,
                capacity,
                persist,
                dry_run,
                task["state"].lower(),
                now,
            )
            if task["state"] == "FAILED" and action == "recovery_limit":
                task["outcome"] = "abandoned"
        elif task["state"] == "COMPLETED":
            number = pr_number(session, policy["repository"])
            if number is None:
                action = recover(
                    jules, task, policy, capacity, persist, dry_run, "missing_pr", now
                )
                if action == "recovery_limit":
                    task["outcome"] = "abandoned"
            else:
                task["pr_number"] = number
                gate, pull = merge_gate(github, policy, task, number)
                action = gate
                if gate in {"already_merged", "closed"}:
                    task["outcome"] = "merged" if gate == "already_merged" else "closed"
                elif gate == "draft_ready":
                    action = promote_ready(
                        github, policy, task, number, pull, persist, dry_run, now
                    )
                elif gate == "ready":
                    if dry_run:
                        action = "would_merge"
                    elif (
                        github.branch_sha(policy["base_branch"])
                        != pull["validated_base_sha"]
                    ):
                        action = "base_changed"
                    else:
                        latest = github.request("GET", f"/pulls/{number}")
                        if latest.get("head", {}).get("sha") != pull["head"]["sha"]:
                            action = "head_changed"
                        elif not same_pull_identity(latest, pull, policy) or latest.get(
                            "draft"
                        ):
                            action = "pull_request_changed"
                        else:
                            response = github.request(
                                "PUT",
                                f"/pulls/{number}/merge",
                                {"merge_method": "squash", "sha": pull["head"]["sha"]},
                            )
                            if response.get("merged") is True:
                                task["outcome"], action = "merged", "merged"
                            else:
                                action = "merge_rejected"
                elif gate in {
                    "needs_refresh",
                    "checks_failed",
                    "status_not_successful",
                }:
                    action = recover(
                        jules,
                        task,
                        policy,
                        capacity,
                        persist,
                        dry_run,
                        gate,
                        now,
                        (github.branch_sha(policy["base_branch"]), pull["head"]["sha"]),
                    )
        else:
            action = "running"
        task["last_result"] = action
        events.append({"session_id": identifier, "action": action})
    persist()
    if state.get("dispatch_intent"):
        events.append({"action": "ambiguous_dispatch_waiting"})
    elif capacity["available"]:
        base_sha = github.branch_sha(policy["base_branch"])
        if (
            checkout_sha(root) != base_sha
            or command(
                root, ["git", "status", "--porcelain", "--untracked-files=no"]
            ).strip()
        ):
            events.append({"action": "checkout_not_clean_current_main"})
        else:
            candidates = choose_tasks(
                source_candidates(root, policy),
                state,
                manifest,
                policy,
                now,
                capacity["available"],
            )
            if not candidates:
                events.append({"action": "no_fresh_grounded_work"})
            for task in candidates:
                if dry_run:
                    events.append(
                        {
                            "action": "would_dispatch",
                            "category": task["category"],
                            "title": task["title"],
                        }
                    )
                    continue
                try:
                    identifier = dispatch(jules, ledger, state, policy, task, now)
                except ControllerError as error:
                    events.append({"action": "dispatch_stopped", "reason": str(error)})
                    break
                events.append(
                    {
                        "action": "dispatched",
                        "session_id": identifier,
                        "category": task["category"],
                    }
                )
    return {"dry_run": dry_run, "account": capacity, "events": events[-60:]}


def load_policy(path):
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
        if (
            not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", policy["repository"])
            or policy["base_branch"] != "main"
            or not policy["state_branch"].startswith("codex/")
            or not valid_path(policy["state_path"])
            or not valid_path(policy["tasks_manifest"])
            or not 1 <= policy["daily_limit"] <= 15
            or not 1 <= policy["max_concurrent"] <= 3
            or not 1 <= policy["max_changed_files"] <= 12
            or not 1 <= policy["max_diff_lines"] <= 800
            or not 0 <= policy["cooldown_days"] <= 365
            or not policy["required_checks"]
            or not set(policy["categories"]) <= set(CATEGORY_PATTERNS)
            or not policy["categories"]
            or not all(valid_path(path) for path in policy["scope_roots"])
        ):
            raise ControllerError("invalid_policy")
        for field in (
            "protected_paths",
            "required_checks",
            "categories",
            "scope_roots",
        ):
            if not isinstance(policy[field], list) or not all(
                isinstance(item, str) for item in policy[field]
            ):
                raise ControllerError("invalid_policy")
        return policy
    except (OSError, ValueError, KeyError, TypeError):
        raise ControllerError("invalid_policy") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "tick"))
    parser.add_argument(
        "--policy", type=Path, default=ROOT / ".github/jules/policy.json"
    )
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.jules")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
        manifest = json.loads(
            (ROOT / policy["tasks_manifest"]).read_text(encoding="utf-8")
        )
        if manifest["repository"] != policy["repository"]:
            raise ControllerError("manifest_repository_mismatch")
        now = datetime.now(UTC)
        if args.command == "plan":
            state = new_state(policy["repository"])
            adopt_manifest(state, manifest)
            tasks = choose_tasks(
                source_candidates(ROOT, policy),
                state,
                manifest,
                policy,
                now,
                policy["daily_limit"],
            )
            result = {
                "offline": True,
                "source_sha": checkout_sha(ROOT),
                "tasks": [
                    {
                        "category": task["category"],
                        "title": task["title"],
                        "owned_files": task["owned_files"],
                        "evidence": task["evidence"][:200],
                    }
                    for task in tasks
                ],
            }
        else:
            key = load_key(args.env_file)
            token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
            if not token:
                raise ControllerError("github_token_missing")
            jules = Jules(
                JSONClient(
                    "https://jules.googleapis.com/v1alpha", {"X-Goog-Api-Key": key}
                )
            )
            github = GitHub(
                JSONClient(
                    "https://api.github.com",
                    {
                        "Authorization": "Bearer " + token,
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    },
                ),
                policy["repository"],
            )
            result = tick(
                ROOT,
                policy,
                manifest,
                jules,
                github,
                Ledger(github, policy),
                now,
                args.dry_run,
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (
        ControllerError,
        APIError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ) as error:
        code = (
            str(error)
            if isinstance(error, ControllerError)
            else "configuration_or_credential_error"
        )
        print(json.dumps({"error": code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
