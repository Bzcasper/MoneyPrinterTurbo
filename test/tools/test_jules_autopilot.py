"""Offline fault tests for quota, durable dispatch and guarded PR publication."""

import base64
import copy
import io
import json
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest

from tools import jules_autopilot as autopilot

NOW = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)
BASE = "a" * 40
HEAD = "b" * 40
REVISION = "c" * 40
REPOSITORY = "Bzcasper/MoneyPrinterTurbo"


@pytest.fixture
def policy():
    return {
        "repository": REPOSITORY,
        "base_branch": "main",
        "state_branch": "codex/jules-autopilot-state",
        "state_path": "state.json",
        "daily_limit": 15,
        "max_concurrent": 3,
        "cooldown_days": 30,
        "categories": list(autopilot.CATEGORY_PATTERNS),
        "required_checks": [
            "Python 3.11 tests",
            "Python 3.13 tests",
            "Windows smoke tests",
        ],
        "max_changed_files": 12,
        "max_diff_lines": 800,
        "tasks_manifest": "docs/JULES_TASKS.json",
        "scope_roots": ["app"],
        "protected_paths": [".github/**", "tools/**", ".env*", "**/AGENTS.md"],
        "mcp": {
            "connected": ["Supabase", "v0"],
            "usage": {"Supabase": "Read-only schema and synthetic fixtures."},
        },
    }


def candidate(number=1, category="stability"):
    return {
        "key": autopilot.digest(f"work-{number}"),
        "semantic_key": autopilot.digest(f"function-{number}"),
        "category": category,
        "title": f"Recover function {number}",
        "owned_files": [
            f"app/services/fixture_{number}.py",
            f"test/services/test_fixture_{number}.py",
        ],
        "evidence": f"app/services/fixture_{number}.py:10: except TimeoutError:",
    }


def session(identifier, state="IN_PROGRESS", age_hours=1, **extras):
    return {
        "id": str(identifier),
        "name": f"sessions/{identifier}",
        "state": state,
        "createTime": autopilot.iso(NOW - timedelta(hours=age_hours)),
        "sourceContext": {"source": "sources/github/" + REPOSITORY},
        **extras,
    }


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


class FakeLedger:
    def __init__(self, state=None, fail_save=None):
        self.state = copy.deepcopy(state or autopilot.new_state(REPOSITORY))
        self.saves = []
        self.fail_save = fail_save

    def load(self):
        return copy.deepcopy(self.state)

    def save(self, state):
        if len(self.saves) + 1 == self.fail_save:
            raise autopilot.ControllerError("http_409", 409)
        self.state = copy.deepcopy(state)
        self.saves.append(copy.deepcopy(state))


def test_quota_is_account_wide_rolling_and_unknown_states_reserve_capacity(policy):
    sessions = [session(i, "COMPLETED", age_hours=23.9) for i in range(14)]
    sessions += [
        session("outside", "COMPLETED", age_hours=24),
        session("old", "COMPLETED", age_hours=30),
    ]
    sessions += [
        session(
            "manual-other-repo",
            "AWAITING_USER_FEEDBACK",
            sourceContext={"source": "other"},
        )
    ]
    capacity = autopilot.account_capacity(sessions, NOW, policy)
    assert capacity == {"rolling_24h_used": 15, "active": 1, "available": 0}
    sessions = [
        session(1, "PAUSED"),
        session(2, "AWAITING_PLAN_APPROVAL"),
        session(3, "NEW_API_STATE"),
    ]
    assert autopilot.account_capacity(sessions, NOW, policy)["available"] == 0
    missing_time = session(4, "COMPLETED")
    missing_time.pop("createTime")
    assert (
        autopilot.account_capacity([missing_time], NOW, policy)["rolling_24h_used"] == 1
    )


def test_jules_pagination_deduplicates_and_detects_cycle():
    client = FakeClient(
        [
            {"sessions": [session(1)], "nextPageToken": "next page"},
            {"sessions": [session(1), session(2, "COMPLETED")]},
        ]
    )
    assert len(autopilot.Jules(client).sessions()) == 2
    assert client.calls[1][1].endswith("pageToken=next%20page")
    cyclic = FakeClient(
        [
            {"sessions": [], "nextPageToken": "repeat"},
            {"sessions": [], "nextPageToken": "repeat"},
        ]
    )
    with pytest.raises(autopilot.ControllerError, match="pagination_cycle"):
        autopilot.Jules(cyclic).sessions()


def test_github_check_pagination_reads_later_page():
    client = FakeClient(
        [{"check_runs": [{"id": i} for i in range(100)]}, {"check_runs": [{"id": 100}]}]
    )
    checks = autopilot.GitHub(client, REPOSITORY).pages(
        "/commits/head/check-runs?filter=latest", "check_runs"
    )
    assert len(checks) == 101
    assert "page=2" in client.calls[1][1]


def test_seed_receipts_are_adopted_once_and_survive_retired_history_pruning():
    task = {"id": "T01", **candidate(), "title": "Known first task"}
    manifest = {
        "tasks": [task],
        "submission": {"sessions": [{"task_id": "T01", "session_id": "known"}]},
    }
    state = autopilot.new_state(REPOSITORY)
    autopilot.adopt_manifest(state, manifest)
    autopilot.adopt_manifest(state, manifest)
    assert [record["session_id"] for record in state["tasks"]] == ["known"]
    state["tasks"].clear()
    autopilot.adopt_manifest(state, manifest)
    assert state["tasks"] == []


