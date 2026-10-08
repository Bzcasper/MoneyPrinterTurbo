"""Lyric-grounded 10-act, 30-shot storyboard for ORIGINAL vocal songs.

Lyrics are evidence, not instructions. Do not invent unmentioned events, claim
timestamps have been acoustically aligned, or borrow Strictly Beats imagery.
"""
from __future__ import annotations

import hashlib
import re

from scripts.music_factory_story import CAMERA_PASSES

SECTION = re.compile(r"^\s*\[[^\]]{1,75}\]\s*$")
INSTRUMENTAL = re.compile(r"^\s*(?:\[\s*instrumental\s*\]|instrumental)\s*$", re.I)
ANNOTATION = re.compile(r"^\s*\((?:yeah|uh|ay|oh|ooh|hey|ah|huh|woo|bow|nah|skrrt|grrt|brr|ad\s*libs?)(?:[,\s]+(?:yeah|uh|ay|oh|ooh|hey|ah|huh|woo|bow|nah|skrrt|grrt|brr))*\W*\)\s*$", re.I)
PLACE = (
    (r"\b(car|wheel|drive|road|lane|highway|street)\b", "a real roadway with one consistent car or pedestrian viewpoint"),
    (r"\b(room|bed|home|house|door|hallway)\b", "a lived-in interior with practical light and recognizable architecture"),
    (r"\b(phone|text|call|screen)\b", "a phone and the real action around receiving a message or call"),
    (r"\b(money|cash|paper|dollar|bank)\b", "physical currency or a grounded transaction with hands in frame"),
    (r"\b(water|river|ocean|sea|boat|wave|sink|splash)\b", "a physical water surface, its ripples, and the subject's actual interaction with it"),
    (r"\b(rain|storm|cloud|thunder)\b", "the exact weather and its physical effects on the environment"),
    (r"\b(stage|mic|studio|record|music)\b", "a believable rehearsal or recording space, without impersonating the artist"),
    (r"\b(heart|love|kiss|girl|boy|she|he|you|friend|enemy)\b", "a believable interpersonal interaction between recurring fictional adults"),
    (r"\b(god|devil|ghost|demon|pray|heaven|hell)\b", "a grounded moment of isolation with a symbolic shadow or shaft of light"),
    (r"\b(night|dark|moon|midnight)\b", "a dark real-world setting lit by motivated practical sources"),
    (r"\b(sun|morning|day|bright|dawn)\b", "daylight in the location established by the story"),
)
BIBLE = (
    "Character Bible: Same fictional adult protagonist, medium build, "
    "charcoal hooded jacket, dark jeans, black shoes, face not modeled "
    "after any real musician. Identical outfit, build and silhouette in every shot."
)
NEGATIVES = (
    "Negative constraints: no face drift, no changing facial features, no hairstyle changes, "
    "no outfit changes, no age changes, no body proportion changes, no art style shift, "
    "no unintended photorealism/3D shift, no extra limbs, no distorted hands, "
    "no inconsistent colors, no random accessories, no changed eye color, no altered silhouette."
)


def lyric_lines(raw: str) -> list[str]:
    """Preserve the real lyric order, rejecting placeholder and instrumental text."""
    if not isinstance(raw, str) or not 80 <= len(raw) <= 30000:
        return []
    lines = []
    for raw_line in raw.splitlines():
        line = " ".join(raw_line.strip().split())
        if not line or SECTION.fullmatch(line) or ANNOTATION.fullmatch(line):
            continue
        # Standalone parenthesized ad-libs are not narrative story beats.
        if line.startswith("(") and line.endswith(")"):
            spoken = re.findall(r"[a-zA-Z']+", line)
            if len(spoken) <= 3 or len(set(x.casefold() for x in spoken)) <= 2:
                continue
        if INSTRUMENTAL.fullmatch(line):
            return []
        # Strip stanza label only when it prefixes actual content.
        line = re.sub(r"^\[(?:verse|chorus|hook|bridge|intro|outro)[^\]]*\]\s*", "", line, flags=re.I)
        if len(line) >= 9:
            lines.append(line[:260])
    if len(lines) < 8 or sum(len(x) for x in lines) < 160:
        return []
    return lines


