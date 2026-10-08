"""Deterministic 10-beat, 30-motion-scene continuity fallback for Suno factory.

Creative draft only. Never claims exact model-conditioned image continuity.
No character introduced, so identity can't silently drift from one face to another.
Channel brand identities stay separate for beats and original vocal songs.
"""

from __future__ import annotations

from hashlib import sha256
import re

WORLDS = {
    "beat": [
        (
            "Prismatic signal chamber",
            "a single luminous cobalt filament",
            "black glass and cyan edge light",
            (
                "mirror corridor",
                "refractive foyer",
                "signal elevator",
                "crystal tunnel",
                "pressure chamber",
                "floating bridge",
                "optic rotor",
                "signal furnace",
                "skyward aperture",
                "quiet rooftop",
            ),
        ),
        (
            "Amber industrial labyrinth",
            "a single amber pulse ring",
            "charcoal steel and warm practical amber",
            (
                "silent loading bay",
                "mechanical hall",
                "amber switchyard",
                "ribbed turbine tunnel",
                "sliding metal bridge",
                "industrial reservoir",
                "rotor vault",
                "wind-swept stairwell",
                "glass observatory",
                "distant skyline",
            ),
        ),
        (
            "Electric coastal night",
            "one bright floating teal compass",
            "deep midnight blue and wet cyan reflections",
            (
                "coastal power station",
                "rain-glazed pier",
                "tide gate",
                "harbor viaduct",
                "blue underpass",
                "saltwater glass gallery",
                "suspended tram line",
                "substation roof",
                "cloud-level antenna",
                "far horizon",
            ),
        ),
        (
            "Molten glass canyon",
            "one molten gold glass sphere",
            "obsidian ridges and firelit edges",
            (
                "black desert overlook",
                "basalt arcade",
                "slow lava channel",
                "stone needle valley",
                "geode tunnel",
                "golden fracture bridge",
                "light chamber",
                "upper cliff pass",
                "night sky portal",
                "still desert summit",
            ),
        ),
    ],
    "song": [
        (
            "Midnight memory journey",
            "one engraved copper record",
            "rain-slick indigo and restrained copper rim light",
            (
                "old recording room",
                "neon alley",
                "station steps",
                "tram platform",
                "underground passage",
                "warehouse corridor",
                "city canal bridge",
                "uphill street",
                "city lookout",
                "sunrise rooftop",
            ),
        ),
        (
            "Cobalt confession",
            "one folded silver letter",
            "low-key blue-black and silver practical reflections",
            (
                "empty loft",
                "window stairwell",
                "rain-soaked arcade",
                "train hall",
                "tunnel of lanterns",
                "river walkway",
                "industrial roof",
                "water tower stairs",
                "open overpass",
                "distant dawn skyline",
            ),
        ),
        (
            "Golden hour after dark",
            "one palm-sized golden lantern",
            "charcoal shadows and warm golden reflections",
            (
                "dark studio room",
                "narrow courtyard",
                "street of shuttered stores",
                "stone underpass",
                "empty market",
                "warehouse gantry",
                "quiet river crossing",
                "upper city walk",
                "glass observation deck",
                "first-light horizon",
            ),
        ),
        (
            "City of echoes",
            "one glass sound capsule",
            "cobalt dusk and soft violet neon spill",
            (
                "deserted rehearsal room",
                "service hallway",
                "midnight boulevard",
                "glass metro entrance",
                "tunnel platform",
                "under-city stair",
                "arched station bridge",
                "elevated train crossing",
                "windy observation tower",
                "open blue sky",
            ),
        ),
    ],
}
# Ten acts have distinct compositions, not the same three camera angles on loop.
# Within each act: reveal -> physically motivated detail -> crossing/handoff.
CAMERA_PASSES = (
    ("18mm ankle-height dolly toward the threshold, deep layered parallax",
     "85mm near-macro oblique insert, actual light grazing the hero object's rim",
     "28mm rear-follow tracking through one visible connecting doorway"),
    ("35mm elevated diagonal crane-down, reveal a new spatial axis",
     "100mm compressed detail tracking one physical reflection",
     "24mm low lateral pass ending at the same foreground occlusion"),
    ("28mm long-lens side silhouette with a foreground wipe",
     "50mm measured orbit no farther than 35 degrees around the hero object",
     "35mm shoulder-height push along the established travel direction"),
    ("24mm overhead geometric composition moving slowly downward",
     "90mm tight focus pull from surface texture to the exact same prop",
     "35mm side-track through the opening revealed in the same shot"),
    ("32mm slow forward pursuit framed behind repeating architecture",
     "75mm shallow-depth detail, tangible particles crossing the same light beam",
     "24mm smooth low-angle pull through a connected archway"),
    ("45mm symmetrical dolly through layered physical foreground",
     "100mm isolated refraction insert, hard practical light source in frame",
     "28mm stabilized rear three-quarter movement into next room"),
    ("20mm accelerating push, strong foreground-to-background separation",
     "65mm tracking close-up of the hero prop with real lens breathing",
     "35mm wide release into the same spatial corridor"),
    ("35mm low oblique tracking with longer background compression",
     "85mm controlled reflection close-up, zero composition reset",
     "24mm continuous rising crane revealing the next connected space"),
    ("28mm lateral reveal past a single sharp foreground structure",
     "105mm macro detail of light interacting with the locked prop material",
     "32mm trailing dolly toward an opening framed on the same axis"),
    ("35mm calm centered arrival, symmetrical physical staging",
     "85mm close-up of the unchanged prop settling into place",
     "24mm gentle withdrawal holding the final single light point"),
)
MOTION_BEATS = (
    "Establish depth: practical reflections lag the camera by real parallax; no artificial zoom.",
    "Make the first low-end impact felt as a subtle pressure wave through dust and haze.",
    "Increase tension via foreground occlusion, then reveal the unchanged hero prop.",
    "Accent the percussion with a single motivated specular highlight crossing metal.",
    "Build toward the hook using thicker atmosphere and a longer uninterrupted push.",
    "At peak intensity, light sources energize surrounding surfaces, not floating VFX.",
    "At the next strong downbeat, accelerate one physical object, keep the camera stable.",
    "Let the chorus return as a stronger version of the established visual motif.",
    "Release tension as airflow slows and practical lights soften naturally.",
    "Resolve with a long, readable silhouette and one sustained final luminance.",
)