def test_variety_dedup_and_pending_pr_file_ownership(policy):
    candidates = [
        candidate(i, category)
        for i, category in enumerate(policy["categories"], start=1)
    ]
    state = autopilot.new_state(REPOSITORY)
    chosen = autopilot.choose_tasks(candidates, state, {"tasks": []}, policy, NOW, 5)
    assert {task["category"] for task in chosen} == set(policy["categories"])
    assert chosen == autopilot.choose_tasks(
        candidates, state, {"tasks": []}, policy, NOW, 5
    )
    blocked = {**candidates[0], "session_id": "completed", "state": "COMPLETED"}
    state["tasks"] = [blocked]
    another_same_file = {**candidate(9), "owned_files": blocked["owned_files"]}
    assert (
        autopilot.choose_tasks(
            [another_same_file], state, {"tasks": []}, policy, NOW, 1
        )
        == []
    )
    state["tasks"] = []
    state["seen_keys"] = [candidates[1]["semantic_key"]]
    changed_blob = {**candidates[1], "key": autopilot.digest("changed source")}
    assert (
        autopilot.choose_tasks([changed_blob], state, {"tasks": []}, policy, NOW, 1)
        == []
    )


def test_candidates_are_grounded_in_bounded_function_evidence(tmp_path, policy):
    folder = tmp_path / "app"
    folder.mkdir()
    for number in range(6):
        (folder / f"module_{number}.py").write_text(
            "def existing_operation(values):\n"
            "    for item in values:\n"
            "        try:\n"
            "            return requests.get(item, timeout=3)\n"
            "        except TimeoutError:\n"
            "            return None\n",
            encoding="utf-8",
        )
    autopilot.command(tmp_path, ["git", "init", "-q"])
    autopilot.command(tmp_path, ["git", "add", "app"])
    (folder / "untracked.py").write_text(
        "def personal_work(value):\n    return requests.get(value)\n", encoding="utf-8"
    )
    candidates = autopilot.source_candidates(tmp_path, policy)
    assert {task["category"] for task in candidates} == set(policy["categories"])
    assert all(task["anchor"] == "existing_operation" for task in candidates)
    assert all(
        "app/module_" in task["evidence"] and ":" in task["evidence"]
        for task in candidates
    )
    assert all(autopilot.SHA.fullmatch(task["blob"]) for task in candidates)
    assert all("untracked.py" not in task["owned_files"] for task in candidates)


def test_ledger_uses_expected_sha_and_safe_branch_creation(policy):
    state = autopilot.new_state(REPOSITORY)
    encoded = base64.b64encode(json.dumps(state).encode()).decode()
    client = FakeClient(
        [
            {"sha": REVISION, "content": encoded},
            {"content": {"sha": HEAD}},
        ]
    )
    ledger = autopilot.Ledger(autopilot.GitHub(client, REPOSITORY), policy)
    assert ledger.load() == state
    ledger.save(state)
    assert client.calls[-1][2]["sha"] == REVISION
    assert client.calls[-1][2]["branch"] == policy["state_branch"]
    fresh = FakeClient(
        [
            autopilot.ControllerError("http_404", 404),
            {"object": {"sha": BASE}},
            {"ref": "created"},
            {"content": {"sha": REVISION}},
        ]
    )
    autopilot.Ledger(autopilot.GitHub(fresh, REPOSITORY), policy).save(state)
    assert fresh.calls[2][2] == {
        "ref": "refs/heads/" + policy["state_branch"],
        "sha": BASE,
    }
    assert "force" not in fresh.calls[2][2]


@pytest.mark.parametrize(
    "error",
    [
        autopilot.ControllerError("transport_uncertain"),
        autopilot.ControllerError("http_500", 500),
        autopilot.ControllerError("http_408", 408),
    ],
)
def test_ambiguous_submission_keeps_durable_intent_and_never_replays(policy, error):
    client = FakeClient([error])
    jules, ledger = autopilot.Jules(client), FakeLedger()
    state = autopilot.new_state(REPOSITORY)
    with pytest.raises(autopilot.ControllerError):
        autopilot.dispatch(jules, ledger, state, policy, candidate(), NOW)
    assert ledger.state["dispatch_intent"]["key"] == candidate()["key"]
    with pytest.raises(autopilot.ControllerError, match="unreconciled_dispatch"):
        autopilot.dispatch(jules, ledger, state, policy, candidate(), NOW)
    assert len(client.calls) == 1
    accepted = session("accepted", title=state["dispatch_intent"]["title"])
    assert autopilot.reconcile_intent(state, [accepted], policy)
    assert state["tasks"][0]["session_id"] == "accepted"
    assert "dispatch_intent" not in state


@pytest.mark.parametrize("status", [400, 429])
def test_definitive_rejection_releases_intent_without_consuming_task(policy, status):
    ledger, state = FakeLedger(), autopilot.new_state(REPOSITORY)
    with pytest.raises(autopilot.ControllerError):
        autopilot.dispatch(
            autopilot.Jules(
                FakeClient([autopilot.ControllerError(f"http_{status}", status)])
            ),
            ledger,
            state,
            policy,
            candidate(),
            NOW,
        )
    assert "dispatch_intent" not in ledger.state
    assert ledger.state["seen_keys"] == []


def test_dispatch_persists_before_post_and_saves_auto_pr_receipt(policy):
    ledger, state = FakeLedger(), autopilot.new_state(REPOSITORY)
    client = FakeClient([session("new")])
    assert (
        autopilot.dispatch(
            autopilot.Jules(client), ledger, state, policy, candidate(), NOW
        )
        == "new"
    )
    assert "dispatch_intent" in ledger.saves[0]
    assert ledger.saves[0]["tasks"] == []
    payload = client.calls[0][2]
    assert payload["automationMode"] == "AUTO_CREATE_PR"
    assert payload["sourceContext"]["githubRepoContext"]["startingBranch"] == "main"
    assert payload["requirePlanApproval"] is False
    assert "Read-only schema" in payload["prompt"]
    assert "not Vercel deployment authority" in payload["prompt"]
    assert ledger.state["tasks"][0]["session_id"] == "new"
    blocked_ledger = FakeLedger(fail_save=1)
    unused_client = FakeClient([])
    with pytest.raises(autopilot.ControllerError):
        autopilot.dispatch(
            autopilot.Jules(unused_client),
            blocked_ledger,
            autopilot.new_state(REPOSITORY),
            policy,
            candidate(),
            NOW,
        )
    assert unused_client.calls == []


