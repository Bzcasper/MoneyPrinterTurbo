# Media Studio / MoneyPrinterTurbo — local Wan 2.2 image-first provider

This optional route is implemented by scripts/music_factory_wan22_local.py.
It is **disabled by default**. Neither installing model files nor successfully
queuing a ComfyUI prompt makes the local provider production-ready.

## Execution path

1. Scene metadata supplies a local still with anchor_qa_status=APPROVED (or image_qa_status=APPROVED).
2. The provider checks that image is within approved local roots, verifies its
   image format, and creates a distinct deterministic copy in ComfyUI/input.
3. It uses the verified Wan2.2 TI2V 5B FP8 e4m3fn scaled checkpoint
   (Kijai, 5.28GB), its checksum-verified manifest, and the native ComfyUI
   Wan graph. The UMT5 text encoder is assigned to CPU on this 8 GB GPU.
   Full-resolution VAE decode uses the built-in VAEDecodeTiled node with
   128px tiles, 64px overlap, 16-frame temporal chunks and 4-frame overlap.
4. Native generation is required to produce at least 1280x704, roughly five
   seconds and actual decoded-frame motion.
5. Padding to 1280x720 and transcoding to H.264 occurs **after** native-motion
   validation; it is not used to fake native resolution or motion.
6. The worker copies the approved local MP4 (never SCP from NUC for this route),
   rechecks its SHA-256 receipt, and performs canonical downstream motion QA.

The native video requirement is intentionally stricter than the low-resolution
engineering canary. A 384x224/25-frame test cannot authorize full-motion scenes.

## Eligibility

Production readiness requires a current on-host health receipt, provider
wan22_local, motion_verified, native_motion_verified, no_external_api_credits,
source_image_first_verified, production_resolution_verified, approved_free_route,
at least 1280x704 native frames, 4.7–5.3s duration, and a 64-digit SHA-256.
The receipt must explicitly set factory_publishing_approved=false.

Without that evidence, the /factory/next API returns
HOLD_MOTION_PROVIDER_UNVERIFIED and does not dispatch new work. The shared
factory selector also excludes Firefly when its strict zero-credit entitlement
is not currently verified. No provider response can approve public publishing.

## Current model and performance constraints

The Comfy-Org FP16 checkpoint is ~10GB, with ~6.74GB UMT5 and ~1.41GB VAE.
The optional Kijai scaled FP8 checkpoint is ~5.28GB. All are on the NUC in
read-only SSHFS storage; ComfyUI enumerates them through extra_model_paths.yaml.
On the ROG, network model-loading latency and 8GB GPU memory are substantial
constraints. A completed MP4 from a bounded engineering canary is a separate
step from validating production resolution and throughput.

Qdrant prompts_v2 retrieval remains part of Media Studio's upstream image
generation. This local video provider consumes the approved still and its
scene prompt; it does not silently replace or ignore the upstream prompt bank.

## Verification and recovery

Run (from the repository):
    python -m unittest discover -s tests -p test_wan_local_provider.py -v

The engineering canary and verified checkpoint manifests reside in the ROG
media-studio/wan2.2-canary directory. A read-only status checker resides in
the NUC media-studio-integration/wan-motion-20261120 directory.

Original router and worker files, their SHA-256 hashes, the integration diff,
and a tested copy-only restoration script are stored separately on the ROG at
media-studio/wan2.2-canary/mpt-integration-backup-20261120.

Do not enable publishing from a video-generation success response. Do not
automatically retry previously failed billed-provider work without matching
provider entitlement, technical QA, semantic QA, and explicit provider choice.
