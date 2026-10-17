# MoneyPrinterTurbo Redis + TwelveLabs deployment (ROG)

## Current deployment

- Dedicated local **Redis 8.0.5** server (`/home/bobby/.local/redis-rootless/redis-server-bin`), bound **only to ROG 127.0.0.1:6381**, with a locally generated password, AOF persistence, `maxmemory 256mb`, and `noeviction` to protect queued jobs. The password was rotated on 2026-10-14 and is stored only in the private env file and local Redis config.
- Redis data: `/home/bobby/.local/share/mpt-redis/data`; Redis configuration and secret: `/home/bobby/.config/mpt-redis/redis.local.conf` and `/home/bobby/.config/mpt-redis/mpt-redis.env` (mode 0600). None are committed.
- The API's task-state `RedisState` and atomic `RedisTaskManager` both use `app.config.runtime_integrations.redis_settings()` and the same environment config. New state is Redis-backed; this is separate from the existing PostgreSQL/n8n music-library dispatch queue.
- Launch path for local API/WebUI processes is the private env file plus the checked-in `webui.sh`/`main.py`. Versioned systemd drop-in: `deploy/systemd/mpt-api.service.d/20-runtime-integrations.conf`. The preflight checks Redis before service startup; `KillMode=process` in the companion drop-in preserves detached video workers during API restarts.
- Health checks passed on 2026-10-14: authenticated Redis PING, API PING, `GET /api/v1/tasks` (HTTP 200), `RedisState` write/read/delete through `GET /api/v1/tasks/{id}` and DELETE, AOF restore, and Redis test suites. DB 0 is the live application DB.

## Local Redis start (user-level, no sudo)

```bash
export LD_LIBRARY_PATH=/home/bobby/.local/redis-rootless/liblzf/usr/lib/x86_64-linux-gnu
/home/bobby/.local/redis-rootless/redis-server-bin \
  /home/bobby/.config/mpt-redis/redis.local.conf \
  --daemonize yes \
  --pidfile /tmp/opencode/mpt-redis-6381.pid \
  --logfile /tmp/opencode/mpt-redis-6381-rootless.log
```

Run the API and WebUI with the private env files loaded:

```bash
set -a
. /home/bobby/.config/mpt-redis/mpt-redis.env
. /home/bobby/.config/mpt-redis/mpt-twelvelabs.env
set +a
cd /home/bobby/projects/MoneyPrinterTurbo
PYTHONPATH=/home/bobby/projects/MoneyPrinterTurbo .venv/bin/python main.py
```

## TwelveLabs integration

- SDK `twelvelabs==1.2.8` installed in the ROG MoneyPrinterTurbo `.venv`, matching `uv.lock` and the optional `pyproject.toml` TwelveLabs extra.
- Existing `app/services/twelvelabs.py` supports **Marengo text embeddings/reranking** (search-material relevance) and **Pegasus video description/QA** of a suitable publicly accessible or explicitly shared video URL.
- SDK method signatures, resource wrappers, and `VideoContext_Url` were checked offline on 2026-10-14. A locally configured key is present and `MPT_TWELVELABS_RERANK_TERMS=1`, so reranking is enabled for future pipeline runs. An authenticated Marengo smoke call completed on 2026-10-14 and returned a finite, nonzero 512-dim vector; no media upload, Pegasus analysis, or publishing action was performed.
- Credentials are read only at runtime from `/home/bobby/.config/mpt-redis/mpt-twelvelabs.env` (mode 0600), never placed in `config.app` or WebUI-saved `config.toml`. Supported variables:

```ini
MPT_TWELVELABS_API_KEY=PASTE_YOUR_REAL_KEY_LOCALLY
MPT_TWELVELABS_RERANK_TERMS=0
MPT_TWELVELABS_MARENGO_MODEL=marengo3.0
MPT_TWELVELABS_PEGASUS_MODEL=pegasus1.5
```

For multiple keys, `MPT_TWELVELABS_API_KEYS=keyA,keyB` is supported (round-robin). Do not put real credentials in Git, tickets, or chat messages. Obtain the key from the official TwelveLabs dashboard. Enable paid embedding/reranking only after reviewing your TwelveLabs quota and pricing: set `MPT_TWELVELABS_RERANK_TERMS=1` in the private env file. The Pegasus helper also requires a reachable video URL and access permissions; never make private or unapproved footage public just to run QA.

Restart during a controlled handoff: if the user unit exists, `XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus systemctl --user restart mpt-api.service`; check worker PID and `/api/v1/internal/type-beat/factory/status` afterward. Current local processes are started manually with the private env files until the systemd unit is installed. To independently run preflight, load your private Redis env file, then run `.venv/bin/python -m scripts.integrations_preflight`. Preflight returns `MPT_TWELVELABS_SDK_READY_KEY_CONFIGURED_API_NOT_CALLED` now that a key is configured.

## Operational boundaries