def merge_responses(policy):
    pull = {
        "state": "open",
        "draft": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "base": {"ref": "main", "repo": {"full_name": REPOSITORY}},
        "head": {"ref": "jules/task", "sha": HEAD, "repo": {"full_name": REPOSITORY}},
    }
    protection = {
        "required_status_checks": {
            "strict": True,
            "contexts": policy["required_checks"],
        },
        "enforce_admins": {"enabled": True},
    }
    runs = [
        {
            "name": name,
            "head_sha": HEAD,
            "status": "completed",
            "conclusion": "success",
            "app": {"slug": "github-actions"},
        }
        for name in policy["required_checks"]
    ]
    return [
        pull,
        protection,
        [{"filename": candidate()["owned_files"][0], "additions": 5, "deletions": 2}],
        [],
        {"check_runs": runs},
        [],
        {"object": {"sha": BASE}},
        {"behind_by": 0, "status": "ahead"},
    ]


@pytest.mark.parametrize(
    "change,reason",
    [
        ("missing_checks", "required_checks_missing"),
        ("skipped_required", "required_check_not_successful"),
        ("wrong_app", "required_check_not_successful"),
        ("pending_extra", "checks_pending"),
        ("failed_extra", "checks_failed"),
        ("wrong_sha", "checks_pending"),
        ("foreign_head", "repository_or_base_mismatch"),
        ("wrong_base", "repository_or_base_mismatch"),
        ("unprotected", "unprotected_base"),
        ("not_strict", "protection_mismatch"),
        ("admin_bypass", "protection_mismatch"),
        ("protected_file", "scope_violation"),
        ("oversized", "diff_limit"),
        ("requested_changes", "changes_requested"),
        ("pending_status", "status_not_successful"),
        ("behind", "needs_refresh"),
    ],
)
def test_merge_gate_rejects_unsafe_or_unverified_prs(policy, change, reason):
    responses = merge_responses(policy)
    if change == "missing_checks":
        responses[4]["check_runs"] = []
    elif change == "skipped_required":
        responses[4]["check_runs"][0]["conclusion"] = "skipped"
    elif change == "wrong_app":
        responses[4]["check_runs"][0]["app"]["slug"] = "other-app"
    elif change == "wrong_sha":
        responses[4]["check_runs"][0]["head_sha"] = BASE
    elif change in {"pending_extra", "failed_extra"}:
        responses[4]["check_runs"].append(
            {
                "name": "Security",
                "head_sha": HEAD,
                "status": "queued" if change == "pending_extra" else "completed",
                "conclusion": "failure",
            }
        )
    elif change == "foreign_head":
        responses[0]["head"]["repo"]["full_name"] = "stranger/repository"
    elif change == "wrong_base":
        responses[0]["base"]["ref"] = "other"
    elif change == "unprotected":
        responses[1] = autopilot.ControllerError("http_404", 404)
    elif change == "not_strict":
        responses[1]["required_status_checks"]["strict"] = False
    elif change == "admin_bypass":
        responses[1]["enforce_admins"]["enabled"] = False
    elif change == "protected_file":
        responses[2][0]["filename"] = ".github/workflows/ci.yml"
    elif change == "oversized":
        responses[2][0]["additions"] = 801
    elif change == "requested_changes":
        responses[3] = [{"user": {"id": 1}, "state": "CHANGES_REQUESTED"}]
    elif change == "pending_status":
        responses[5] = [{"context": "external-ci", "state": "pending"}]
    elif change == "behind":
        responses[7] = {"behind_by": 1, "status": "diverged"}
    client = FakeClient(responses)
    gate, _ = autopilot.merge_gate(
        autopilot.GitHub(client, REPOSITORY), policy, candidate(), 7
    )
    assert gate == reason
    assert all(method == "GET" for method, _, _ in client.calls)


def test_successful_merge_gate_binds_head_and_current_base(policy):
    client = FakeClient(merge_responses(policy))
    gate, pull = autopilot.merge_gate(
        autopilot.GitHub(client, REPOSITORY), policy, candidate(), 7
    )
    assert gate == "ready"
    assert pull["head"]["sha"] == HEAD
    assert pull["validated_base_sha"] == BASE
    assert HEAD in client.calls[4][1]


def test_only_exact_repository_pr_outputs_are_accepted():
    valid = {
        "outputs": [{"pullRequest": {"url": f"https://github.com/{REPOSITORY}/pull/7"}}]
    }
    assert autopilot.pr_number(valid, REPOSITORY) == 7
    for url in [
        f"https://github.com.evil.invalid/{REPOSITORY}/pull/7",
        "https://github.com/other/repo/pull/7",
        f"https://github.com/{REPOSITORY}/pull/7?token=private",
    ]:
        with pytest.raises(autopilot.ControllerError, match="foreign_pull_request"):
            autopilot.pr_number(
                {"outputs": [{"pullRequest": {"url": url}}]}, REPOSITORY
            )


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class FakeOpener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return FakeResponse(self.response)