def qualified_catalog_lyrics(catalog: dict, clip_id: str, title: str) -> str | None:
    item = catalog.get(clip_id)
    if not isinstance(item, dict):
        return None
    if str(item.get("title") or "").strip().casefold() != title.strip().casefold():
        return None
    raw = item.get("lyrics")
    return raw if lyric_lines(raw) else None


def _setting(lines: list[str]) -> str:
    passage = " ".join(lines).lower()
    for pattern, setting in PLACE:
        if re.search(pattern, passage):
            return setting
    return "the real physical action or concrete metaphor described by the lyric passage"


def _choose_lyrics(lines: list[str], act: int) -> tuple[tuple[int, str], ...]:
    """Three chronological anchors from act's exact contiguous lyric span."""
    low = (act * len(lines)) // 10
    high = max(low + 1, ((act + 1) * len(lines)) // 10)
    high = min(high, len(lines))
    indices = (low, low + (high-low)//2, high-1)
    return tuple((i, lines[i]) for i in indices)


def build_lyric_story(source: dict, lyrics: str) -> dict:
    if source.get("media_kind") != "song":
        raise ValueError("Lyric director is only for vocal songs")
    lines = lyric_lines(lyrics)
    if not lines:
        raise ValueError("Missing substantive canonical vocal lyrics")
    title = str(source.get("title") or "").strip()
    if not title or not re.fullmatch(r"[a-f0-9-]{36}", str(source.get("clip_id") or "")):
        raise ValueError("Unverifiable song identity")
    scenes = []
    state = "The same fictional protagonist is entering the first lyric-defined moment."
    for act in range(10):
        cues = _choose_lyrics(lines, act)
        setting = _setting([lyric for _, lyric in cues])
        for part, (line_index, lyric) in enumerate(cues):
            shot = act * 3 + part + 1
            end = (
                f"The same protagonist finishes the action described by lyric passage {act+1}, "
                f"holding the action's final direction for the next shot."
            )
            # Literal evidence stays in the treatment; models may show the action
            # but may not render text overlays or embellish it with unrelated props.
            action = (
                f"Visually dramatize this exact sung moment: '{lyric[:145]}'. "
                "Show the subject, place and physical action implied by these words, "
                "not generic neon shapes or an unrelated fantasy environment."
            )
            scenes.append({
                "master_beat": act+1,
                "act": f"LYRIC PASSAGE {act+1:02d}",
                "location": setting,
                "camera": CAMERA_PASSES[act][part].replace("hero object's", "lyric subject's").replace("hero prop", "lyric subject"),
                "music_reaction": "Prioritize the sung words and the emotional turn, not beat-synced visual noise.",
                "opening_state": state,
                "action": action,
                "end_state": end,
                "lyric_excerpt": lyric,
                "lyric_line_index": line_index,
                "lyric_timing": "ordered_estimate_not_forced_aligned",
                "shot_role": "lyric_wide" if part == 0 else ("lyric_detail" if part == 1 else "lyric_handoff"),
                "transition": "Complete the preceding movement; preserve fictional character identity.",
            })
            state = end
    return {
        "thesis": f"A continuous 10-act live-action interpretation of the actual sung lyrics in {title}.",
        "motif": "Exactly the same protagonist and any recurring lyric-mentioned objects, not invented abstract props.",
        "lighting": "Motivated live-action cinematography; lighting changes only when the actual lyrics change setting or time.",
        "palette": "Natural cinematic color grounded in the words rather than an imposed house palette.",
        "display_title": title[:64],
        "title_motion": "lyric_based_reveal",
        "media_kind": "song",
        "channel": "BC TRAP GOD",
        "genre": str(source.get("genre") or "")[:95],
        "character_bible": BIBLE,
        "planning_source": "canonical_suno_lyrics_grounded_v1",
        "lyrics_sha256": hashlib.sha256(lyrics.encode()).hexdigest(),
        "lyrics_line_count": len(lines),
        "lyrics_alignment_verified": False,
        "strict_image_conditioning_verified": False,
        "publishing_approved": False,
        "scenes": scenes,
    }
