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
