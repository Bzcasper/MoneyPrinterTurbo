# Keeping our fork current

`origin` is our fork: https://github.com/Bzcasper/MoneyPrinterTurbo.
`upstream` is the official repository: https://github.com/harry0703/MoneyPrinterTurbo.

Our fork's default branch contains our customizations. Keep working on that
branch, or create another `codex/<task>` branch before making commits. The
fork's `main` branch represents upstream, not our customized application.

On a fresh clone of our fork, add the upstream remote and repository-local
settings once:

```sh
git remote add upstream https://github.com/harry0703/MoneyPrinterTurbo.git
git remote set-url --push upstream DISABLED
git config --local remote.pushDefault origin
git config --local push.default simple
git config --local pull.ff only
git config --local fetch.prune true
```

## Save local work

Review `git status` and `git diff --stat`, then stage the specific files you
want to save and commit them. Keep credentials, local configuration, generated
media, caches, and virtual environments out of Git.

```sh
git add <changed-files>
git commit -m "feat: describe the change"
git push -u origin HEAD
```

## Bring in upstream updates

From the repository root, on the `codex/` branch containing your customizations,
with a clean working tree and `uv` installed:

```sh
python3 tools/sync_upstream.py --publish --set-default
```

The helper saves your original commits on a dated local backup branch and
pushes that backup to your fork. It fetches upstream, creates a fresh dated
`codex/upstream-*` branch, and rebases your commits onto `upstream/main`.
It runs lint, compilation, tests, and the repository's coverage gate before
pushing the result and making it your fork's default branch. Every published
branch keeps its history; no force push is used. Fresh clones then start with
the latest verified customizations.

Without flags, the helper creates local backup and rebased branches and runs
checks, without publishing. `--publish` publishes without changing the fork's
default branch. Ordinary `git pull` only fast-forwards from your fork; it does
not replace this upstream sync command. Git cannot guarantee automatic conflict
resolution: the helper stops for review when necessary.

## If the rebase stops

Resolve the indicated files, stage only those resolutions, then run:

```sh
git rebase --continue
```

Repeat until complete, run the CI checks, and only then push the new branch
and set the fork default using `gh repo edit Bzcasper/MoneyPrinterTurbo
--default-branch <new-branch>`. The helper does not automatically resume its
verification/publishing steps after a manual `git rebase --continue`.

The CI checks are:

```sh
uv run --locked ruff check app cli.py main.py webui test docs/skill tools/sync_upstream.py
uv run --locked python -m compileall -q app cli.py main.py webui test docs/skill tools/sync_upstream.py
uv run --locked python -X utf8 -m coverage run -m pytest -q test
uv run --locked python -m coverage report
```

To abandon the attempt, use `git rebase --abort` and switch back to the
original branch printed by the helper. If checks fail after a successful
rebase, the new branch stays local for diagnosis and the fork default stays
unchanged. The dated backup preserves the original commits.

Repository-local settings use `origin` as the default push remote and
`pull.ff=only` to refuse an accidental divergent pull. No global Git settings
are required. Upstream's push URL is disabled locally; publish to `origin`.