def test_api_empty_body_and_errors_do_not_expose_secrets():
    opener = FakeOpener(b"")
    client = autopilot.JSONClient(
        "https://example.invalid", {"Authorization": "fixture-secret"}, opener
    )
    assert client.request("POST", "/sendMessage", {}) == {}
    assert opener.calls[0][1] == 30
    opener.response = urllib.error.HTTPError(
        "https://example.invalid",
        401,
        "fixture-secret",
        {},
        io.BytesIO(b'"fixture-secret"'),
    )
    with pytest.raises(autopilot.ControllerError) as caught:
        client.request("POST", "/sessions", {})
    assert str(caught.value) == "http_401"
    assert "fixture-secret" not in str(caught.value)
    assert (
        autopilot.NoRedirect().redirect_request(
            None, None, 307, "", {}, "https://other.invalid"
        )
        is None
    )


def test_recovery_is_bounded_and_durable_before_empty_body_post(policy):
    task = {**candidate(), "session_id": "known", "state": "AWAITING_USER_FEEDBACK"}
    client = FakeClient([{}])
    saved = []
    capacity = {"active": 3, "available": 0}

    def persist():
        saved.append(copy.deepcopy(task))

    assert (
        autopilot.recover(
            autopilot.Jules(client), task, policy, capacity, persist, False, "feedback"
        )
        == "recovery_requested"
    )
    assert saved[0]["recovery_attempts"] == {"feedback": 1}
    assert (
        autopilot.recover(
            autopilot.Jules(client), task, policy, capacity, persist, False, "feedback"
        )
        == "recovery_limit"
    )
    assert len(client.calls) == 1
    task["state"] = "FAILED"
    assert (
        autopilot.recover(
            autopilot.Jules(client), task, policy, capacity, persist, False, "failed"
        )
        == "recovery_waiting_for_capacity"
    )


@pytest.mark.parametrize("error_status", [429, 500])
def test_terminal_recovery_rejection_and_ambiguity_reserve_capacity(
    policy, error_status
):
    task = {**candidate(), "session_id": "known", "state": "FAILED"}
    client = FakeClient(
        [autopilot.ControllerError(f"http_{error_status}", error_status)]
    )
    capacity = {"rolling_24h_used": 1, "active": 2, "available": 1}
    saved = []

    def persist():
        saved.append(copy.deepcopy(task))

    result = autopilot.recover(
        autopilot.Jules(client), task, policy, capacity, persist, False, "failed"
    )
    assert saved[0]["recovery_intent"]["previous_state"] == "FAILED"
    if error_status == 429:
        assert result == "recovery_rejected_http_429"
        assert "recovery_intent" not in task
        assert task["recovery_attempts"]["failed"] == 0
        assert capacity["active"] == 2
        assert capacity["available"] == 1
    else:
        assert result == "recovery_uncertain"
        assert task["recovery_intent"]["reason"] == "failed"
        assert capacity["active"] == 3
        assert capacity["available"] == 0
        assert (
            autopilot.recover(
                autopilot.Jules(client),
                task,
                policy,
                capacity,
                persist,
                False,
                "failed",
            )
            == "recovery_pending"
        )
        assert len(client.calls) == 1


def test_unreconciled_terminal_recovery_keeps_ownership_and_global_slot(
    policy, tmp_path
):
    task = {
        **candidate(),
        "session_id": "known",
        "state": "FAILED",
        "recovery_attempts": {"failed": 1},
        "recovery_intent": {"reason": "failed", "previous_state": "FAILED"},
    }
    state = autopilot.new_state(REPOSITORY)
    state["tasks"] = [task]
    ledger = FakeLedger(state)
    client = FakeClient(
        [
            {"name": "sources/github/" + REPOSITORY},
            {"sessions": [session("known", "FAILED"), session(2), session(3)]},
            session("known", "FAILED"),
        ]
    )
    result = autopilot.tick(
        tmp_path,
        policy,
        {"tasks": []},
        autopilot.Jules(client),
        autopilot.GitHub(FakeClient([]), REPOSITORY),
        ledger,
        NOW,
    )
    assert result["account"]["active"] == 3
    assert result["account"]["available"] == 0
    assert result["events"][0]["action"] == "recovery_pending"
    assert "outcome" not in ledger.state["tasks"][0]
    assert all(method == "GET" for method, _, _ in client.calls)


def test_policy_manifest_must_be_a_repository_relative_path(policy, tmp_path):
    policy["tasks_manifest"] = "../.env.jules"
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(autopilot.ControllerError, match="invalid_policy"):
        autopilot.load_policy(path)


def test_live_tick_merges_exact_head_and_dry_run_never_mutates(
    policy, monkeypatch, tmp_path
):
    task = {**candidate(), "session_id": "known", "state": "COMPLETED"}
    state = autopilot.new_state(REPOSITORY)
    state["tasks"] = [task]
    completed = session(
        "known",
        "COMPLETED",
        outputs=[{"pullRequest": {"url": f"https://github.com/{REPOSITORY}/pull/7"}}],
    )
    monkeypatch.setattr(autopilot, "checkout_sha", lambda root: BASE)
    monkeypatch.setattr(autopilot, "command", lambda root, args: "")
    monkeypatch.setattr(autopilot, "source_candidates", lambda root, policy: [])
    for dry_run in [True, False]:
        jules_client = FakeClient(
            [
                {"name": "sources/github/" + REPOSITORY},
                {"sessions": [completed]},
                completed,
            ]
        )
        responses = merge_responses(policy)
        if not dry_run:
            responses.extend(
                [{"object": {"sha": BASE}}, responses[0], {"merged": True}]
            )
        responses.append({"object": {"sha": BASE}})
        github_client = FakeClient(responses)
        ledger = FakeLedger(state)
        result = autopilot.tick(
            tmp_path,
            policy,
            {"tasks": []},
            autopilot.Jules(jules_client),
            autopilot.GitHub(github_client, REPOSITORY),
            ledger,
            NOW,
            dry_run,
        )
        merges = [call for call in github_client.calls if call[0] == "PUT"]
        if dry_run:
            assert merges == [] and ledger.saves == []
            assert result["events"][0]["action"] == "would_merge"
        else:
            assert merges[0][2] == {"merge_method": "squash", "sha": HEAD}
            assert ledger.state["tasks"][0]["outcome"] == "merged"


