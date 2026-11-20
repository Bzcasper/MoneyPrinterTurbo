# Media Studio + Prompt Bank + ROG ComfyUI

Media Studio on the NUC routes selected image jobs through n8n to the ROG Visual Worker
at http://10.0.0.248:8096. The worker retrieves style techniques from Qdrant
prompts_v2 and runs the local FLUX.2 Klein 4B workflow on ComfyUI.

The independent MoneyPrinterTurbo worktree is configured in a private config.toml
with video_source=openai_image and openai_image_base_url=http://10.0.0.248:8096/v1.
The Visual Worker implements POST /v1/images/generations and returns
OpenAI-compatible data[0].url for consumption by MoneyPrinterTurbo.

The original MPT working directory remains untouched and its config was copied
rather than moved. The new config is ignored by Git and restricted to mode 0600.

## Verification

GET http://10.0.0.248:8096/health checks the Visual Worker.
GET http://10.0.0.248:8098/health checks retrieval.
POST /v1/images/generations returns HTTP 422 for invalid sizes before generation.
The /v1/images/generate/comfyui endpoint completed a real 512x512 image with
prompt-memory provenance.

## Limitations

MPT's full video render service is not deployed from this worktree yet. End-to-end
voice, FFmpeg, subtitle and timeline workflow readiness require separate checks.
Qdrant lookup currently uses keyword scoring over Qdrant collection payloads,
not embedding similarity, until its embedding runtime is restored.