def build_story(source: dict) -> dict:
    kind = source.get("media_kind")
    if kind not in WORLDS:
        raise ValueError("Unknown media lane")
    clip_id = str(source.get("clip_id") or "")
    if not re.fullmatch(r"[0-9a-f-]{36}", clip_id):
        raise ValueError("Canonical clip identity required")
    full_title = str(source.get("title") or "").strip()
    if len(full_title) < 3:
        raise ValueError("Title missing")
    words = re.findall(r"[A-Za-z0-9]+", full_title)
    if len(words) > 0 and words[0].lower() not in {"untitled", "track", "song"}:
        display_title = full_title if len(full_title) <= 33 else " ".join(words[:2])
    else:
        display_title = "Neon Pulse" if kind == "beat" else "Midnight Echo"
    index = int(sha256((clip_id + kind).encode()).hexdigest()[:8], 16) % len(
        WORLDS[kind]
    )
    name, prop, palette, settings = WORLDS[kind][index]
    genre = " ".join(str(source.get("genre") or "original sound").split())[:95]
    thesis = f"In {name.lower()}, the same {prop[4:]} follows a single unbroken physical journey through ten linked spaces, resolving on its final destination in time with {display_title}."
    # No performers or faces are invented for vocal songs; avoids misrepresenting the artist.
    motif = f"The same {prop}, with its exact color, shape, and scale in every shot; no characters or logos."
    prev = f"The same {prop} is hovering at the threshold of {settings[0]} under {palette}."
    scenes = []
    for master, location in enumerate(settings, 1):
        next_place = settings[master] if master < 10 else "the still final frame"
        for part in range(1, 4):
            if part == 1:
                action = f"Continue the same {prop} moving from the previous threshold into {location}; the camera reveals this setting without resetting spatial direction."
                end = f"The unchanged {prop} now floats inside {location}, moving ahead through the same practical light."
            elif part == 2:
                action = f"The identical {prop} crosses a distinctive foreground of {location}; layered real materials react to its soft physical light as the camera holds a smooth tracking move."
                end = f"The unchanged {prop} reaches the far end of {location}, still travelling toward the next exit."
            else:
                action = (
                    f"The same {prop} moves through a visible connected passage from {location} into {next_place}; maintain exact trajectory and palette."
                    if master < 10
                    else f"The same {prop} slowly comes to rest at {location}; the camera drifts backward as the glow fades to a final steady point."
                )
                end = (
                    f"The unchanged {prop} is crossing the passage from {location} to {next_place} in one continuous direction."
                    if master < 10
                    else f"The unchanged {prop} is still at {location}, its light reduced to a single steady point."
                )
            scenes.append(
                {
                    "master_beat": master,
                    "act": f"{name.upper()} — {master:02d}",
                    "location": location,
                    "camera": CAMERA_PASSES[master - 1][part - 1],
                    "music_reaction": MOTION_BEATS[master - 1],
                    "opening_state": prev,
                    "action": action,
                    "end_state": end,
                    "transition": "Preserve the prior physical movement and original lighting; no reset, freeze or slideshow.",
                    "shot_role": "hero_handoff" if part == 3 else "coverage",
                }
            )
            prev = end
    assert len(scenes) == 30 and all(
        scenes[i]["opening_state"] == scenes[i - 1]["end_state"] for i in range(1, 30)
    )
    return {
        "thesis": thesis,
        "motif": motif,
        "lighting": f"Continuous {palette}; naturally motivated spatial changes only.",
        "palette": palette,
        "display_title": display_title,
        "title_motion": "swoop" if kind == "beat" else "refraction",
        "media_kind": kind,
        "channel": "Strictly Beats" if kind == "beat" else "BC TRAP GOD",
        "genre": genre,
        "planning_source": "deterministic_distinct_world_review_fallback",
        "strict_image_conditioning_verified": False,
        "publishing_approved": False,
        "scenes": scenes,
    }
