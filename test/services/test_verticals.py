# -*- coding: utf-8 -*-
import unittest

from app.models.schema import VideoAspect
from app.services import verticals


class TestContentVerticals(unittest.TestCase):
    """
    内容垂类预设的契约测试：每个预设必须自带可用的脚本提示词、
    带 {term} 占位符的图片模板，以及合法的生产设置。不跑网络。
    """

    def test_all_verticals_have_valid_shape(self):
        self.assertEqual(
            set(verticals.VERTICAL_ORDER) if False else set(verticals.VERTICAL_ORDER),
            set(verticals.VERTICALS.keys())
            if False
            else set(verticals.VERTICALS.keys()),
        )
        for key in verticals.VERTICAL_ORDER:
            preset = verticals.get_vertical(key)
            self.assertTrue(preset["label"], key)
            if key == "none":
                continue
            self.assertTrue(len(preset["script_prompt"]) > 50, key)
            self.assertIn("{term}", preset["image_template"], key)
            self.assertIn(preset["aspect"], ("16:9", "9:16", "1:1"), key)
            self.assertIn(preset["voice_mode"], ("tts", "none"), key)
            self.assertIn(preset["bgm_type"], ("random", "custom"), key)
            self.assertGreater(preset["clip_duration"], 0, key)
            self.assertGreater(preset["paragraph_number"], 0, key)

    def test_unknown_key_falls_back_to_empty_preset(self):
        preset = verticals.get_vertical("does-not-exist")
        self.assertEqual(preset["script_prompt"], "")
        applied = verticals.apply_vertical_to_session_state({}, "does-not-exist")
        self.assertEqual(applied, {"stable": {}, "notes": []})

    def test_apply_writes_widget_keys(self):
        state: dict = {}
        applied = verticals.apply_vertical_to_session_state(state, "jewelry")
        self.assertEqual(state["video_script_prompt"], verticals.JEWELRY_SCRIPT_PROMPT)
        self.assertIn("{term}", state["firefly_prompt_template_input"])
        self.assertEqual(state["paragraph_number_input"], 4)
        self.assertEqual(
            applied["stable"],
            {
                "voice_mode_control": "tts",
                "bgm_type_select": "random",
                "video_clip_duration_select": 4,
            },
        )
        self.assertTrue(any("AriaNeural" in note for note in applied["notes"]))

    def test_type_beat_has_no_voiceover_and_custom_bgm(self):
        state: dict = {}
        applied = verticals.apply_vertical_to_session_state(state, "type_beat")
        self.assertEqual(applied["stable"]["voice_mode_control"], "none")
        self.assertEqual(applied["stable"]["bgm_type_select"], "custom")
        self.assertTrue(any("beat" in note.lower() for note in applied["notes"]))

    def test_aspect_values_match_video_aspect_enum(self):
        valid = {aspect.value for aspect in VideoAspect}
        for key in verticals.VERTICAL_ORDER:
            aspect = verticals.get_vertical(key)["aspect"]
            if aspect:
                self.assertIn(aspect, valid, key)


    def test_scene_prompts_have_required_style_and_visual_constraints(self):
        for key, suffix in (
            ("diy", "photorealistic, natural window light, clean wooden workbench, shallow depth of field, 4k"),
            ("type_beat", "dark noir, cinematic, 35mm film grain, low-key lighting, deep shadows, teal and crimson neon accents"),
        ):
            prompt = verticals.get_vertical(key)["terms_prompt"]
            self.assertIn("{amount}", prompt)
            self.assertIn("twenty to thirty-five", prompt)
            self.assertIn("readable text", prompt)
            self.assertIn("faces", prompt)
            self.assertTrue(prompt.endswith(suffix))
        self.assertIn("first scene shows the finished project", verticals.DIY_TERMS_PROMPT)
        self.assertIn("last shows the finished project in use", verticals.DIY_TERMS_PROMPT)
        self.assertEqual(verticals.get_vertical("jewelry")["terms_prompt"], "")

    def test_diy_script_prompt_is_tts_safe_and_type_beat_has_lease_line(self):
        self.assertIn("numbers and units as words", verticals.DIY_SCRIPT_PROMPT)
        self.assertIn("one simple step per paragraph", verticals.DIY_SCRIPT_PROMPT)
        self.assertIn("sentence under twenty words", verticals.DIY_SCRIPT_PROMPT)
        self.assertIn("lease the beat via the link in the description", verticals.TYPE_BEAT_SCRIPT_PROMPT)
        self.assertIn("quoted lyrics", verticals.TYPE_BEAT_SCRIPT_PROMPT)


if __name__ == "__main__":
    unittest.main()
