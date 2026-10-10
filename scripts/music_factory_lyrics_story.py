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
    "Character Bible: ONE anonymous fictional adult with a medium-build silhouette, "
    "plain charcoal hooded jacket with NO lettering, dark jeans, black boots. "
    "Hood up; face ALWAYS out of view, seen from behind or shadowed profile. "
    "Same silhouette and unchanged plain wardrobe in every shot, never clones."
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


def _world(lines: list[str]) -> tuple[str, str]:
    """Choose a SINGLE grounded visual world from the song's recurring imagery."""
    words = " ".join(lines).casefold()
    candidates = (
        (r"\b(water|ocean|sea|river|shore|wave|liquid|boat|tide)\b",
         "the same moonlit tidal causeway, wet dark stone beside a wide open sea",
         "deep cobalt water, subtle silver moonlight and warm amber rim light"),
        (r"\b(road|street|car|drive|highway|lane|wheel|block|sidewalk|corner|neighborhood)\b",
         "the same rain-polished empty city street beside one parked car",
         "deep navy street shadows, warm sodium-lamp light and teal reflections"),
        (r"\b(room|bed|house|home|apartment|door)\b",
         "the same lived-in apartment with one large window and a wooden table",
         "deep charcoal shadows, warm window light and muted amber practicals"),
        (r"\b(studio|stage|microphone|recording|rehearsal)\b",
         "the same real music rehearsal room with one microphone and two practical lamps",
         "low-key midnight blue, small amber practical lights and natural shadows"),
    )
    ranking = [(len(re.findall(p, words)), idx, world, light)
               for idx, (p, world, light) in enumerate(candidates)]
    n, _, world, light = max(ranking, key=lambda t: (t[0], -t[1]))
    if n >= 3:
        return world, light
    return (
        "the same physically grounded dim interior with one broad window and an open doorway",
        "low-key indigo shadows and a single warm practical edge light",
    )