@pytest.mark.parametrize(
    "changed,reason", [("base", "base_changed"), ("head", "head_changed")]
)
def test_tick_refuses_merge_when_validated_revision_changes(
    policy, monkeypatch, tmp_path, changed, reason
):
    task = {**candidate(), "session_id": "known", "state": "COMPLETED"}
    state = autopilot.new_state(REPOSITORY)
    state["tasks"] = [task]
    completed = session(
        "known",
        "COMPLETED",
        outputs=[{"pullRequest": {"url": f"https://github.com/{REPOSITORY}/pull/7"}}],
    )
    jules_client = FakeClient(
        [{"name": "sources/github/" + REPOSITORY}, {"sessions": [completed]}, completed]
    )
    responses = merge_responses(policy)
    responses.append({"object": {"sha": HEAD if changed == "base" else BASE}})
    if changed == "head":
        changed_pull = copy.deepcopy(responses[0])
        changed_pull["head"]["sha"] = BASE
        responses.append(changed_pull)
    responses.append({"object": {"sha": BASE}})
    monkeypatch.setattr(autopilot, "checkout_sha", lambda root: BASE)
    monkeypatch.setattr(autopilot, "command", lambda root, args: "")
    monkeypatch.setattr(autopilot, "source_candidates", lambda root, policy: [])
    github_client = FakeClient(responses)
    result = autopilot.tick(
        tmp_path,
        policy,
        {"tasks": []},
        autopilot.Jules(jules_client),
        autopilot.GitHub(github_client, REPOSITORY),
        FakeLedger(state),
        NOW,
    )
    assert result["events"][0]["action"] == reason
    assert all(method == "GET" for method, _, _ in github_client.calls)


def test_completed_draft_must_pass_every_gate_before_readiness(policy):
    responses = merge_responses(policy)
    responses[0]["draft"] = True
    responses[0]["mergeable_state"] = "blocked"
    gate, _ = autopilot.merge_gate(
        autopilot.GitHub(FakeClient(responses), REPOSITORY), policy, candidate(), 7
    )
    assert gate == "draft_ready"
    responses[4]["check_runs"][0]["status"] = "in_progress"
    gate, _ = autopilot.merge_gate(
        autopilot.GitHub(FakeClient(responses), REPOSITORY), policy, candidate(), 7
    )
    assert gate == "checks_pending"


def ready_pull(policy):
    pull = merge_responses(policy)[0]
    return {**pull, "draft": True, "node_id": "PR_fixture", "validated_base_sha": BASE}


def test_draft_promotion_is_journaled_and_does_not_merge(policy):
    pull = ready_pull(policy)
    published = {**pull, "draft": False}
    client = FakeClient(
        [
            {"object": {"sha": BASE}},
            pull,
            {
                "data": {
                    "markPullRequestReadyForReview": {
                        "pullRequest": {
                            "id": "PR_fixture",
                            "isDraft": False,
                            "headRefOid": HEAD,
                        }
                    }
                }
            },
            published,
        ]
    )
    task, snapshots = candidate(), []

    def persist():
        snapshots.append(copy.deepcopy(task))

    assert (
        autopilot.promote_ready(
            autopilot.GitHub(client, REPOSITORY), policy, task, 7, pull, persist, False
        )
        == "draft_promoted"
    )
    assert snapshots[0]["ready_attempt_sha"] == HEAD
    assert client.calls[2][0:2] == ("POST", "/graphql")
    assert client.calls[2][2]["variables"] == {"id": "PR_fixture"}
    assert not any("/merge" in path for _, path, _ in client.calls)
    unused = FakeClient([])
    assert (
        autopilot.promote_ready(
            autopilot.GitHub(unused, REPOSITORY), policy, {}, 7, pull, persist, True
        )
        == "would_publish_ready"
    )
    assert unused.calls == []


@pytest.mark.parametrize("change", ["before", "after", "ambiguous"])
def test_readiness_revision_races_or_lost_receipt_cannot_trigger_merge(policy, change):
    pull = ready_pull(policy)
    changed = {**pull, "head": {**pull["head"], "sha": BASE}}
    responses = [{"object": {"sha": BASE}}, changed if change == "before" else pull]
    if change == "ambiguous":
        responses.append(autopilot.ControllerError("transport_uncertain"))
    elif change == "after":
        responses.extend(
            [
                {
                    "data": {
                        "markPullRequestReadyForReview": {
                            "pullRequest": {
                                "id": "PR_fixture",
                                "isDraft": False,
                                "headRefOid": HEAD,
                            }
                        }
                    }
                },
                changed,
            ]
        )
    client, task = FakeClient(responses), candidate()
    result = autopilot.promote_ready(
        autopilot.GitHub(client, REPOSITORY), policy, task, 7, pull, lambda: None, False
    )
    assert result == (
        "ready_promotion_uncertain" if change == "ambiguous" else "pull_request_changed"
    )
    assert not any("/merge" in path for _, path, _ in client.calls)
    if change == "ambiguous":
        again = FakeClient([{"object": {"sha": BASE}}, pull])
        assert (
            autopilot.promote_ready(
                autopilot.GitHub(again, REPOSITORY),
                policy,
                task,
                7,
                pull,
                lambda: None,
                False,
            )
            == "ready_promotion_pending"
        )
        assert all(method == "GET" for method, _, _ in again.calls)


