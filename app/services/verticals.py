# -*- coding: utf-8 -*-
"""
Content vertical presets: DIY tutorials, type-beat music videos, jewelry promos.

Each preset is a bundle of prompt enhancements and production defaults tuned
for one content type. Selecting a vertical fills the script prompt, the
Firefly image template and the matching production settings, so a new video
starts from prompts that already know the genre instead of generic ones.
"""

from __future__ import annotations

DIY_SCRIPT_PROMPT = """Write this as a hands-on DIY tutorial. Open with the finished result in one
enticing sentence, then teach step by step: name each step naturally ("First",
"Next", "Then"), one action per paragraph, concrete tools and materials with
amounts where it matters. Add one short safety or pro-tip line where relevant.
Close with the payoff (how it looks, feels or works now) plus a one-line nudge
to try it. Keep sentences short and speakable; no intro fluff, no headings."""

DIY_IMAGE_TEMPLATE = (
    "overhead workshop photo of {term}, hands crafting, bright natural light, "
    "organized tools nearby, detailed DIY tutorial style, photorealistic"
)

TYPE_BEAT_SCRIPT_PROMPT = """This video has NO voiceover - it is a visual loop for a type beat. Write at
most two short lines of on-screen text total (a hook line plus the producer
tag), designed to be read in under 3 seconds. No narration, no tutorial, no
explanation. Every keyword below must be a dark, loopable visual: neon-noir
city, luxury cars, smoke, rain, studio gear. Keep it minimal and atmospheric."""

TYPE_BEAT_IMAGE_TEMPLATE = (
    "dark cinematic still of {term}, neon-noir lighting, rain and smoke, "
    "luxury aesthetic, album-cover composition, ultra high contrast, "
    "photorealistic"
)

JEWELRY_SCRIPT_PROMPT = """Write this as a 30-second luxury jewelry spot. Line one is the hook - desire,
not description. Then exactly three short beats: masterful craftsmanship, the
materials (gold, diamonds, gemstones - be specific and sensory), and the moment
she wears it. Close with a single elegant call to action (shop the collection).
Short, speakable sentences with pauses; every line must sound expensive. No
tutorials, no prices, no fluff."""

JEWELRY_IMAGE_TEMPLATE = (
    "extreme macro luxury product photo of {term}, sparkling faceted diamonds, "
    "soft studio reflections, black velvet background, commercial jewelry "
    "photography, ultra detailed"
)

VERTICALS: dict[str, dict] = {
    "none": {
        "label": "No Preset",
        "description": "",
        "script_prompt": "",
        "image_template": "",
        "aspect": "",
        "voice_mode": "",
        "voice_note": "",
        "bgm_type": "",
        "clip_duration": 0,
        "paragraph_number": 0,
    },
    "diy": {
        "label": "DIY Tutorial",
        "description": (
            "Step-by-step build videos: result-first hook, one action per beat, "
            "workshop visuals, landscape YouTube format."
        ),
        "script_prompt": DIY_SCRIPT_PROMPT,
        "image_template": DIY_IMAGE_TEMPLATE,
        "aspect": "16:9",
        "voice_mode": "tts",
        "voice_note": "Clear instructional voice, e.g. en-US-GuyNeural.",
        "bgm_type": "random",
        "clip_duration": 5,
        "paragraph_number": 5,
    },
    "type_beat": {
        "label": "Type Beat Music Video",
        "description": (
            "Dark loopable visuals over your beat: no voiceover, minimal "
            "on-screen text, portrait Shorts format. Drop your beat file in "
            "as custom background music."
        ),
        "script_prompt": TYPE_BEAT_SCRIPT_PROMPT,
        "image_template": TYPE_BEAT_IMAGE_TEMPLATE,
        "aspect": "9:16",
        "voice_mode": "none",
        "voice_note": "No voiceover - the beat is the audio.",
        "bgm_type": "custom",
        "clip_duration": 5,
        "paragraph_number": 1,
    },
    "jewelry": {
        "label": "Jewelry Promo",
        "description": (
            "Luxury 30-second spots: desire hook, craftsmanship, materials, "
            "elegant close. Macro sparkle visuals, portrait format."
        ),
        "script_prompt": JEWELRY_SCRIPT_PROMPT,
        "image_template": JEWELRY_IMAGE_TEMPLATE,
        "aspect": "9:16",
        "voice_mode": "tts",
        "voice_note": "Expressive narrator voice, e.g. en-US-AriaNeural.",
        "bgm_type": "random",
        "clip_duration": 4,
        "paragraph_number": 4,
    },
}

VERTICAL_ORDER = ("none", "diy", "type_beat", "jewelry")


def get_vertical(key: str) -> dict:
    """Return the preset for key, falling back to the empty preset."""
    return VERTICALS.get(key or "none", VERTICALS["none"])


def apply_vertical_to_session_state(session_state, key: str) -> dict:
    """Write a vertical preset into raw (non-localized) widget state.

    Plain Streamlit widgets (text areas, sliders, text inputs) read plain
    session_state keys, so they are written here. Select-type widgets use
    language-suffixed keys (see ``localized_widget_key`` in the WebUI) and
    are returned under ``"stable"`` as base key -> value for the caller to
    apply with ``_set_stable_widget_value``. Aspect keys are per video
    source (``video_aspect_for_<source>_<lang>``); the caller sets every
    matching key already present in session_state.
    Returns ``{"stable": {...}, "notes": [...]}``.
    """
    preset = get_vertical(key)
    result: dict = {"stable": {}, "notes": []}
    if not preset["script_prompt"]:
        return result
    session_state["video_script_prompt"] = preset["script_prompt"]
    session_state["firefly_prompt_template_input"] = preset["image_template"]
    if preset["paragraph_number"]:
        session_state["paragraph_number_input"] = preset["paragraph_number"]
    if preset["voice_mode"]:
        result["stable"]["voice_mode_control"] = preset["voice_mode"]
    if preset["bgm_type"]:
        result["stable"]["bgm_type_select"] = preset["bgm_type"]
    if preset["clip_duration"]:
        result["stable"]["video_clip_duration_select"] = preset["clip_duration"]
    if preset["voice_note"]:
        result["notes"].append(preset["voice_note"])
    if key == "type_beat":
        result["notes"].append(
            "Add your beat file via Custom Background Music, or pick a "
            "generated track, before generating."
        )
    return result
