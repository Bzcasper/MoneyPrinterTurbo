# MoneyPrinterTurbo Redis + TwelveLabs deployment (ROG)

## Current deployment

- Dedicated Docker **Redis 7** container: `mpt-redis`, bound **only to ROG 127.0.0.1:6381**, with a 48-byte locally generated password, AOF persistence, `maxmemory 256mb`, and `noeviction` to protect queued jobs.
- Redis data: `/home/bobby/.local/share/mpt-redis/data`; Redis configuration and secret: `/home/bobby/.config/mpt-redis/redis.conf` and `mpt-redis.env` (mode 0600). None are committed.
- The API's task-state `RedisState` and atomic `RedisTaskManager` both use `app.config.runtime_integrations.redis_settings()` and the same environment config. New state is Redis-backed; this is separate from the existing PostgreSQL/n8n music-library dispatch queue.
- Runtime unit drop-in: `~/.config/systemd/user/mpt-api.service.d/20-runtime-integrations.conf`; versioned sample at `deploy/systemd/mpt-api.service.d/20-runtime-integrations.conf`. The preflight checks Redis before service startup; `KillMode=process` in the existing companion drop-in preserves detached video workers during API restarts.
- Health checks passed: authenticated Redis PING, write/read/delete, persistent AOF, actual RedisState hash and RedisTaskManager Lua atomic-queue admission in isolated DB 1, API `GET /api/v1/tasks` (HTTP 200) after restarting with Redis settings. DB 0 is the live application DB.

## TwelveLabs integration

- SDK `twelvelabs==1.2.8` installed in the ROG MoneyPrinterTurbo `.venv`, matching `uv.lock` and the optional `pyproject.toml` TwelveLabs extra.
- Existing `app/services/twelvelabs.py` supports **Marengo text embeddings/reranking** (search-material relevance) and **Pegasus video description/QA** of a suitable publicly accessible or explicitly shared video URL.
- SDK method signatures and `VideoContext_Url` checked offline; **an authenticated TwelveLabs API call has not been made because no API key is installed.**
- Credentials are read only at runtime from `/home/bobby/.config/mpt-redis/mpt-twelvelabs.env` (mode 0600), never placed in `config.app` or WebUI-saved `config.toml`. Supported variables:

```ini
MPT_TWELVELABS_API_KEY=PASTE_YOUR_REAL_KEY_LOCALLY
MPT_TWELVELABS_RERANK_TERMS=0
MPT_TWELVELABS_MARENGO_MODEL=marengo3.0
MPT_TWELVELABS_PEGASUS_MODEL=pegasus1.5
```

For multiple keys, `MPT_TWELVELABS_API_KEYS=keyA,keyB` is supported (round-robin). Do not put real credentials in Git, tickets, or chat messages. Obtain the key from the official TwelveLabs dashboard. Enable paid embedding/reranking only after reviewing your TwelveLabs quota and pricing: set `MPT_TWELVELABS_RERANK_TERMS=1` in the private env file. The Pegasus helper also requires a reachable video URL and access permissions; never make private or unapproved footage public just to run QA.

Restart during a controlled handoff: `XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus systemctl --user restart mpt-api.service`; check worker PID and `/api/v1/internal/type-beat/factory/status` afterward. To independently run preflight, load your private Redis env file, then run `.venv/bin/python -m scripts.integrations_preflight`. Preflight intentionally returns `MPT_TWELVELABS_DISABLED_NO_KEY` until an API key is set.

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

The offline smoke test verified that real private review video as a 64,345,478-byte, 195.75-second MP4 with SHA-256 `f5fed78ae42796e6d014112118da17bca107b804204a7ab042a46a153c0c7899`.

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

For original lyric songs, the review prompt includes the thirty ordered canonical lyric excerpts from `story.json` as **untrusted reference evidence**. It asks Pegasus to report visible correspondence to lyrics, inconsistent people/outfits/locations, and unwanted text. Pegasus does not receive authority to approve creative QA or public publishing, and both flags stay explicitly false. If TwelveLabs has no key, `--allow-remote-upload` must fail before upload or billing. An actual authenticated TwelveLabs request has **not yet been completed**.

Official API references:
- https://docs.twelvelabs.io/sdk-reference/python/upload-files/direct-uploads
- https://docs.twelvelabs.io/docs/guides/analyze-videos-and-images/videos
