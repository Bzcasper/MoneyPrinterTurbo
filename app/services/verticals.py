# -*- coding: utf-8 -*-
"""
Content vertical presets: DIY tutorials, type-beat music videos, jewelry promos.

Each preset is a bundle of prompt enhancements and production defaults tuned
for one content type. Selecting a vertical fills the script prompt, the
Firefly image template and the matching production settings, so a new video
starts from prompts that already know the genre instead of generic ones.
"""

from __future__ import annotations

from string import Formatter

DIY_SCRIPT_PROMPT = """Write a spoken, TTS-safe DIY tutorial in the requested language.
Write all numbers and units as words. Use no markdown, titles, bullets, emoji,
parentheses, welcome lines or subscribe lines. Start with a finished-result
hook, name the materials, then give one simple step per paragraph. Close with
the finished result and a useful tip. Keep every sentence under twenty words.
Include a short safety note only where the tools or materials require it.
Use natural spoken transitions and concrete instructions, without intro fluff."""

DIY_TERMS_PROMPT = """Return {amount} complete English scene prompts as a JSON array of strings,
and nothing else. Use one scene per script paragraph, in paragraph order.
Each scene description must contain
twenty to thirty-five words before its mandatory style suffix. Describe one
scene and one simple action, using close-ups or macro views of hands, tools
and materials. Show no faces, readable text, logos, brands or numbers. Use a
static camera or a slow push-in. The first scene shows the finished project;
the last shows the finished project in use. Keep the project and materials
consistent throughout. Append exactly this suffix to every prompt:
photorealistic, natural window light, clean wooden workbench, shallow depth of field, 4k"""

DIY_IMAGE_TEMPLATE = (
    "overhead workshop photo of {term}, hands crafting, bright natural light, "
    "organized tools nearby, detailed DIY tutorial style, photorealistic"
)

TYPE_BEAT_SCRIPT_PROMPT = """Write a concise visual treatment for an instrumental type-beat video.
Describe one continuous abstract cinematic world that evolves with the music.
Use no narrator dialogue, lyrics, artist comparisons, people, faces, silhouettes,
brands, copyrighted characters or readable text. Emphasize color, geometry,
light, reflective materials, particles, atmosphere, scale and beat-reactive
transformation. Keep the treatment suitable for a 16:9 YouTube master."""

TYPE_BEAT_TERMS_PROMPT = """Return {amount} complete English scene prompts as a JSON array of strings,
and nothing else. Every scene belongs to one continuous vibrant abstract film.
Describe decisive focal geometry or environment, saturated spectral color,
foreground/midground/background depth, emissive light, reflective materials,
particles, atmospheric scale and one clear visual transformation or motion cue.
Show no people, faces, bodies, silhouettes or character-like figures. Use no
artist names, brands, copyrighted characters, logos or readable text. Vary shot
geometry and scale while preserving one coherent palette, material language and
lighting logic across all scenes. Append exactly this suffix to every prompt:
vibrant abstract cinematic masterpiece, saturated spectral light, reflective materials, volumetric atmosphere, deep spatial layers, premium 16:9 composition, no people"""

TYPE_BEAT_IMAGE_TEMPLATE = (
    "vibrant abstract cinematic frame of {term}, saturated spectral light, "
    "reflective materials, volumetric atmosphere, deep spatial layers, "
    "premium 16:9 composition, no people"
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
        "terms_prompt": "",
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
        "terms_prompt": DIY_TERMS_PROMPT,
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
            "Vibrant character-free abstract visuals over the full beat: no "
            "voiceover and no embedded text, composed as a landscape YouTube "
            "master. Drop your beat file in as custom background music."
        ),
        "script_prompt": TYPE_BEAT_SCRIPT_PROMPT,
        "terms_prompt": TYPE_BEAT_TERMS_PROMPT,
        "image_template": TYPE_BEAT_IMAGE_TEMPLATE,
        "aspect": "16:9",
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
        "terms_prompt": "",
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

_PUBLISHING_FIELDS = ("username", "title_template", "description_template", "youtube_privacy_status")
_PUBLISHING_TEMPLATE_FIELDS = frozenset({
    "subject", "title", "description", "hashtags", "vertical", "bpm", "key", "genre", "lease_url",
})


def get_publishing_settings(key: str, app_config: dict) -> dict:
    """Read only this vertical's optional public publishing overrides."""
    bindings = app_config.get("upload_post_verticals", {})
    binding = bindings.get(key, {}) if isinstance(bindings, dict) and key != "none" else {}
    if not isinstance(binding, dict):
        return {}
    settings = {field: binding.get(field, "") for field in _PUBLISHING_FIELDS}
    return settings if any(settings.values()) else {}


def validate_publishing_settings(settings: dict) -> dict:
    """Validate templates before queueing an upload; never include values in errors."""
    if not isinstance(settings, dict):
        raise ValueError("vertical publishing settings must be a table")
    normalized = {}
    for field in _PUBLISHING_FIELDS:
        value = settings.get(field, "")
        if not isinstance(value, str):
            raise ValueError("vertical publishing fields must be strings")
        normalized[field] = value.strip()
    if normalized["youtube_privacy_status"] not in {"", "public", "unlisted", "private"}:
        raise ValueError("vertical YouTube privacy must be public, unlisted, private or blank")
    for field in ("title_template", "description_template"):
        try:
            for _, placeholder, spec, conversion in Formatter().parse(normalized[field]):
                if placeholder is not None and (
                    placeholder not in _PUBLISHING_TEMPLATE_FIELDS or spec or conversion
                ):
                    raise ValueError("unsupported placeholder")
        except ValueError as exc:
            raise ValueError("publishing templates contain an unsupported or malformed placeholder") from exc
    return normalized


def apply_publishing_templates(metadata: dict, settings: dict, context: dict) -> dict:
    """Keep generated metadata as the fallback and substitute explicit templates."""
    settings = validate_publishing_settings(settings)
    result = dict(metadata)
    values = {field: str(context.get(field) or "") for field in _PUBLISHING_TEMPLATE_FIELDS}
    values.update(
        title=str(metadata.get("title") or context.get("subject") or ""),
        description=str(metadata.get("caption") or ""),
        hashtags=" ".join(str(tag) for tag in (metadata.get("hashtags") or [])),
    )
    for field, template in (("title", "title_template"), ("caption", "description_template")):
        if settings[template]:
            result[field] = settings[template].format_map(values)
    return result


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
    session_state["beat_length_mode_input"] = key == "type_beat"
    if key == "type_beat":
        result["notes"].append(
            "Add your beat file via Custom Background Music, or pick a "
            "generated track, before generating."
        )
    return result