@pytest.mark.parametrize("listed_state", ["IN_PROGRESS", "COMPLETED"])
def test_unfinished_session_with_pr_output_is_never_promoted(
    policy, monkeypatch, tmp_path, listed_state
):
    running = session(
        "known",
        "IN_PROGRESS",
        outputs=[{"pullRequest": {"url": f"https://github.com/{REPOSITORY}/pull/7"}}],
    )
    state = autopilot.new_state(REPOSITORY)
    state["tasks"] = [{**candidate(), "session_id": "known", "state": "IN_PROGRESS"}]
    listed = {**running, "state": listed_state}
    responses = [{"name": "sources/github/" + REPOSITORY}, {"sessions": [listed]}]
    if listed_state == "COMPLETED":
        responses.append(running)
    jules_client = FakeClient(responses)
    github_client = FakeClient([{"object": {"sha": BASE}}])
    monkeypatch.setattr(autopilot, "checkout_sha", lambda root: BASE)
    monkeypatch.setattr(autopilot, "command", lambda root, args: "")
    monkeypatch.setattr(autopilot, "source_candidates", lambda root, policy: [])
    result = autopilot.tick(
        tmp_path,
        policy,
        {"tasks": []},
        autopilot.Jules(jules_client),
        autopilot.GitHub(github_client, REPOSITORY),
        FakeLedger(state),
        NOW,
    )
    assert result["events"][0]["action"] == "running"
    assert all(
        "/pulls/" not in path and path != "/graphql"
        for _, path, _ in github_client.calls
    )


def test_refresh_recovery_tracks_revisions_without_a_lifetime_cap(policy):
    task = {**candidate(), "session_id": "known", "state": "COMPLETED"}
    client = FakeClient([{}, {}, {}, {}])
    jules = autopilot.Jules(client)
    capacity = {"rolling_24h_used": 1, "active": 0, "available": 3}
    for index in range(4):
        now = NOW + timedelta(days=index)
        revision = (str(index + 1) * 40, HEAD)
        assert (
            autopilot.recover(
                jules,
                task,
                policy,
                capacity,
                lambda: None,
                False,
                "needs_refresh",
                now,
                revision,
            )
            == "recovery_requested"
        )
        task["state"] = "IN_PROGRESS"
        autopilot.reconcile_recovery(task, session("known"), now)
        task["state"] = "COMPLETED"
        capacity.update(active=0, available=3)
        assert (
            autopilot.recover(
                jules,
                task,
                policy,
                capacity,
                lambda: None,
                False,
                "needs_refresh",
                now,
                revision,
            )
            == "recovery_limit"
        )
    assert len(client.calls) == 4
    assert len(task["recovery_attempts"]) == 4


def test_refresh_recovery_budget_expires_but_uncertain_revision_is_not_replayed(policy):
    task = {**candidate(), "session_id": "known", "state": "COMPLETED"}
    client = FakeClient([autopilot.ControllerError("transport_uncertain"), {}, {}, {}])
    jules = autopilot.Jules(client)
    capacity = {"rolling_24h_used": 1, "active": 0, "available": 3}
    for index in range(3):
        now = NOW + timedelta(hours=index)
        result = autopilot.recover(
            jules,
            task,
            policy,
            capacity,
            lambda: None,
            False,
            "checks_failed",
            now,
            (str(index + 1) * 40, HEAD),
        )
        assert result == ("recovery_uncertain" if index == 0 else "recovery_requested")
        task["state"] = "IN_PROGRESS"
        autopilot.reconcile_recovery(task, session("known"), now)
        task["state"] = "COMPLETED"
        capacity.update(active=0, available=3)
    assert (
        autopilot.recover(
            jules,
            task,
            policy,
            capacity,
            lambda: None,
            False,
            "checks_failed",
            NOW + timedelta(hours=3),
            (REVISION, HEAD),
        )
        == "recovery_retry_limit"
    )
    assert (
        autopilot.recover(
            jules,
            task,
            policy,
            capacity,
            lambda: None,
            False,
            "checks_failed",
            NOW + timedelta(hours=25),
            ("1" * 40, HEAD),
        )
        == "recovery_limit"
    )
    assert (
        autopilot.recover(
            jules,
            task,
            policy,
            capacity,
            lambda: None,
            False,
            "checks_failed",
            NOW + timedelta(hours=25),
            (REVISION, HEAD),
        )
        == "recovery_requested"
    )
    assert len(client.calls) == 4


def test_tick_refresh_records_real_base_even_when_gate_returns_early(
    policy, monkeypatch, tmp_path
):
    completed = session(
        "known",
        "COMPLETED",
        updateTime=autopilot.iso(NOW),
        outputs=[{"pullRequest": {"url": f"https://github.com/{REPOSITORY}/pull/7"}}],
    )
    state = autopilot.new_state(REPOSITORY)
    state["tasks"] = [{**candidate(), "session_id": "known", "state": "COMPLETED"}]
    responses = merge_responses(policy)
    responses[6] = {"object": {"sha": REVISION}}
    responses[7] = {"behind_by": 1, "status": "diverged"}
    responses += [{"object": {"sha": REVISION}}, {"object": {"sha": REVISION}}]
    github_client = FakeClient(responses)
    jules_client = FakeClient(
        [
            {"name": "sources/github/" + REPOSITORY},
            {"sessions": [completed]},
            completed,
            {},
        ]
    )
    monkeypatch.setattr(autopilot, "checkout_sha", lambda root: BASE)
    ledger = FakeLedger(state)
    result = autopilot.tick(
        tmp_path,
        policy,
        {"tasks": []},
        autopilot.Jules(jules_client),
        autopilot.GitHub(github_client, REPOSITORY),
        ledger,
        NOW,
    )
    task = ledger.state["tasks"][0]
    assert result["events"][0]["action"] == "recovery_requested"
    assert task["recovery_attempts"] == {f"needs_refresh:{REVISION}:{HEAD}": 1}
    assert task["pr_recovery_attempts"][0]["revision"] == [REVISION, HEAD]
    assert task["recovery_intent"]["acknowledged_at"] == autopilot.iso(NOW)
    assert not github_client.responses


