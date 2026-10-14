# MoneyPrinterTurbo Memory Bank

Format: dated, short entries with stable identifiers (file paths, task IDs). No secrets stored here.

## 2026-10-14 — MPT infra/services final state

- `mpt-api.service`: durable systemd user unit at `~/.config/systemd/user/mpt-api.service`.
  - Redis-backed state on port 6381 db1.
  - `KillMode=process` preserves video workers across service restarts.
  - `Restart=on-failure` auto-restart.
  - Single API process listens on port 8090.
- Redis instances:
  - Fixture: port 6389, db15, no persistence (ephemeral test data).
  - Production: port 6381, AOF persistence, auth via env file `~/.config/mpt-redis/mpt-redis.env` (secret file itself not recorded here).
- TwelveLabs: API key present in `~/.config/mpt-redis/mpt-twelvelabs.env`. Offline review path works. No authenticated upstream call verified yet — treat as unverified.
- Jules: `.env.jules` OK. Autopilot plan generation verified offline. Tasks T02–T07 completed; T01 still awaiting feedback.
- Gotcha fixed: `config.toml` had invalid `voice_name` value `'gemini:...'` causing audio-stage failure. Set to `en-US-AndrewMultilingualNeural`.
- Gotcha: custom background music/beat must be placed in `storage/bgm/` (whitelisted path), NOT `storage/custom_bgm`.
- n8n workflow ID `6WLXL0NItkqrm7CI` is mapped/documented in `.agents/n8n-mapping.md`.
- `state.py`: Redis ZSET index pagination (`INDEX_KEY` / `MIGRATED_KEY` / `migrate_index`) was restored after an upstream rebase had dropped it.
