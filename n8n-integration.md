# n8n integration guide — MoneyPrinterTurbo API

Base URL (local): http://127.0.0.1:8090
(Use your LAN/hostname URL when calling from n8n.)

Ready-to-import workflow: `n8n-flows/moneyprinterturbo-full-video.json`

Working notes (local):
- `edge_tts` Azure v1 voices currently fail with `403 Invalid response status`; use a `gemini:` voice such as `gemini:Rasalgethi-Informative`.
- Download path in the workflow strips the leading slash from `data.videos[0]` to match `/api/v1/stream/{file_path}`.

## Core endpoints
- POST /api/v1/scripts — body: {video_subject, video_script, video_language, ...} → {script}
- POST /api/v1/terms — {video_subject, video_script, amount} → search terms
- POST /api/v1/audio — {text, voice_name, speed, pitch} → generated audio file
- POST /api/v1/subtitle — {audio_path, ...} → subtitle (SRT/VTT)
- POST /api/v1/video_materials — search materials (pixabay/pexels)
- POST /api/v1/videos — full pipeline: creates a task from params and starts generation
- GET /api/v1/tasks — list task ids
- GET /api/v1/tasks/{task_id} — task status and result video path
- GET /api/v1/musics, POST /api/v1/musics — background music
- POST /api/v1/social-metadata — generate social captions
- GET /api/v1/stream/{file_path}, GET /api/v1/download/{file_path} — retrieve outputs

## Minimal n8n flow
1. Set trigger (Manual/HTTP/webhook).
2. HTTP Request node → POST /api/v1/videos with JSON body `TaskVideoRequest` (subject, script, voice). Local working voice: `gemini:Rasalgethi-Informative`.
3. Wait/loop Get task: HTTP GET /api/v1/tasks/$taskId until status=complete or has video.
4. Download: GET /api/v1/download/<video_path> or stream.
5. Optional: POST /api/v1/social-metadata for captions; upload to other platforms.

## DIY Longform Factory (replaces the n8n "DIY Longform Factory" render hop)

The old n8n workflow 6WLXL0NItkqrm7CI planned scenes/chapters in n8n, then shipped a
job to a Modal service for rendering. That render hop is now native to MPT: the
chapter/scene *planning* and *run logging* live in
`app/services/diy_factory.py`, and the *render* uses this repo's own
`app/services/video_project.py` (deterministic FFmpeg, no external service or
credential).

Run the full chain from the repo root:

```bash
# Offline smoke test (built-in fixture plan + synthetic clip, no provider/network):
uv run --locked python -m scripts.diy_factory --demo \
  --topic "Build a birdhouse" --vertical diy \
  --project-dir storage/projects/birdhouse

# Real run (configured LLM + your prepared scene media):
uv run --locked python -m scripts.diy_factory \
  --topic "Build a birdhouse" --vertical diy --llm oneapi \
  --media-dir /path/to/prepared/scenes \
  --project-dir storage/projects/birdhouse \
  --openmontage-root /home/bobby/projects/OpenMontage
```

`--media-dir` must contain `scene-NNN.mp4` (footage) and optional
`scene-NNN.mp3` (narration audio) per scene number. Planning output, the
readiness gate result, and a run-log line are persisted under
`<project-dir-parent>/factory-runs/` (one JSON per run + append-only
`runs.jsonl`), replacing the workflow's ~20 n8n DataTable nodes.

Each preset's `production` block in `app/services/verticals.py` carries the art
style / visual bible, chapter+scene targets, render settings (resolution, fps,
transition, volumes) and QA gates for that vertical.

### OpenMontage output multiplier

OpenMontage (cloned at `/home/bobby/projects/OpenMontage`) multiplies one master
into many shorts/derivatives. Passing `--openmontage-root` writes a
`projects/<project-id>/` scaffold (`project.json`, `artifacts/script.json`,
`artifacts/scene_plan.json`, plus `master_video`) that the OpenMontage agent
picks up, and computes a shorts plan (scene windows mapped to its `clip-factory`
pipeline). It never publishes automatically; the OpenMontage agent runs its own
checkpoints.

## Tips
- Check /docs (Swagger) at http://<host>:8090/docs for exact schemas.
- API key auth currently disabled on local; enable in config for production.
- Watch config oneapi/llm provider settings before generating.
"}