def terminal_intent(acknowledged=False, state="FAILED", requested=NOW):
    return {
        "reason": "failed" if state == "FAILED" else "missing_pr",
        "previous_state": state,
        "requested_at": autopilot.iso(requested),
        "baseline_update_time": autopilot.iso(requested - timedelta(minutes=2)),
        **({"acknowledged_at": autopilot.iso(requested)} if acknowledged else {}),
    }


@pytest.mark.parametrize("terminal", ["FAILED", "COMPLETED"])
def test_acknowledged_terminal_recoveries_release_all_slots_and_retire(
    policy, monkeypatch, tmp_path, terminal
):
    state = autopilot.new_state(REPOSITORY)
    known = [session(i, terminal, updateTime=autopilot.iso(NOW)) for i in range(3)]
    reason = "failed" if terminal == "FAILED" else "missing_pr"
    state["tasks"] = [
        {
            **candidate(i),
            "session_id": str(i),
            "state": terminal,
            "recovery_attempts": {reason: 1},
            "recovery_intent": terminal_intent(True, terminal),
        }
        for i in range(3)
    ]
    client = FakeClient(
        [
            {"name": "sources/github/" + REPOSITORY},
            {"sessions": known},
            *known,
        ]
    )
    github_client = FakeClient([{"object": {"sha": BASE}}])
    monkeypatch.setattr(autopilot, "checkout_sha", lambda root: BASE)
    monkeypatch.setattr(autopilot, "command", lambda root, args: "")
    monkeypatch.setattr(autopilot, "source_candidates", lambda root, policy: [])
    ledger = FakeLedger(state)
    result = autopilot.tick(
        tmp_path,
        policy,
        {"tasks": []},
        autopilot.Jules(client),
        autopilot.GitHub(github_client, REPOSITORY),
        ledger,
        NOW + timedelta(hours=1),
    )
    assert result["account"]["active"] == 0
    assert result["account"]["available"] == 3
    assert all(task["outcome"] == "abandoned" for task in ledger.state["tasks"])
    assert all("recovery_intent" not in task for task in ledger.state["tasks"])
    assert all(method == "GET" for method, _, _ in client.calls)


@pytest.mark.parametrize(
    "update_time",
    [
        None,
        "invalid",
        autopilot.iso(NOW - timedelta(minutes=1)),
        autopilot.iso(NOW),
    ],
)
def test_ambiguous_terminal_recovery_requires_post_request_progress(update_time):
    task = {
        "state": "FAILED",
        "recovery_intent": terminal_intent(),
        "recovery_attempts": {"failed": 1},
    }
    autopilot.reconcile_recovery(
        task,
        session("known", "FAILED", updateTime=update_time),
        NOW + timedelta(hours=1),
    )
    assert "recovery_intent" in task


def test_recovery_progress_after_grace_reconciles_without_replay_permission(policy):
    task = {
        **candidate(),
        "session_id": "known",
        "state": "FAILED",
        "recovery_intent": terminal_intent(),
        "recovery_attempts": {"failed": 1},
    }
    fresh = session(
        "known", "FAILED", updateTime=autopilot.iso(NOW + timedelta(minutes=1))
    )
    autopilot.reconcile_recovery(task, fresh, NOW + timedelta(minutes=9))
    assert "recovery_intent" in task
    autopilot.reconcile_recovery(task, fresh, NOW + timedelta(minutes=10))
    assert "recovery_intent" not in task
    client = FakeClient([])
    assert (
        autopilot.recover(
            autopilot.Jules(client),
            task,
            policy,
            {"rolling_24h_used": 1, "active": 0, "available": 3},
            lambda: None,
            False,
            "failed",
            NOW + timedelta(hours=1),
        )
        == "recovery_limit"
    )
    assert not client.calls


@pytest.mark.parametrize("unknown", [None, "UNKNOWN", "FUTURE_STATE"])
def test_unknown_state_does_not_reconcile_ambiguous_recovery(unknown):
    task = {"state": unknown, "recovery_intent": terminal_intent()}
    autopilot.reconcile_recovery(
        task, session("known", unknown), NOW + timedelta(hours=1)
    )
    assert "recovery_intent" in task


@pytest.mark.parametrize("acknowledged", [False, True])
def test_pending_terminal_recovery_blocks_existing_pr_publication(
    policy, tmp_path, acknowledged
):
    completed = session(
        "known",
        "COMPLETED",
        updateTime=autopilot.iso(NOW),
        outputs=[{"pullRequest": {"url": f"https://github.com/{REPOSITORY}/pull/7"}}],
    )
    state = autopilot.new_state(REPOSITORY)
    state["tasks"] = [
        {
            **candidate(),
            "session_id": "known",
            "state": "COMPLETED",
            "recovery_intent": terminal_intent(acknowledged, "COMPLETED"),
        }
    ]
    client = FakeClient(
        [
            {"name": "sources/github/" + REPOSITORY},
            {"sessions": [completed, session(2), session(3)]},
            completed,
        ]
    )
    github_client = FakeClient([])
    result = autopilot.tick(
        tmp_path,
        policy,
        {"tasks": []},
        autopilot.Jules(client),
        autopilot.GitHub(github_client, REPOSITORY),
        FakeLedger(state),
        NOW + timedelta(minutes=5),
    )
    assert result["account"]["active"] == 3
    assert result["events"][0]["action"] == "recovery_pending"
    assert not github_client.calls


