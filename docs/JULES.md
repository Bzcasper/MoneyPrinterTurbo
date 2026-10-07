# Jules environment and continuous automation

The original ten scoped tasks and confirmed session receipts are in
[JULES_TASKS.json](JULES_TASKS.json). Their published starting branch is recorded
in the manifest. The continuous controller starts new work from `main`, rotates
hardening, stability, enhancement, performance and test work, and uses task
history to avoid repeating the same change.

## Repository VM setup

The owner has already saved the setup script and completed **Run and snapshot**
in the [repository configuration](https://jules.google.com/repo/github/Bzcasper/MoneyPrinterTurbo/config).
Preserve that saved script and snapshot; no configuration edits or new snapshot
are needed. [tools/jules_setup.sh](../tools/jules_setup.sh) is a local setup
reference, not a command to overwrite the saved environment. Jules clones into
`/app` automatically.

The script installs Python 3.11, uv, locked development dependencies, the optional
TwelveLabs SDK, FFmpeg and an isolated Redis fixture. It runs compilation, Ruff and
focused schema/state tests. Provider credentials and the Jules API key are not
needed in the VM.

New task shells need these fixture variables:

| Key | Value | Purpose |
| --- | --- | --- |
| `MPT_RUN_INTEGRATION_TESTS` | `0` | Keep live provider calls disabled |
| `MPT_TEST_REDIS_HOST` | `127.0.0.1` | VM-local Redis fixture |
| `MPT_TEST_REDIS_PORT` | `6389` | Isolate the fixture port |
| `MPT_TEST_REDIS_DB` | `15` | Isolate test state |

Verify restoration inside the next real Jules session. If fixture variables are
missing, source `~/.mpt-jules-env` when available. Check the isolated port and
Redis before running tests; use the saved helper if present, otherwise start only
the VM-local fixture:

```bash
set +x
if [[ -z "${MPT_TEST_REDIS_HOST:-}" || -z "${MPT_TEST_REDIS_PORT:-}" ||
      -z "${MPT_TEST_REDIS_DB:-}" || -z "${MPT_RUN_INTEGRATION_TESTS:-}" ]]; then
    if [[ -f "$HOME/.mpt-jules-env" ]]; then
        source "$HOME/.mpt-jules-env"
    fi
fi
export MPT_RUN_INTEGRATION_TESTS="${MPT_RUN_INTEGRATION_TESTS:-0}"
export MPT_TEST_REDIS_HOST="${MPT_TEST_REDIS_HOST:-127.0.0.1}"
export MPT_TEST_REDIS_PORT="${MPT_TEST_REDIS_PORT:-6389}"
export MPT_TEST_REDIS_DB="${MPT_TEST_REDIS_DB:-15}"
test "$MPT_RUN_INTEGRATION_TESTS" = 0 &&
test "$MPT_TEST_REDIS_HOST" = 127.0.0.1 &&
test "$MPT_TEST_REDIS_PORT" = 6389 &&
test "$MPT_TEST_REDIS_DB" = 15 || exit 1
if ! redis-cli -h 127.0.0.1 -p 6389 ping >/dev/null 2>&1; then
    if command -v mpt-redis-up >/dev/null 2>&1; then
        mpt-redis-up
    else
        redis-server --bind 127.0.0.1 --port 6389 --save '' --appendonly no \
            --daemonize yes --pidfile /tmp/mpt-jules-redis.pid --dir /tmp
    fi
fi
redis-cli -h 127.0.0.1 -p 6389 ping
```

This restores runtime processes without changing the saved setup or snapshot.
Setup always has network access; task-time networking supports connected MCP
services and documentation, while provider tests use mocks.

## Local API authentication

The account key in the private, gitignored `.env.jules` has been verified against
`Bzcasper/MoneyPrinterTurbo`. The unrelated account key in
`/home/bobby/.jules/env` was preserved.

Store keys privately as `JULES_API_KEY`; keep `.env.jules` readable only by its
owner. Do not include credentials in command arguments, task prompts or logs.

```bash
chmod 600 .env.jules
.venv/bin/python tools/jules_tasks.py check
.venv/bin/python tools/jules_tasks.py status
.venv/bin/python tools/jules_tasks.py submit --limit 1
```

The runner also accepts `JULES_API_KEY` from the environment or an explicit
`--env-file`. It verifies repository access, saves actual session IDs and skips
tasks with receipts. Use one manual runner at a time.

A rejected request exits nonzero. An ambiguous submission retains a
`dispatching_task` marker; inspect Jules sessions and reconcile the receipt before
clearing it or retrying. If Jules rejects creation with `FAILED_PRECONDITION` or
`RESOURCE_EXHAUSTED`, check active sessions and wait for capacity. The
[documented free-plan limit](https://jules.google/docs/usage-limits) is three
concurrent sessions. Preserve existing keys and sessions.

## Scheduled controller

[jules-autopilot.yml](../.github/workflows/jules-autopilot.yml) runs hourly at
minute 23, after same-repository **CI** completes, or by manual dispatch. It checks
out trusted `main` and installs no application dependencies. Runs serialize so
two controllers cannot spend the same session slot or overwrite the ledger.

[policy.json](../.github/jules/policy.json) sets a ceiling of 15 sessions in a
rolling 24-hour window and three concurrent sessions. These limits apply to the
whole Jules account, including manual work and other repositories, rather than
resetting at midnight. Actual throughput also depends on task completion and
available quota. [Jules usage limits](https://jules.google/docs/usage-limits)

The controller adopts manifest receipts, reconciles session state, and uses
bounded source searches to select scoped candidates. History and cooldowns
prevent repeated topics; active tasks reserve their owned files. A dispatch
intent is saved before creation so an ambiguous API response is reconciled
instead of blindly replayed. New sessions request `AUTO_CREATE_PR` through the
[sessions API](https://jules.google/docs/api/reference/sessions/).

The JSON ledger lives on `codex/jules-autopilot-state`, at `state.json`. GitHub
updates require the expected blob SHA, so a stale writer stops without replacing
newer history. Restarting the workflow resumes from this branch and Jules, not
from a runner's temporary disk.

Only PRs associated with tracked sessions are eligible for merge. They must
target this repository's `main`, stay within owned paths and size limits, and
pass **Python 3.11 tests**, **Python 3.13 tests** and **Windows smoke tests** on
the exact head SHA. Reported pending/failing checks or requested review changes
block merging. Stale or conflicted branches need refresh and fresh CI; protected
automation, credentials and deployment controls stay outside automatic tasks.
Repository branch protection remains authoritative.

A completed session's draft PR can be marked ready only after passing the same
scope, protection and exact-head CI gates. The controller records that action
before sending it and waits for a later fully checked run before merging.
`main` protection must require all three checks, require an up-to-date branch,
and apply to administrators; missing protection blocks automatic merges.

### Enable and operate

In repository **Settings → Secrets and variables → Actions**, save:

| Setting | Purpose |
| --- | --- |
| Secret `JULES_API_KEY` | Jules account key with repository access |
| Secret `JULES_GITHUB_TOKEN` | Repository-scoped bot token for ledger updates and PR merges |
| Variable `JULES_AUTOPILOT_ENABLED=true` | Enable controller runs |

Give the bot token Contents and Pull requests read/write, plus Checks, Actions,
Commit statuses and Administration read access for this repository. Keep its expiration and
rotation current. The workflow refuses live mutations without this dedicated
token. GitHub's built-in token suppresses push-triggered workflows and can leave
automation-created PR CI awaiting approval; a dedicated App/PAT avoids that
unattended-workflow limitation. [GitHub token behavior](https://docs.github.com/en/actions/concepts/security/github_token)

First dispatch **Jules autopilot** with `dry_run=true` while the enable variable
is absent or `false`; inspect its bounded summary. Only then set
`JULES_AUTOPILOT_ENABLED=true` and dispatch with `dry_run=false`. Scheduled and
CI-triggered runs are live. Set `JULES_AUTOPILOT_ENABLED=false` to stop future controller activity;
already-running Jules sessions continue.

```bash
# Offline: inspect candidate work without credentials or network calls.
python3 -m tools.jules_autopilot plan --policy .github/jules/policy.json

# Read-only account, ledger and PR reconciliation; credentials come from env.
python3 -m tools.jules_autopilot tick --policy .github/jules/policy.json --dry-run
```

Do not use the manual submission runner concurrently with the controller. A
quota/authentication error stops new dispatches; investigate the reported status
without deleting receipts, retrying an uncertain POST or weakening CI gates.
See [Jules MCP policy](JULES_MCP.md) for the six connected services.

## Configuration status

REST authentication is verified. The owner's successful setup log reports uv,
Python 3.11, locked dependencies, lint and **24 passing smoke tests**, followed by
the completed snapshot. Keep that saved environment as it is. This is
owner-reported cloud evidence; verification of environment-variable and Redis
restoration in the next real task shell remains pending. Remote workflow
activation also remains pending separate deployment evidence.
