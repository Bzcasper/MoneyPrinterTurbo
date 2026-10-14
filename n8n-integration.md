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

## Tips
- Check /docs (Swagger) at http://<host>:8090/docs for exact schemas.
- API key auth currently disabled on local; enable in config for production.
- Watch config oneapi/llm provider settings before generating.
"}