@pytest.mark.parametrize("listed_state", ["FAILED", "IN_PROGRESS"])
def test_authoritative_active_recovery_is_not_double_counted(
    policy, tmp_path, listed_state
):
    state = autopilot.new_state(REPOSITORY)
    state["tasks"] = [
        {
            **candidate(),
            "session_id": "known",
            "state": "FAILED",
            "recovery_intent": terminal_intent(),
        }
    ]
    client = FakeClient(
        [
            {"name": "sources/github/" + REPOSITORY},
            {"sessions": [session("known", listed_state), session(2), session(3)]},
            session("known", "IN_PROGRESS"),
        ]
    )
    result = autopilot.tick(
        tmp_path,
        policy,
        {"tasks": []},
        autopilot.Jules(client),
        autopilot.GitHub(FakeClient([]), REPOSITORY),
        FakeLedger(state),
        NOW + timedelta(hours=1),
    )
    assert result["account"]["active"] == 3
    assert result["events"][0]["action"] == "running"


def readiness_receipt():
    return {
        "data": {
            "markPullRequestReadyForReview": {
                "pullRequest": {
                    "id": "PR_fixture",
                    "isDraft": False,
                    "headRefOid": HEAD,
                }
            }
        }
    }


def test_graphql_forbidden_readiness_retries_after_backoff_and_fresh_read(policy):
    pull, task = ready_pull(policy), candidate()
    rejected = FakeClient(
        [
            {"object": {"sha": BASE}},
            pull,
            {
                "data": {"markPullRequestReadyForReview": None},
                "errors": [{"type": "FORBIDDEN"}],
            },
        ]
    )
    saved = []

    def persist():
        saved.append(copy.deepcopy(task))

    assert (
        autopilot.promote_ready(
            autopilot.GitHub(rejected, REPOSITORY),
            policy,
            task,
            7,
            pull,
            persist,
            False,
            NOW,
        )
        == "ready_rejected_graphql"
    )
    assert saved[0]["ready_attempt_sha"] == HEAD
    assert "ready_attempt_sha" not in task
    assert len(task["ready_attempts"]) == 1
    immediate = FakeClient([{"object": {"sha": BASE}}, pull])
    assert (
        autopilot.promote_ready(
            autopilot.GitHub(immediate, REPOSITORY),
            policy,
            task,
            7,
            pull,
            persist,
            False,
            NOW + timedelta(minutes=59),
        )
        == "ready_retry_backoff"
    )
    assert all(method == "GET" for method, _, _ in immediate.calls)
    retry = FakeClient(
        [
            {"object": {"sha": BASE}},
            pull,
            readiness_receipt(),
            {**pull, "draft": False},
        ]
    )
    assert (
        autopilot.promote_ready(
            autopilot.GitHub(retry, REPOSITORY),
            policy,
            task,
            7,
            pull,
            persist,
            False,
            NOW + timedelta(hours=1),
        )
        == "draft_promoted"
    )
    assert len(task["ready_attempts"]) == 2
    assert "ready_attempt_sha" not in task
    assert not any("/merge" in path for _, path, _ in retry.calls)


def test_uncertain_readiness_retries_are_bounded_and_rolling(policy):
    pull, task = ready_pull(policy), candidate()
    for index in range(3):
        client = FakeClient(
            [
                {"object": {"sha": BASE}},
                pull,
                autopilot.ControllerError("transport_uncertain"),
            ]
        )
        assert (
            autopilot.promote_ready(
                autopilot.GitHub(client, REPOSITORY),
                policy,
                task,
                7,
                pull,
                lambda: None,
                False,
                NOW + timedelta(hours=index),
            )
            == "ready_promotion_uncertain"
        )
        assert [method for method, _, _ in client.calls] == ["GET", "GET", "POST"]
    blocked = FakeClient([{"object": {"sha": BASE}}, pull])
    assert (
        autopilot.promote_ready(
            autopilot.GitHub(blocked, REPOSITORY),
            policy,
            task,
            7,
            pull,
            lambda: None,
            False,
            NOW + timedelta(hours=3),
        )
        == "ready_retry_limit"
    )
    assert all(method == "GET" for method, _, _ in blocked.calls)
    retry = FakeClient(
        [
            {"object": {"sha": BASE}},
            pull,
            readiness_receipt(),
            {**pull, "draft": False},
        ]
    )
    assert (
        autopilot.promote_ready(
            autopilot.GitHub(retry, REPOSITORY),
            policy,
            task,
            7,
            pull,
            lambda: None,
            False,
            NOW + timedelta(hours=25),
        )
        == "draft_promoted"
    )


@pytest.mark.parametrize("changed", ["base", "head", "already_ready"])
def test_uncertain_readiness_rechecks_revision_and_already_ready_before_retry(
    policy, changed
):
    pull = ready_pull(policy)
    task = {
        **candidate(),
        "ready_attempt_sha": HEAD,
        "ready_attempt_at": autopilot.iso(NOW),
        "ready_attempts": [{"requested_at": autopilot.iso(NOW)}],
    }
    responses = [{"object": {"sha": REVISION if changed == "base" else BASE}}]
    if changed != "base":
        responses.append(
            {**pull, "draft": False}
            if changed == "already_ready"
            else {**pull, "head": {**pull["head"], "sha": REVISION}}
        )
    client = FakeClient(responses)
    result = autopilot.promote_ready(
        autopilot.GitHub(client, REPOSITORY),
        policy,
        task,
        7,
        pull,
        lambda: None,
        False,
        NOW + timedelta(hours=1),
    )
    assert (
        result
        == {
            "base": "base_changed",
            "head": "pull_request_changed",
            "already_ready": "already_ready",
        }[changed]
    )
    assert all(method == "GET" for method, _, _ in client.calls)
    if changed == "already_ready":
        assert "ready_attempt_sha" not in task
