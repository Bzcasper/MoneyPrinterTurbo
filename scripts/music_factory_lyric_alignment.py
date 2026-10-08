"""ASR-anchored lyric passage timing for private music-video reviews.

Whisper speech recognition is an imperfect observation of vocals over music.
The canonical Suno lyrics remain the ONLY source for on-screen story prompts.
This module places act boundaries from monotonic multiword matches, exposes
interpolation/coverage, and never claims verified word-level lip sync.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
from statistics import median

TOKEN = re.compile(r"[a-z0-9]+")
MODEL = "base"
MIN_ACTS = 7
MIN_MATCH_TOKENS = 28
MIN_MATCH_FRACTION = .14


def normalized_words(text: str) -> list[str]:
    return TOKEN.findall(text.lower().replace("'", ""))


def _word_stream(segments: list[dict]) -> tuple[list[str], list[float]]:
    words, times = [], []
    for segment in segments:
        for w in segment.get("words") or []:
            t = w.get("start")
            if t is None or not math.isfinite(float(t)) or float(t) < 0:
                continue
            for tok in normalized_words(str(w.get("word") or "")):
                words.append(tok)
                times.append(float(t))
    return words, times


def _line_stream(lines: list[str]) -> tuple[list[str], list[int]]:
    words, indices = [], []
    for index, line in enumerate(lines):
        for tok in normalized_words(line):
            words.append(tok)
            indices.append(index)
    return words, indices


def _interpolated_line_times(
    lines: list[str], segments: list[dict], duration: float
) -> tuple[list[float], dict]:
    canonical, original_indices = _line_stream(lines)
    recognized, recognized_times = _word_stream(segments)
    if len(canonical) < 80 or len(recognized) < 70:
        raise ValueError("Insufficient lyric or audible vocabulary")
    matcher = difflib.SequenceMatcher(
        None, canonical, recognized, autojunk=False
    )
    pairs = [[] for _ in lines]
    matched = 0
    blocks = 0
    for block in matcher.get_matching_blocks():
        if block.size < 3:
            continue
        blocks += 1
        for n in range(block.size):
            li = original_indices[block.a + n]
            t = recognized_times[block.b + n]
            if t <= duration:
                pairs[li].append(t)
                matched += 1
    covered_acts = {
        min(9, index * 10 // len(lines))
        for index, points in enumerate(pairs) if points
    }
    fraction = matched / max(1, len(canonical))
    evidence = {
        "matched_tokens": matched,
        "canonical_token_count": len(canonical),
        "matched_fraction": round(fraction, 4),
        "matching_word_runs": blocks,
        "matching_acts": sorted(covered_acts),
        "matching_act_count": len(covered_acts),
        "anchored_line_count": sum(bool(p) for p in pairs),
        "source": "faster_whisper_word_times_plus_monotonic_multiword_anchors",
        "word_level_forced_alignment_verified": False,
        "human_verified": False,
    }
    if (
        matched < MIN_MATCH_TOKENS
        or fraction < MIN_MATCH_FRACTION
        or len(covered_acts) < MIN_ACTS
    ):
        raise ValueError("HOLD_LYRIC_TIMING_LOW_ASR_MATCH: " + json.dumps(evidence))
    known = [(i, median(times)) for i, times in enumerate(pairs) if times]
    # Whisper often misses musical intros; do not pretend a lyric starts at zero.
    # Only internal act boundaries consume these interpolated timings.
    line_times = []
    for index in range(len(lines)):
        if index <= known[0][0]:
            i1, t1 = known[0]
            i2, t2 = known[1]
        elif index >= known[-1][0]:
            i1, t1 = known[-2]
            i2, t2 = known[-1]
        else:
            hi = next(j for j, (i, _) in enumerate(known) if i >= index)
            if known[hi][0] == index:
                line_times.append(known[hi][1])
                continue
            i1, t1 = known[hi - 1]
            i2, t2 = known[hi]
        estimate = t1 + (index - i1) * (t2 - t1) / max(1, i2 - i1)
        line_times.append(max(0.0, min(duration, estimate)))
    # Enforce nondecreasing lyric time after ASR anchors (ASR may jitter).
    for i in range(1, len(line_times)):
        line_times[i] = max(line_times[i], line_times[i - 1])
    return line_times, evidence


def _normalize_boundaries(
    act_times: list[float], duration: float
) -> list[float]:
    """Thirty nonuniform scenes, three per act; bound pathological act lengths."""
    if len(act_times) != 11:
        raise ValueError("Expected ten act spans")
    bounds = [0.0]
    for act in range(10):
        start = act_times[act]
        end = act_times[act + 1]
        for part in (1, 2, 3):
            bounds.append(start + (end-start) * part/3)
    bounds[-1] = duration
    if len(bounds) != 31:
        raise ValueError("Timeline is not thirty scenes")
    durations = [bounds[i+1]-bounds[i] for i in range(30)]
    if not all(1.6 <= sec <= 21 for sec in durations):
        raise ValueError("HOLD_LYRIC_TIMING_SCENE_DURATION")
    return [round(x, 4) for x in bounds]


def align_segments(
    lines: list[str], segments: list[dict], duration: float
) -> dict:
    if len(lines) < 10 or not 60 <= duration <= 360:
        raise ValueError("Invalid song length or lyric evidence")
    estimates, evidence = _interpolated_line_times(lines, segments, duration)
    acts = [0.0]
    for act in range(1, 10):
        lyric_idx = (act * len(lines)) // 10
        acts.append(estimates[lyric_idx])
    acts.append(duration)
    scene_times = _normalize_boundaries(acts, duration)
    return {
        "status": "ASR_ANCHORED_PRIVATE_REVIEW",
        "scene_boundaries_seconds": scene_times,
        "act_boundaries_seconds": [round(x, 4) for x in acts],
        "duration_seconds": round(duration, 4),
        "asr_model": MODEL,
        "evidence": evidence,
        "timing_quality": "estimated_from_monotonic_word_matches",
        "word_level_forced_alignment_verified": False,
        "public_release_approved": False,
    }


def transcribe_lyrics_audio(audio: Path, *, threads: int = 4) -> list[dict]:
    from faster_whisper import WhisperModel

    model = WhisperModel(
        MODEL, device="cpu", compute_type="int8",
        cpu_threads=threads, num_workers=1, local_files_only=True,
    )
    recognized, _ = model.transcribe(
        str(audio), beam_size=2, language="en", word_timestamps=True,
        condition_on_previous_text=False, vad_filter=False, temperature=0,
    )
    result = []
    for item in recognized:
        words = [
            {"start": float(w.start), "end": float(w.end), "word": w.word,
             "probability": float(w.probability)}
            for w in item.words or [] if w.start is not None
        ]
        result.append({
            "start": float(item.start), "end": float(item.end),
            "text": item.text, "words": words,
        })
    return result


def song_timing(
    clip: dict, lyrics: str, folder: Path,
) -> dict:
    from scripts.music_factory_lyrics_story import lyric_lines
    from scripts.music_factory_catalog import MODAL

    lines = lyric_lines(lyrics)
    if not lines:
        raise ValueError("Missing substantive canonical source lyrics")
    output = folder / "lyric-timing.json"
    if output.is_file():
        prev = json.loads(output.read_text())
        if (
            prev.get("source_sha256") == clip["source_sha256"]
            and prev.get("lyrics_sha256") == hashlib.sha256(lyrics.encode()).hexdigest()
            and prev.get("status") == "ASR_ANCHORED_PRIVATE_REVIEW"
        ):
            return prev
    source = folder / ("timing-source." + clip["source_format"])
    try:
        if not source.is_file():
            subprocess.run(
                [str(MODAL), "volume", "get", clip["source_volume"],
                 clip["source_modal_path"], str(source)],
                check=True, timeout=180, capture_output=True, text=True,
            )
        if hashlib.sha256(source.read_bytes()).hexdigest() != clip["source_sha256"]:
            raise ValueError("Lyric timing source audio does not match canonical SHA")
        duration = float(clip["duration_seconds"])
        observed = float(json.loads(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(source)], check=True,
            capture_output=True, text=True, timeout=25,
        ).stdout)["format"]["duration"])
        if abs(observed - duration) > .3:
            raise ValueError("Lyrics-alignment source duration differs from canonical audio")
        segments = transcribe_lyrics_audio(source)
        report = align_segments(lines, segments, observed)
        report["source_sha256"] = clip["source_sha256"]
        report["lyrics_sha256"] = hashlib.sha256(lyrics.encode()).hexdigest()
        stage = output.with_suffix(".tmp")
        stage.write_text(json.dumps(report, indent=2) + "\n")
        stage.replace(output)
        return report
    finally:
        # Keep no duplicate full private source audio, including on failures.
        source.unlink(missing_ok=True)
