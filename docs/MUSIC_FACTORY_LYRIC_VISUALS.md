# Lyric-grounded music-video direction (v1)

## Production contract

- Vocal songs (BC TRAP GOD) use their **original, canonical Suno lyrics** matched by the immutable clip UUID and exact source title. Lyric text is treated as artistic evidence, not an executable instruction.
- A song without substantive matched lyrics is **not admitted** to the 30-scene automated queue. No invented replacement lyrics, plot, generic city world, or instrumental visualizer. Song audio always remains untagged.
- Ten ordered lyric passages produce three individually directed film shots each, for exactly 30 real moving clips. Every shot retains a real lyric excerpt and its zero-based source line position; neighboring shots must hand off exact closing/opening state.
- The same fictional adult protagonist, charcoal jacket, dark jeans, black shoes and anonymous non-artist likeness appears consistently; repeat the exact Character Bible in all 30 song generation prompts. If lyrics imply objects/locations, stage the physical action or motivated metaphor, not text over abstract imagery.
- Real video footage remains subject to independent decoded-motion and 29-edge continuity screening; the screening can flag weak structure but cannot approve creative/identity consistency. Commercial rights, human approval, originality review and YouTube channel verification remain separate release requirements.

## Evidence, limitations, and diagnostics

- `scripts/music_factory_lyrics_story.py` handles lyric validation, section/ad-lib filtering and chronological 10-act story design. `scripts/music_factory_catalog.py` skips missing-lyric MP3s. `scripts/music_factory_worker.py` refuses a legacy mood-only song treatment and enforces lyric-bearing prompts of at most 1485 characters.
- `scripts/music_factory_visual_qa.py` records exact 29 MP4 boundary measurements and weak-structure/hard-cut flags in `visual-continuity-review.json`. No automatic `visual_approved` changes.
- **ASR-anchored scene timing is now implemented**: a locally cached, CPU `faster-whisper` base model transcribes the SHA-verified song audio into timed words; monotonic 3+-word matches against canonical lyrics anchor ten acts and produce 31 variable scene boundaries. Song scene cuts follow those boundaries in the Modal FFmpeg xfade editor rather than uniform stretching. Songs with low match counts, less than 7/10 covered acts, invalid boundaries, or implausible shot lengths fail closed before expensive generation. The temporary source-audio copy is deleted, including on errors.
- Alignment evidence is saved to `lyric-timing.json` and embedded into story and render receipts. **This is inferred timing for private review, not a verified forced alignment or lyric-level lip sync**; first-frame image conditioning and final visual continuity still require review. Beat videos retain the original uniformly timed editor and two-tag preview policy.
- Private no-claim canary: `/home/bobby/Videos/scene-director/lyric-storyboards/LYRIC_REVIEW_caf91888-77ba-40ad-bf57-d627b4f39c8f.json`.

## Reloading the ROG API without losing a video worker

- The API is `mpt-api.service`. Its workers launch as detached child sessions; job status comes from the saved PID and the worker command line.
- Install the tracked drop-in at `deploy/systemd/mpt-api.service.d/10-preserve-media-workers.conf` into `~/.config/systemd/user/mpt-api.service.d/`. It sets `KillMode=process`, so a control-plane restart does not signal the active video-render child.
- `systemctl --user daemon-reload`, verify effective `KillMode=process`, restart the API, verify the existing video PID is still live and `/api/v1/internal/type-beat/factory/status` still reports `running=true` for that job.
- The one-minute n8n dispatcher must retain its busy check and atomic clip lease, and no rights/visual/human release gate is modified by this procedure.

## Prompt fidelity and video provider refusal handling

- The director must truncate prompt fields **at complete word boundaries**, preserving meaningful locations and handoffs instead of yielding malformed partial words. The October 8 `Grind Eternal` failure showed the phrase `night sky portal` shortened midword in slots 24 and 25; Adobe's upstream Firefly returned `[nsfw]` policy refusal for both. Truncation is a plausible trigger but not proven to be the sole cause; Firefly retains final moderation authority.
- A provider's explicit safety refusal is treated as `VideoProviderPolicyError`: do not immediately retry the identical rejected request. Genuine transient 500/timeouts remain subject to the bounded retry budget, preserving the existing zero-credit-only guarantee.
- Existing verified scene MP4s are kept on retry. The clip remains `FAILED_RETRYABLE` with the original delayed queue lease; the scheduler selects it when eligible, rather than creating a competing project. Unverified video providers remain disabled.
