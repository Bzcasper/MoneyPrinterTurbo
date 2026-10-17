# n8n "DIY Longform Factory" → MoneyPrinterTurbo pipeline mapping

Source: https://n8n.trapmoney.dpdns.org/workflow/6WLXL0NItkqrm7CI
Retrieved: 2026-10-14 (98 nodes, 73 connections, `active: false`)

Fetched with the operator-supplied public API key. The key is **not** stored in this
repo, not committed, and not written to any file. Re-fetch by exporting it in your
shell and calling the public API with `X-N8N-API-KEY`.

## What the workflow does

Long-form DIY explainer videos: research a trending topic, write a chaptered
script, build a scene manifest, generate Firefly stills + voiceover, gate every
asset through vision/audio QA, then submit one render job to a Modal service and
poll until it finishes.

## Stage-by-stage mapping

| # | n8n stage (nodes) | MPT equivalent |
|---|---|---|
| 1 | `Schedule Trigger`, `Build Trend Query`, `Perplexity Research`, `Trend Analyzer`, `Parse Trend Output` | No direct equivalent — upstream research. Closest: an operator-supplied `video_subject`. |
| 2 | `Map Trend To Form Fields` → `On form submission`, `Topic Request`, `Config` | `VideoParams` request body (`app/models/schema.py`). `Config` maps to `config.toml` / `config.example.toml`. |
| 3 | `HTTP Request` (Modal `/health`), `Server isn't ready`, `Notify Server Not Ready` | `scripts.integrations_preflight`; MPT's preflight before `mpt-api.service` start. |
| 4 | `Structure Agent` → `Parse Structure Output` → `Split Chapters` | `generate_script` (`app/services/task.py`) + `voice.split_script_paragraphs`. MPT already caps paragraphs to `paragraph_number`. |
| 5 | `Create Chapter Scenes` → `Parse Chapter Scenes` → `Chapter Media Enhancer` | `generate_terms` + `_materials_follow_script`. The Media Enhancer's per-scene style suffix is what `verticals.py` `*_TERMS_PROMPT` enforces. |
| 6 | `Aggregate Chapters`, `YouTube Metadata Agent`, `Parse YouTube Metadata` | `llm.generate_social_metadata`; MPT adds `verticals.apply_publishing_templates`. |
| 7 | `Split Scenes for Firefly`, `Is Title Card?`, `Guard Firefly Prompt`, `Batch Firely Scenes`, `Prepare Firefly Scene`, `Generate Firefly Scene` (sub-workflow `ffProviderSubV1`), `Pair Firefly Result`, `Attach Firefly Asset`, `Reassemble Firefly Scenes` | `get_video_materials` → `_download_videos_firefly_on_demand` (`app/services/material.py`): one still per script paragraph, `firefly_clip_durations`, hero shot, on-demand "stop when narration is covered", retry manifest via `task_artifacts`. |
| 8 | `Split Scenes for TTS`, `Prepare Firefly TTS`, `Generate Firefly TTS`, `Attach Firefly TTS`, `Merge TTS Into Scenes` | `generate_audio` / `voice` service. MPT uses configured TTS provider instead of a local n8n webhook. |
| 9 | `Enrich Hero Motion`, `Split Motion for QA`, `Probe DIY Motion URL`, `Build DIY Motion Vision Request`, `Call DIY Motion Vision QA` (GLM at `glm-zai-sandbox:5082`), `Normalize/Reassemble DIY Motion QA`, `Retry Rejected Motion` (+ retry QA pass) | MPT's independent QA: probe every scene MP4 before assembly, `test/services/test_qa_full_motion_project.py`, `video_project` QA + publishing hold endpoint. The n8n GLM vision gate is equivalent to MPT's TwelveLabs Pegasus QA (`app/services/twelvelabs.py`, consent-gated). |
| 10 | `Audio Production Policy`, `Media Manifest QA`, `Final Audio QA`, `Final Audio QA Passed?`, `Audio QA Failure` | MPT's beat/video QA gates; `video.analyze_beat` + `is_beat_length_mode`. |
| 11 | `Build Job Payload`, `Production Readiness Gate`, `Submit Job` → `{{MODAL_SERVER_URL}}/jobs`, `Wait`, `Check Job Status`, `Job Complete?`, `Job Failed?`, `Count Poll Attempt`, `Attempts Exceeded?` | `generate_final_videos` / `video_project` render + the canonical type-beat render API that polls for completion. |
| 12 | `Mark Topic Published`, `Notify Complete`, plus ~30 `dataTable` nodes | `_run_cross_post` / task state writes. MPT keeps state in Redis (`app/services/state.py`) instead of n8n DataTables. |

## External dependencies in the n8n workflow

- `https://mastermin206--explainer-videos-flask-app.modal.run` — the renderer (`/jobs`, polling).
- Sub-workflow `ffProviderSubV1` — Firefly still generation.
- `http://n8n:5678/webhook/firefly-provider-generate` — local Firefly TTS.
- `http://glm-zai-sandbox:5082/v1/chat/completions` — GLM vision QA (LAN-only, HTTP header auth).
- `lmChatGoogleGemini` — the LLM behind every agent node.
- ~30 n8n DataTable nodes for run logging/archiving.

## Conclusion

Every production stage in this workflow already exists natively in MoneyPrinterTurbo
(`generate_script` → `generate_terms` → `generate_audio` → `generate_subtitle` →
`get_video_materials` → `generate_final_videos`), including the parts n8n rebuilds by
hand: one Firefly still per paragraph with hero shot and retry manifest, per-scene
vision QA, and a Modal-backed canonical render. Running it through MPT removes n8n
from the critical path and keeps QA gates, publishing gates, and task state in one
place.

The only capability with no MPT counterpart is **stage 1 (trend research)** — an
operator-supplied subject covers it today.
