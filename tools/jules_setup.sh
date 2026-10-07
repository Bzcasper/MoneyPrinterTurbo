#!/usr/bin/env bash
set +x
set -euo pipefail

# Run in the MoneyPrinterTurbo checkout inside a Jules Ubuntu VM.
export PATH="$HOME/.local/bin:$PATH"
python3 -m pip install --user --upgrade 'uv==0.12.5'
uv python install 3.11
uv sync --frozen --python 3.11 --extra twelvelabs

if ! command -v ffmpeg >/dev/null || ! command -v redis-server >/dev/null; then
    sudo apt-get update
    sudo apt-get install -y --no-install-recommends ffmpeg redis-server
fi

# This daemon and database are dedicated to fixtures in the isolated VM.
export MPT_RUN_INTEGRATION_TESTS=0
export MPT_TEST_REDIS_HOST=127.0.0.1
export MPT_TEST_REDIS_PORT=6389
export MPT_TEST_REDIS_DB=15
if ! redis-cli -h "$MPT_TEST_REDIS_HOST" -p "$MPT_TEST_REDIS_PORT" ping >/dev/null 2>&1; then
    redis-server --bind "$MPT_TEST_REDIS_HOST" --port "$MPT_TEST_REDIS_PORT" \
        --save '' --appendonly no --daemonize yes \
        --pidfile /tmp/mpt-jules-redis.pid --dir /tmp
fi

uv run --no-sync python -m compileall -q \
    app cli.py main.py webui test docs/skill tools/sync_upstream.py
uv run --no-sync ruff check \
    app cli.py main.py webui test docs/skill tools/sync_upstream.py
uv run --no-sync python -m pytest -q \
    test/services/test_schema.py test/services/test_state.py

printf '%s\n' 'Jules environment ready: Python, locked dependencies, FFmpeg, Redis, lint, and smoke tests.'

