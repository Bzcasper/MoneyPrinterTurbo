"""Evidence-only QA of scene-to-scene visual continuity for 30 native MP4 shots.

Text-only prompts do not guarantee pixel continuity. This report identifies
hard cuts needing human review without falsely certifying image conditioning.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

FRAME_WIDTH = 160
FRAME_HEIGHT = 90
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT * 3


def frame_at(video: Path, seconds: float) -> bytes:
    result = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-ss", f"{seconds:.3f}",
            "-i", str(video), "-frames:v", "1", "-vf",
            f"scale={FRAME_WIDTH}:{FRAME_HEIGHT}", "-f", "rawvideo",
            "-pix_fmt", "rgb24", "pipe:1",
        ],
        capture_output=True, check=True, timeout=25,
    )
    if len(result.stdout) != FRAME_BYTES:
        raise ValueError(f"Invalid decoded scene frame: {video.name}")
    return result.stdout


def pair_metrics(a: bytes, b: bytes) -> dict:
    if not a or len(a) != len(b) or len(a) % 3:
        raise ValueError("Mismatched or empty RGB frames")
    length = len(a)
    mae = sum(abs(p - q) for p, q in zip(a, b)) / (255 * length)
    x = [(a[i] + a[i + 1] + a[i + 2]) / 765 for i in range(0, length, 3)]
    y = [(b[i] + b[i + 1] + b[i + 2]) / 765 for i in range(0, length, 3)]
    n = len(x)
    ux, uy = sum(x) / n, sum(y) / n
    vx = sum((v - ux) ** 2 for v in x) / n
    vy = sum((v - uy) ** 2 for v in y) / n
    cov = sum((p - ux) * (q - uy) for p, q in zip(x, y)) / n
    ss = ((2 * ux * uy + .0001) * (2 * cov + .0009)) / (
        (ux * ux + uy * uy + .0001) * (vx + vy + .0009)
    )
    # Review heuristics calibrated against a real 30-shot factory master.
    # This is NOT proof of true narrative drift or first-frame conditioning.
    weak_structure = ss < .28
    severe = mae > .125 and ss < .20
    return {"luma_ssim": round(ss, 4), "rgb_mae": round(mae, 4),
            "weak_structure_similarity": weak_structure,
            "high_visual_discontinuity": severe}


def audit_scenes(sources: list[Path]) -> dict:
    if len(sources) != 30 or not all(v.is_file() for v in sources):
        raise ValueError("Require all thirty canonical MP4 scenes")
    start_frames = [frame_at(v, .2) for v in sources]
    end_frames = [frame_at(v, 4.7) for v in sources]
    rows = []
    for n in range(29):
        metrics = pair_metrics(end_frames[n], start_frames[n + 1])
        rows.append({"from_scene": n + 1, "to_scene": n + 2, **metrics})
    severe = [r for r in rows if r["high_visual_discontinuity"]]
    weak = [r for r in rows if r["weak_structure_similarity"]]
    return {
        "method": "decoded_frame_end_to_start_similarity",
        "conditioning_verified": False,
        "visual_qa_approved": False,
        "screening_status": "REVIEW_REQUIRED" if severe else "REVIEW_REQUIRED_UNCONDITIONED",
        "audited_transitions": len(rows),
        "high_discontinuity_count": len(severe),
        "weak_structure_count": len(weak),
        "high_discontinuity_pairs": [
            [x["from_scene"], x["to_scene"]] for x in severe
        ],
        "weak_structure_pairs": [[x["from_scene"], x["to_scene"]] for x in weak],
        "thresholds": {"hard_cut_rgb_mae_gt": .125,
                       "hard_cut_luma_ssim_lt": .20,
                       "weak_structure_ssim_lt": .28},
        "transition_metrics": rows,
        "warning": "Pixel similarity alone cannot establish true narrative continuity.",
    }


def save_review(sources: list[Path], destination: Path) -> dict:
    report = audit_scenes(sources)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.with_suffix(".tmp")
    stage.write_text(json.dumps(report, indent=2) + "\n")
    stage.replace(destination)
    return report