- Redis listens on ROG loopback only; never publish 6381 or reuse the NUC's `ai-redis` operational data.
- Runtime credentials are deliberately **not** inserted into config.app, because WebUI `save_config()` persists it to TOML.
- No public YouTube upload, rights gate or human/visual QA gate is changed by these integrations.
- Redis AOF contains task state and can hold private task metadata; back it up with the same privacy controls as the rest of the production system.


## Private video QA (explicit, opt-in)

TwelveLabs supports **direct local MP4 asset uploads** (up to 200 MB) and Pegasus analysis by reusable asset ID. This allows review without exposing a private video on a public URL. The SDK supports `VideoContext_AssetId` and synchronous `client.analyze` with `pegasus1.5` (SDK 1.2.8). Do not assume that a model-generated description is human visual approval.

Offline, no API key, no upload, no charge:

```bash
cd /home/bobby/projects/MoneyPrinterTurbo
.venv/bin/python -m scripts.twelvelabs_private_review \
  --video /home/bobby/Videos/scene-director/automatic/autovideo_caf9188877ba40adbf57d627b4f39c8f/final/autovideo_caf9188877ba40adbf57d627b4f39c8f-review.mp4
```

The offline smoke test verified that real private review video as a 64,345,478-byte, 195.75-second MP4 with SHA-256 `f5fed78ae42796e6d014112118da17bca107b804204a7ab042a46a153c0c7899`. Re-running it on 2026-10-14 returned `review_status: OFFLINE_PREFLIGHT_ONLY`, `twelvelabs_key_configured: true`, and `remote_upload_permitted: false`; no media left the machine.

**Only after a valid TwelveLabs API key is supplied and you approve usage:**

```bash
cd /home/bobby/projects/MoneyPrinterTurbo
set -a
. /home/bobby/.config/mpt-redis/mpt-twelvelabs.env
set +a
.venv/bin/python -m scripts.twelvelabs_private_review \
  --video /home/bobby/Videos/scene-director/automatic/autovideo_caf9188877ba40adbf57d627b4f39c8f/final/autovideo_caf9188877ba40adbf57d627b4f39c8f-review.mp4 \
  --allow-remote-upload
```

`--allow-remote-upload` is **required** even when credentials exist; this will send private copyrighted media and potentially lyric excerpts to TwelveLabs and may use a free quota or incur billable usage. The action is never invoked by the hourly music-video scheduler. The tool rejects MP4s outside the local factory directory, rejects files larger than 200 MB and durations outside 4 seconds to 1 hour, and checks for a configured API key before contacting the SDK. On successful upload it immediately saves the asset ID to a per-video private report (0600); future retries reuse that asset instead of uploading again. Once the same SHA-verified video and review prompt are analyzed, repeat calls reuse the completed report instead of calling the API again.

For original lyric songs, the review prompt includes the thirty ordered canonical lyric excerpts from `story.json` as **untrusted reference evidence**. It asks Pegasus to report visible correspondence to lyrics, inconsistent people/outfits/locations, and unwanted text. Pegasus does not receive authority to approve creative QA or public publishing, and both flags stay explicitly false. If TwelveLabs has no key, `--allow-remote-upload` must fail before upload or billing. An authenticated Marengo text-embedding smoke call completed on 2026-10-14, but no private review video upload or Pegasus analysis was performed.

Official API references:
- https://docs.twelvelabs.io/sdk-reference/python/upload-files/direct-uploads
- https://docs.twelvelabs.io/docs/guides/analyze-videos-and-images/videos

## Generated-video QA gate (phase 1)

`app/services/twelvelabs_qa.py` adds an opt-in QA stage after final videos are rendered and before cross-platform publishing. It is off by default. It never deletes a generated video and never marks video rendering failed.

Configuration is environment-first, so no credentials or paid-call approvals are persisted in `config.toml`:

```bash
MPT_TWELVELABS_QA_MODE=off|report|gate
MPT_TWELVELABS_QA_UPLOAD=0|1
MPT_TWELVELABS_QA_MAX_TOKENS=2048
MPT_TWELVELABS_QA_MAX_POLLS=36
MPT_TWELVELABS_QA_POLL_INTERVAL_SECONDS=5
```

- `report` runs QA and stores reports but does not block publishing.
- `gate` runs QA and blocks cross-posting unless every reviewed video returns both `verdict: "pass"` and `publishing_ready: true`.
- `MPT_TWELVELABS_QA_UPLOAD=1` is required before any media is uploaded. In `gate` mode without this explicit approval, publishing is fail-closed and reports `remote_upload_not_approved`.
- Reports are stored as private `0600` files at `storage/tasks/<task_id>/twelvelabs-qa-<video_index>.json` and summarized in `script.json` under `twelvelabs_review`.
- Task API/WebUI responses include `twelvelabs_review` and a `twelvelabs_qa_blocked` warning when gate mode blocks publishing.

Live enabling example:

```bash
export MPT_TWELVELABS_QA_MODE=report
export MPT_TWELVELABS_QA_UPLOAD=1
```

Do not use `gate` with private, copyrighted, personal, or otherwise restricted footage unless remote processing of that footage is explicitly authorized. TwelveLabs has duration/file-size limits and paid usage may apply.
