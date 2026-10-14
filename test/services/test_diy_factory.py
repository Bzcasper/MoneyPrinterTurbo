# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path

from app.services import diy_factory, openmontage_bridge


def _structure_stub(prompt: str) -> str:
    return json.dumps(
        {
            "hook": "Build a birdhouse in one afternoon.",
            "optimized_brief": "A calm, result-first build guide.",
            "pacing_notes": "one action per scene",
            "materials_list": ["1x6 board", "handsaw", "nails"],
            "visual_bible": {"color_palette": "muted blues and warm wood tones"},
            "chapters": [
                {"chapter_number": 1, "title": "Materials", "summary": "gather", "target_seconds": 60},
                {"chapter_number": 2, "title": "Assembly", "summary": "build", "target_seconds": 90},
            ],
        }
    )


def _scene_stub(prompt: str) -> str:
    return json.dumps(
        {
            "chapter_title": "Materials",
            "scenes": [
                {
                    "scene_number": 1,
                    "narration": "Lay out your board and mark the cuts.",
                    "image_prompt": "Overhead workshop photo of a marked board",
                    "image_action": "push-in",
                    "camera_hint": "static",
                    "duration_hint_seconds": 6,
                },
                {
                    "scene_number": 2,
                    "narration": "Cut each piece with a handsaw.",
                    "image_prompt": "Close-up of a handsaw cutting wood",
                    "duration_hint_seconds": 7,
                },
            ],
        }
    )


class TestDiyFactoryPlanner(unittest.TestCase):
    def test_plan_structure_parses_outline(self):
        outline = diy_factory.plan_structure(
            topic="Build a birdhouse", generate=_structure_stub, vertical="diy"
        )
        self.assertEqual(len(outline["chapters"]), 2)
        self.assertEqual(outline["chapters"][0]["title"], "Materials")
        self.assertIn("visual_bible", outline)

    def test_plan_structure_requires_topic(self):
        with self.assertRaises(diy_factory.FactoryError):
            diy_factory.plan_structure(topic="  ", generate=_structure_stub)

    def test_plan_structure_rejects_no_json(self):
        with self.assertRaises(diy_factory.FactoryError):
            diy_factory.plan_structure(topic="x", generate=lambda p: "no json here")

    def test_plan_structure_rejects_empty_chapters(self):
        with self.assertRaises(diy_factory.FactoryError):
            diy_factory.plan_structure(
                topic="x", generate=lambda p: json.dumps({"chapters": []})
            )

    def test_write_chapter_scenes_parses(self):
        out = diy_factory.write_chapter_scenes(
            chapter={"title": "Materials"},
            chapter_index=0,
            total_chapters=2,
            outline={"hook": "h", "visual_bible": {}},
            generate=_scene_stub,
        )
        self.assertEqual(len(out["scenes"]), 2)
        self.assertEqual(out["chapter_title"], "Materials")


def _chapters():
    return [
        {
            "chapter_title": "Materials",
            "scenes": [
                {"scene_number": 1, "narration": "a", "image_prompt": "p1", "duration_hint_seconds": 6},
                {"scene_number": 2, "narration": "b", "image_prompt": "p2", "duration_hint_seconds": 7},
            ],
        },
        {
            "chapter_title": "Assembly",
            "scenes": [
                {"scene_number": 1, "narration": "c", "image_prompt": "p3"},
            ],
        },
    ]


class TestAggregateAndGate(unittest.TestCase):
    def test_aggregate_adds_title_cards_and_renumbers(self):
        scenes = diy_factory.aggregate_scenes(
            outline={"visual_bible": {"color_palette": "x"}},
            chapter_outputs=_chapters(),
            vertical="diy",
        )
        self.assertEqual(len(scenes), 5)
        self.assertEqual([s["scene_number"] for s in scenes], [1, 2, 3, 4, 5])
        self.assertTrue(scenes[0]["is_title_card"])
        self.assertIn("Materials", scenes[0]["image_prompt"])
        self.assertFalse(scenes[1]["is_title_card"])

    def test_gate_ok_for_valid_manifest(self):
        scenes = diy_factory.aggregate_scenes(
            outline={}, chapter_outputs=_chapters(), vertical="diy"
        )
        gate = diy_factory.readiness_gate(scenes=scenes, vertical="diy")
        self.assertTrue(gate["ok"], gate["errors"])

    def test_gate_flags_missing_narration(self):
        scenes = [
            {"scene_number": i, "is_title_card": False, "narration": "ok", "image_prompt": "p"}
            for i in range(1, 6)
        ]
        scenes[0]["narration"] = ""
        gate = diy_factory.readiness_gate(scenes=scenes, vertical="diy")
        self.assertFalse(gate["ok"])
        self.assertIn("missing_narration:1", gate["errors"])

    def test_gate_warns_on_placeholder_text(self):
        scenes = [
            {"scene_number": i, "is_title_card": False, "narration": "ok", "image_prompt": "p"}
            for i in range(1, 6)
        ]
        scenes[2]["narration"] = "[NEEDS CLARIFICATION: which saw?]"
        gate = diy_factory.readiness_gate(scenes=scenes, vertical="diy")
        self.assertTrue(gate["ok"])
        self.assertTrue(any(w.startswith("placeholder_text:3") for w in gate["warnings"]))

    def test_gate_too_few_scenes(self):
        gate = diy_factory.readiness_gate(
            scenes=[{"scene_number": 1, "is_title_card": True}], vertical="diy"
        )
        self.assertFalse(gate["ok"])
        self.assertIn("too_few_scenes:1", gate["errors"])


