from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models.schema import VideoParams
from app.services import task_artifacts


class TestTaskArtifacts(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.task_dir = Path(self.temp_dir.name)
        self.task_dir_patch = patch(
            "app.services.task_artifacts.utils.task_dir",
            return_value=str(self.task_dir),
        )
        self.task_dir_patch.start()

    def tearDown(self):
        self.task_dir_patch.stop()
        self.temp_dir.cleanup()

    def test_patch_preserves_existing_script_fields(self):
        """补充素材来源时不能覆盖历史任务恢复依赖的文案、关键词和参数。"""
        original = {
            "script": "existing script",
            "search_terms": ["nature"],
            "params": {"video_source": "pixabay"},
        }
        task_artifacts.write_script_data("task-1", original)

        updated = task_artifacts.patch_script_data(
            "task-1",
            material_sources=[
                {
                    "provider": "pixabay",
                    "asset_id": "123",
                    "local_file": "vid-123.mp4",
                }
            ],
        )
        payload = json.loads((self.task_dir / "script.json").read_text())

        self.assertTrue(updated)
        self.assertEqual(payload["script"], original["script"])
        self.assertEqual(payload["search_terms"], original["search_terms"])
        self.assertEqual(payload["params"], original["params"])
        self.assertEqual(payload["material_sources"][0]["asset_id"], "123")
        self.assertEqual(list(self.task_dir.glob(".script.json.*.tmp")), [])

    def test_write_script_data_serializes_video_params(self):
        """原子写入替换旧实现后，仍需完整兼容任务主流程传入的 Pydantic 参数。"""
        params = VideoParams(
            video_subject="test subject",
            video_terms=["city", "night"],
        )

        task_artifacts.write_script_data(
            "task-params",
            {
                "script": "test script",
                "search_terms": ["city"],
                "params": params,
            },
        )
        payload = json.loads((self.task_dir / "script.json").read_text())

        self.assertEqual(payload["params"]["video_subject"], "test subject")
        self.assertEqual(payload["params"]["video_terms"], ["city", "night"])
        self.assertEqual(payload["params"]["video_source"], "pexels")

    def test_patch_missing_script_is_non_blocking(self):
        """独立调用素材下载时没有任务清单，应静默跳过而不是创建残缺 JSON。"""
        updated = task_artifacts.patch_script_data(
            "standalone",
            material_sources=[],
        )

        self.assertFalse(updated)
        self.assertFalse((self.task_dir / "script.json").exists())

    def test_patch_invalid_script_returns_false_without_overwrite(self):
        """历史 JSON 损坏时必须保留原文件、记录错误，并允许视频主流程继续。"""
        target = self.task_dir / "script.json"
        target.write_text("{invalid-json", encoding="utf-8")

        with patch.object(task_artifacts.logger, "warning") as warning:
            updated = task_artifacts.patch_script_data(
                "task-1",
                material_sources=[],
            )

        self.assertFalse(updated)
        self.assertEqual(target.read_text(encoding="utf-8"), "{invalid-json")
        self.assertTrue(warning.called)


    def test_firefly_manifest_is_bound_to_task_and_rejects_invalid_json(self):
        key = hashlib.sha256(b"scene|model|64x96").hexdigest()
        self.assertEqual(task_artifacts.read_firefly_image_manifest("task-one"), {})
        task_artifacts.record_firefly_image("task-one", key, str(self.task_dir / "image.png"))
        self.assertEqual(set(task_artifacts.read_firefly_image_manifest("task-one")), {key})
        self.assertEqual(task_artifacts.read_firefly_image_manifest("task-two"), {})
        (self.task_dir / "firefly-images.json").write_text("{invalid-json", encoding="utf-8")
        self.assertEqual(task_artifacts.read_firefly_image_manifest("task-one"), {})

    def test_concurrent_firefly_manifest_updates_preserve_all_saved_images(self):
        keys = [hashlib.sha256(f"scene-{index}".encode()).hexdigest() for index in range(20)]
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda key: task_artifacts.record_firefly_image("task-one", key, str(self.task_dir / f"{key}.png")), keys))
        self.assertEqual(set(task_artifacts.read_firefly_image_manifest("task-one")), set(keys))
        self.assertEqual(list(self.task_dir.glob(".firefly-images.json.*.tmp")), [])

    def test_failed_firefly_manifest_replace_keeps_previous_cache(self):
        first_key = hashlib.sha256(b"first scene").hexdigest()
        second_key = hashlib.sha256(b"second scene").hexdigest()
        task_artifacts.record_firefly_image("task-one", first_key, str(self.task_dir / "first.png"))
        with patch.object(task_artifacts.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                task_artifacts.record_firefly_image("task-one", second_key, str(self.task_dir / "second.png"))
        self.assertEqual(set(task_artifacts.read_firefly_image_manifest("task-one")), {first_key})
        self.assertEqual(list(self.task_dir.glob(".firefly-images.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