def _lyric_stage(line: str) -> tuple[str, str]:
    """Concrete, word-derived film action; never invent a second character."""
    s = line.casefold()
    matched = (
        (r"\b(walk\w* on water|step\w* on liquid|foot.*ocean|walk.*wave)\b",
         "walks one steady step across shallow water, two clear concentric ripples forming around a black boot",
         "step across reflective water"),
        (r"\b(water|ocean|sea|river|wave|tide|liquid|boat|drown)\b",
         "reaches toward the water, its real wavelets reflecting across the plain charcoal sleeve",
         "reach toward moving water"),
        (r"\b(wrist|watch|chain|jewel|ice)\b",
         "lifts one wrist into a passing shaft of light; film the sleeve, skin and physically plausible reflection without writing",
         "lift the right wrist"),
        (r"\b(fire|flame|burn|ember|heat)\b",
         "passes a narrow patch of flame-lit stone, with warm light moving naturally across the jacket",
         "pass the firelit stone"),
        (r"\b(phone|message|texting|ring|calling)\b",
         "looks down at one blank-screen phone held naturally, then lowers it without showing any letters",
         "lower the blank phone"),
        (r"\b(car|drive|wheel|highway|road)\b",
         "follows the pavement toward one stationary car, with genuine background parallax",
         "move toward the car"),
        (r"\b(pray|god|heaven|holy|divine|angel|demon|miracle)\b",
         "stands alone as a distinct shaft of warm light breaks across the location, a restrained metaphor for the sung words",
         "follow the moving light"),
        (r"\b(competition|haters|enemies|rivals|jealous)\b",
         "walks past opposing dark reflections on the stone while remaining the only person in the scene",
         "pass the opposing shadows"),
        (r"\b(money|cash|dollar|bank|bill|paper)\b",
         "checks a single folded blank paper note without any printed numbers or writing",
         "fold the paper"),
        (r"\b(block|street|corner|hood|neighborhood|sidewalk)\b",
         "crosses the same rain-wet neighborhood block, passing a recognizable streetlight without any readable signs",
         "cross the wet street corner"),
        (r"\b(mirage|illusion|fake|not real|reflection)\b",
         "passes a distorted reflection in a rain puddle that naturally straightens as the camera moves",
         "walk past the moving reflection"),
        (r"\b(walk|walking|steppin|step|march|pace)\b",
         "takes two clearly visible steps forward with boots meeting the real surface on the same camera axis",
         "complete a step forward"),
        (r"\b(talk|speak|voice|say|tell|word|verse|bar|flow)\b",
         "raises one empty hand in a single measured speech gesture, then continues forward without lip-sync performance",
         "lower the gesturing hand"),
        (r"\b(fall|sink|down|grave|bottom|obstacle|bars)\b",
         "steps around a physical drop in the stone path, emphasizing resistance and balance",
         "step past the obstacle"),
        (r"\b(sun|dawn|morning|daylight)\b",
         "turns toward a gradual strip of natural sunrise above the established horizon",
         "turn toward the horizon"),
    )
    for pattern, action, gesture in matched:
        if re.search(pattern, s):
            return action, gesture
    return (
        "pauses in a physically believable reflective gesture tied to the sung line, then resumes moving forward in one continuous shot",
        "resume forward walking",
    )


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
    world, palette = _world(lines)
    scenes = []
    state = f"ONE hooded adult seen from behind, left foot leading into {world}."
    for act in range(10):
        cues = _choose_lyrics(lines, act)
        for part, (line_index, lyric) in enumerate(cues):
            shot = act * 3 + part + 1
            action, gesture = _lyric_stage(lyric)
            end = (
                f"ONE unchanged hooded adult continues to {gesture} in {world}; "
                "camera holds the same direction."
            )
            # Actual lyric evidence determines physical action. The camera
            # and location continue naturally from the preceding real shot.
            directive = (
                f"ONE plain-hooded protagonist {action}. "
                "ONE continuous moving shot in the identical real location; "
                "no duplicated subjects, lettering, or invented stage signage."
            )
            scenes.append({
                "master_beat": act+1,
                "act": f"LYRIC PASSAGE {act+1:02d}",
                "location": world,
                "camera": CAMERA_PASSES[act][part].replace("hero object's", "physical subject's").replace("hero prop", "lyric object"),
                "music_reaction": "The lyric determines the action; physical, motivated camera parallax.",
                "opening_state": state,
                "action": directive,
                "end_state": end,
                "lyric_excerpt": lyric,
                "lyric_line_index": line_index,
                "lyric_visual_grounded": not action.startswith(
                    "pauses in a physically believable reflective gesture"
                ),
                "lyric_timing": "ordered_estimate_not_forced_aligned",
                "shot_role": "lyric_wide" if part == 0 else ("lyric_detail" if part == 1 else "lyric_handoff"),
                "transition": "Continue the exact pose, silhouette, camera direction and lighting from the last frame.",
            })
            state = end
    return {
        "thesis": f"A continuous 10-act live-action interpretation of the actual sung lyrics in {title}.",
        "motif": "One unchanged hooded adult and recurring physical objects directly grounded in the lyrics, no clone figures.",
        "lighting": palette + "; identical practical lighting across the episode.",
        "palette": palette,
        "display_title": title[:64],
        "title_motion": "lyric_based_reveal",
        "media_kind": "song",
        "channel": "BC TRAP GOD",
        "genre": str(source.get("genre") or "")[:95],
        "character_bible": BIBLE,
        "planning_source": "canonical_suno_lyrics_grounded_v2",
        "lyrics_sha256": hashlib.sha256(lyrics.encode()).hexdigest(),
        "lyrics_line_count": len(lines),
        "lyric_grounded_scene_count": sum(
            bool(scene["lyric_visual_grounded"]) for scene in scenes
        ),
        "lyric_unresolved_scene_count": sum(
            not scene["lyric_visual_grounded"] for scene in scenes
        ),
        "story_quality_status": "REVIEW_ONLY_PARTIAL_GROUNDING",
        "lyrics_alignment_verified": False,
        "strict_image_conditioning_verified": False,
        "publishing_approved": False,
        "scenes": scenes,
    }