class TestProjectSpec(unittest.TestCase):
    def test_to_project_spec_uses_preset_render_settings(self):
        scenes = [
            {"scene_number": 1, "is_title_card": True, "narration": "", "image_prompt": "t"},
            {"scene_number": 2, "is_title_card": False, "narration": "hi", "image_prompt": "p",
             "duration_hint_seconds": 8},
        ]

        def resolve(scene):
            return {"footage": f"/tmp/{scene['scene_number']}.mp4", "audio": None,
                    "duration": scene.get("duration_hint_seconds", 5.0)}

        spec = diy_factory.to_project_spec(
            project_id="proj1", scenes=scenes, vertical="diy", resolve_media=resolve
        )
        self.assertEqual(spec["project_id"], "proj1")
        self.assertEqual(spec["settings"]["width"], 1920)  # DIY landscape
        self.assertEqual(spec["settings"]["height"], 1080)
        self.assertEqual(spec["scenes"][0]["id"], "s001")
        self.assertEqual(spec["scenes"][1]["duration"], 8.0)
        self.assertEqual(spec["scenes"][1]["footage"], "/tmp/2.mp4")

    def test_to_project_spec_jewelry_is_portrait(self):
        spec = diy_factory.to_project_spec(
            project_id="p",
            scenes=[{"scene_number": 1, "is_title_card": True, "narration": "", "image_prompt": "t"}],
            vertical="jewelry",
            resolve_media=lambda s: {"footage": "/tmp/a.mp4"},
        )
        self.assertEqual((spec["settings"]["width"], spec["settings"]["height"]), (1080, 1920))


class TestSaveRun(unittest.TestCase):
    def test_save_run_writes_snapshot_and_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenes = [
                {"scene_number": i, "is_title_card": False, "narration": "n", "image_prompt": "p"}
                for i in range(1, 6)
            ]
            gate = diy_factory.readiness_gate(scenes=scenes, vertical="diy")
            paths = diy_factory.save_run(
                directory=tmp, run_id="run123", topic="birdhouse", vertical="diy",
                outline={"hook": "h"}, scenes=scenes, gate=gate,
            )
            record = json.loads(Path(paths["run"]).read_text(encoding="utf-8"))
            self.assertEqual(record["scene_count"], 5)
            self.assertTrue(record["readiness_gate"]["ok"])
            log_lines = Path(paths["log"]).read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(log_lines), 1)
            diy_factory.save_run(
                directory=tmp, run_id="run456", topic="t2", vertical="diy",
                outline={}, scenes=scenes, gate=gate,
            )
            log_lines = Path(paths["log"]).read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(log_lines), 2)


class TestOpenMontageBridge(unittest.TestCase):
    def _master_scenes(self):
        scenes = []
        n = 0
        for _ in range(3):
            n += 1
            scenes.append({"scene_number": n, "is_title_card": True})
            for _ in range(4):
                n += 1
                scenes.append(
                    {"scene_number": n, "is_title_card": False, "duration_hint_seconds": 10}
                )
        return scenes

    def test_plan_shorts_groups_scenes(self):
        result = openmontage_bridge.plan_shorts(
            scenes=self._master_scenes(), vertical="diy", target_seconds=30
        )
        self.assertTrue(result["enabled"])
        self.assertEqual(result["pipeline"], "clip-factory")
        self.assertGreaterEqual(len(result["shorts"]), 1)
        for short in result["shorts"]:
            self.assertGreaterEqual(short["approx_seconds"], 30)

    def test_plan_shorts_disabled_for_preset(self):
        from app.services import verticals

        preset = verticals.get_vertical("diy")["production"]["openmontage"]
        original = preset["shorts"]
        try:
            preset["shorts"] = False
            result = openmontage_bridge.plan_shorts(
                scenes=[{"scene_number": 1, "is_title_card": False, "duration_hint_seconds": 5}],
                vertical="diy",
            )
            self.assertFalse(result["enabled"])
            self.assertEqual(result["shorts"], [])
        finally:
            preset["shorts"] = original

    def test_export_project_writes_scaffold(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenes = [
                {"scene_number": 1, "is_title_card": True, "narration": "", "image_prompt": "t"},
                {"scene_number": 2, "is_title_card": False, "narration": "hi", "image_prompt": "p"},
            ]
            paths = openmontage_bridge.export_project(
                openmontage_root=tmp,
                project_name="birdhouse-master",
                topic="birdhouse",
                vertical="diy",
                outline={"hook": "h", "chapters": [], "materials_list": []},
                scenes=scenes,
                master_video="/tmp/master.mp4",
            )
            for key in ("script", "scene_plan", "meta"):
                self.assertTrue(Path(paths[key]).exists(), key)
            meta = json.loads(Path(paths["meta"]).read_text(encoding="utf-8"))
            self.assertEqual(meta["pipeline"], "animated-explainer")
            self.assertEqual(meta["master_video"], "/tmp/master.mp4")

    def test_export_project_requires_name(self):
        with self.assertRaises(openmontage_bridge.OpenMontageError):
            openmontage_bridge.export_project(
                openmontage_root="/tmp", project_name="  ", topic="t",
                vertical="diy", outline={}, scenes=[],
            )


if __name__ == "__main__":
    unittest.main()
