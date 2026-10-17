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

## 2026-10-14 — DIY Longform Factory restructure

- 3 feature commits on branch `codex/upstream-20261014-195226-511649`:
  - `382060f` — preset production profiles.
  - `1535997` — factory planner/renderer + OM bridge.
  - `06d4437` — CLI runner + doc.
- `app/services/verticals.py`: each preset (diy/type_beat/jewelry) now has a `"production"` block: art_style/visual bible, planning (chapters/scenes/pacing), render (res/fps/transition/volumes), QA gates, openmontage multiplier settings. No preset has `production=None`. Backward compat kept; 35 existing preset tests pass.
- `app/services/diy_factory.py`: replaces n8n workflow `6WLXL0NItkqrm7CI` Modal render hop.
  - `plan_structure` / `write_chapter_scenes` (injectable generate callable).
  - `aggregate_scenes` (title cards + numbering).
  - `readiness_gate` (QA gate).
  - `to_project_spec` + `render_factory_video` (uses `app/services/video_project.py` native FFmpeg render).
  - `save_run` (one JSON per run + append-only `runs.jsonl`), replaces ~20 n8n DataTable nodes.
- `app/services/openmontage_bridge.py`: `export_project` writes OpenMontage `projects/<name>/` scaffold; `plan_shorts` proposes short derivatives for the clip-factory pipeline. Never shells out/publishes.
- `scripts/diy_factory.py`: CLI runner. `--demo` runs full chain offline with synthetic clip (no provider/network). Real runs use `--llm` + `--media-dir`. `--openmontage-root` exports OM scaffold + shorts plan. Flagged in commit that `scripts/**` is AGENTS.md-protected; user may want it moved to `cli.py`.
- Verified end-to-end: diy demo rendered 1920x1080 h264+aac 40s master; jewelry rendered portrait 1080x1920. 17 new tests in `test/services/test_diy_factory.py`; lint+compile clean; 52 tests pass (new + existing presets).
- Gotcha: `video_project.create_revision` rejects narration without prepared audio — demo path supplies silent audio via ffmpeg `anullsrc`.
- OpenMontage repo cloned at `/home/bobby/projects/OpenMontage` (has clip-factory pipeline for volume).

