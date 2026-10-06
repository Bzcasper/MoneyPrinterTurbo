import hashlib
# -*- coding: utf-8 -*-
import io
import os
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote, quote_plus

from PIL import Image

from app.config import config
from app.services import material


def _png_bytes(width=64, height=96, color=(30, 120, 60)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _webhook_response(payload, status_code=200):
    return SimpleNamespace(json=lambda: payload, status_code=status_code)


def _download_response(content, status_code=200):
    return SimpleNamespace(
        status_code=status_code,
        content=content,
        headers={"Content-Length": str(len(content))},
        iter_content=lambda chunk_size: iter((content,)),
        close=lambda: None,
    )


class TestFireflyImageProvider(unittest.TestCase):
    """
    Adobe Firefly 文生图素材源（经 n8n webhook）。与其它素材源测试一致，
    全部用 unittest.mock 替换 requests，CI 不依赖真实网络与真实 Adobe 会话。
    """

    def setUp(self):
        self.original_app_config = dict(config.app)
        self.original_proxy_config = dict(config.proxy)
        self.save_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.save_dir, ignore_errors=True)
        config.app["firefly_webhook_url"] = (
            "http://localhost:5678/webhook/firefly-provider-generate"
        )
        config.app.pop("firefly_prompt_template", None)
        config.app.pop("firefly_webhook_token", None)
        config.app.pop("firefly_concurrency", None)
        config.app.pop("tls_verify", None)
        config.proxy.clear()

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)
        config.proxy.clear()
        config.proxy.update(self.original_proxy_config)

    def test_is_firefly_enabled(self):
        self.assertTrue(material.is_firefly_enabled())
        config.app["firefly_webhook_url"] = "   "
        self.assertFalse(material.is_firefly_enabled())
        config.app.pop("firefly_webhook_url", None)
        self.assertFalse(material.is_firefly_enabled())

    def test_generate_images_firefly_success(self):
        """webhook 返回 image_url 时必须下载落盘并记录真实尺寸。"""
        response = _webhook_response(
            {
                "provider": "firefly_direct",
                "success": True,
                "image_url": "https://cdn.example.com/ff/abc.png?sig=1",
            }
        )
        download = _download_response(_png_bytes(width=200, height=300))

        with (
            patch("app.services.material.requests.post", return_value=response) as post,
            patch("app.services.material.requests.get", return_value=download) as get,
        ):
            results = material.generate_images_firefly(
                "sunrise over mountains",
                minimum_duration=5,
                video_aspect=material.VideoAspect.portrait,
                save_dir=self.save_dir,
            )

        self.assertEqual(len(results), 1)
        item = results[0]
        self.assertEqual(item.provider, "firefly")
        self.assertEqual(item.duration, 5)
        assert post.call_args is not None
        assert get.call_args is not None
        self.assertEqual(
            post.call_args.args[0],
            "http://localhost:5678/webhook/firefly-provider-generate",
        )
        self.assertNotIn("headers", post.call_args.kwargs)
        self.assertNotIn("Authorization", get.call_args.kwargs["headers"])
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["media_type"], "image")
        self.assertEqual((payload["width"], payload["height"]), (1080, 1920))
        self.assertIn("sunrise over mountains", payload["prompt"])
        # presigned 临时 URL 原样下载，签名查询参数不能被剥离
        self.assertEqual(
            get.call_args.args[0],
            "https://cdn.example.com/ff/abc.png?sig=1",
        )
        self.assertTrue(item.url.endswith(".png"))
        self.assertIn("firefly-image-", os.path.basename(item.url))
        self.assertTrue(os.path.isfile(item.url))
        self.assertEqual(
            item.source_info["rendition"],
            {"id": None, "width": 200, "height": 300},
        )

    def test_firefly_token_is_sent_only_to_webhook(self):
        config.app["firefly_webhook_token"] = "  example-firefly-token  "
        response = _webhook_response({
            "success": True, "image_url": "https://cdn.example.com/image.png",
        })
        with (
            patch("app.services.material.requests.post", return_value=response) as post,
            patch("app.services.material.requests.get", return_value=_download_response(_png_bytes())) as get,
        ):
            content, detail = material._request_firefly_image("test scene", 64, 96)
        self.assertTrue(content)
        self.assertEqual(detail, "")
        self.assertEqual(post.call_args.kwargs["headers"], {
            "Authorization": "Bearer example-firefly-token",
        })
        self.assertNotIn("Authorization", get.call_args.kwargs["headers"])

    def test_firefly_blank_token_sends_no_auth_header(self):
        for token in ("", "   ", None):
            config.app["firefly_webhook_token"] = token
            response = _webhook_response({"success": False})
            with (
                self.subTest(token_is_none=token is None),
                patch("app.services.material.requests.post", return_value=response) as post,
                patch("app.services.material.time.sleep"),
            ):
                material._request_firefly_image("test scene", 64, 96)
            self.assertNotIn("headers", post.call_args.kwargs)

    def test_firefly_redacts_token_in_request_errors_and_response_metadata(self):
        token = "test token+/value"
        config.app["firefly_webhook_token"] = token
        variants = (token, quote_plus(token), quote(token, safe=""))
        echo = " | ".join(variants)
        responses = (
            RuntimeError(f"connection error: {echo}"),
            _webhook_response({"provider": echo, "message": echo}, 403),
            _webhook_response({"success": False, "message": echo}),
        )
        for response in responses:
            with (
                self.subTest(response_type=type(response).__name__),
                patch("app.services.material.requests.post", side_effect=response if isinstance(response, Exception) else None, return_value=response),
                patch("app.services.material.time.sleep"),
                patch("app.services.material.logger") as log,
            ):
                content, detail = material._request_firefly_image("test scene", 64, 96)
            self.assertIsNone(content)
            output = detail + " ".join(str(call.args) for call in log.mock_calls)
            for variant in variants:
                self.assertNotIn(variant, output)
            self.assertIn("***", output)

    def test_firefly_redacts_token_before_truncating_response_errors(self):
        token = "example-firefly-long-token"
        config.app["firefly_webhook_token"] = token
        response = _webhook_response({"message": "x" * 295 + token}, 403)
        with (
            patch("app.services.material.requests.post", return_value=response),
            patch("app.services.material.time.sleep"),
            patch("app.services.material.logger") as log,
        ):
            _, detail = material._request_firefly_image("test scene", 64, 96)
        output = detail + " ".join(str(call.args) for call in log.mock_calls)
        self.assertNotIn("examp", output)
        self.assertIn("***", output)

    def test_firefly_download_failure_redacts_token_and_keeps_paid_fail_fast(self):
        token = "example-firefly-token"
        config.app["firefly_webhook_token"] = token
        response = _webhook_response({"success": True, "image_url": "https://cdn.example.com/image.png"})
        with (
            patch("app.services.material.requests.post", return_value=response) as post,
            patch("app.services.material.requests.get", side_effect=RuntimeError(f"download failed {token}")),
            patch("app.services.material.time.sleep"),
            patch("app.services.material.logger") as log,
        ):
            with self.assertRaises(material.OpenAIImagePaidResultError) as error:
                material._request_firefly_image("test scene", 64, 96)
        self.assertEqual(post.call_count, 1)
        output = str(error.exception) + " ".join(str(call.args) for call in log.mock_calls)
        self.assertNotIn(token, output)

    def test_generate_images_firefly_failure_skips_term(self):
        """明确拒绝必须按素材源约定返回空列表，不能中断任务。"""
        response = _webhook_response(
            {"provider": "firefly_direct", "success": False, "reason": "nope"}
        )
        with patch("app.services.material.requests.post", return_value=response):
            results = material.generate_images_firefly(
                "sunrise over mountains",
                minimum_duration=5,
                save_dir=self.save_dir,
            )
        self.assertEqual(results, [])

    def test_generate_images_firefly_http_error_skips_term(self):
        """webhook 500（如工作流异常）必须返回空列表，不能抛错中断任务。"""
        response = _webhook_response({"message": "Error in workflow"}, 500)
        with patch("app.services.material.requests.post", return_value=response):
            results = material.generate_images_firefly(
                "sunrise over mountains",
                minimum_duration=5,
                save_dir=self.save_dir,
            )
        self.assertEqual(results, [])

    def test_firefly_prompt_template(self):
        """默认模板注入关键词；自定义模板支持 {term} 占位符。"""
        self.assertIn(
            "lonely lighthouse", material._firefly_prompt("lonely lighthouse")
        )
        config.app["firefly_prompt_template"] = "flat vector of {term}, no text"
        self.assertEqual(
            material._firefly_prompt("lonely lighthouse"),
            "flat vector of lonely lighthouse, no text",
        )

    def test_firefly_image_model_defaults_and_override(self):
        """未配置模型时用 Firefly Image 5；配置后原样透传给 n8n 链路。"""
        config.app.pop("firefly_image_model", None)
        self.assertEqual(
            material._firefly_image_model(), material.FIREFLY_IMAGE_MODEL_DEFAULT
        )
        config.app["firefly_image_model"] = "  gpt-image-1.5  "
        self.assertEqual(material._firefly_image_model(), "gpt-image-1.5")

    def test_firefly_request_sends_configured_model(self):
        """模型名必须进入 webhook 请求体，否则切换模型不会有任何效果。"""
        config.app["firefly_image_model"] = "flux-2-pro"
        response = _webhook_response(
            {
                "provider": "firefly_direct",
                "success": True,
                "model_key": "flux-2-pro",
                "credit_cost": 0,
                "fair_use": True,
                "image_url": "https://cdn.example.com/ff/x.png",
            }
        )
        download = _download_response(_png_bytes(width=64, height=64))

        with (
            patch("app.services.material.requests.post", return_value=response) as post,
            patch("app.services.material.requests.get", return_value=download),
        ):
            results = material.generate_images_firefly(
                "quiet harbour", minimum_duration=4, save_dir=self.save_dir
            )

        self.assertEqual(len(results), 1)
        assert post.call_args is not None
        self.assertEqual(post.call_args.kwargs["json"]["model"], "flux-2-pro")

    def test_firefly_log_response_summarises_billing_metadata(self):
        """日志要能直接看出"为什么被拦"，但不泄露 prompt 或凭据。"""
        summary = material._firefly_log_response(
            {
                "provider": "firefly_private_web_api",
                "model_key": "tts",
                "credit_cost": 1,
                "usage_mode": "credit_charged",
                "blocked": True,
                "error_message": "Generation blocked",
            }
        )
        self.assertIn("model_key=tts", summary)
        self.assertIn("credit_cost=1", summary)
        self.assertIn("blocked=True", summary)
        self.assertNotIn("Generation blocked", summary)
        self.assertEqual(material._firefly_log_response(None), "")

    def test_download_videos_firefly_stops_when_covered(self):
        """凑够配音时长必须立即停止，不再发起新的生成（单张耗时数分钟）。"""
        item = material.MaterialInfo()
        item.provider = "firefly"
        item.url = os.path.join(self.save_dir, "fake.png")
        item.duration = 5
        item.source_info = {"provider": "firefly"}

        with (
            patch(
                "app.services.material.generate_images_firefly",
                return_value=[item],
            ) as gen,
            patch(
                "app.services.material._render_openai_image_video",
                return_value=os.path.join(self.save_dir, "clip.mp4"),
            ),
        ):
            paths = material._download_videos_firefly_on_demand(
                task_id="test-task",
                search_terms=["one", "two", "three"],
                video_aspect=material.VideoAspect.portrait,
                audio_duration=3,
                max_clip_duration=5,
                material_directory=self.save_dir,
            )
        self.assertEqual(gen.call_count, 1)
        self.assertEqual(len(paths), 1)

    def test_download_videos_firefly_skips_non_positive_duration(self):
        """配音时长非法时直接返回，不发起任何生成。"""
        with patch("app.services.material.generate_images_firefly") as gen:
            paths = material._download_videos_firefly_on_demand(
                task_id="test-task",
                search_terms=["one"],
                video_aspect=material.VideoAspect.portrait,
                audio_duration=0,
                max_clip_duration=5,
                material_directory=self.save_dir,
            )
        gen.assert_not_called()
        self.assertEqual(paths, [])


    def _firefly_test_item(self, term):
        return material.MaterialInfo(
            provider="firefly",
            url=os.path.join(self.save_dir, f"{term}.png"),
            duration=5,
            source_info={"provider": "firefly", "search_term": term},
        )

    def _run_firefly_test_terms(self, terms, duration):
        return material._download_videos_firefly_on_demand(
            task_id="firefly-concurrency-test",
            search_terms=terms,
            video_aspect=material.VideoAspect.portrait,
            audio_duration=duration,
            max_clip_duration=5,
            material_directory=self.save_dir,
        )

    def test_firefly_concurrency_defaults_and_clamps(self):
        self.assertEqual(material._get_firefly_concurrency(), 1)
        for value, expected in ((None, 1), ("bad", 1), (float("inf"), 1), (-2, 1), (0, 1), ("3", 3), (4, 4), (99, 4)):
            with self.subTest(expected=expected):
                config.app["firefly_concurrency"] = value
                self.assertEqual(material._get_firefly_concurrency(), expected)

    def test_firefly_parallel_results_keep_script_order(self):
        config.app["firefly_concurrency"] = 3
        started = threading.Barrier(3, timeout=5)
        rendered = {term: threading.Event() for term in ("one", "two", "three")}
        completion_order = []

        def generate(search_term, **kwargs):
            started.wait()
            return [self._firefly_test_item(search_term)]

        def render(path, duration):
            term = os.path.basename(path).removesuffix(".png")
            if term == "one":
                self.assertTrue(rendered["two"].wait(5))
            elif term == "two":
                self.assertTrue(rendered["three"].wait(5))
            completion_order.append(term)
            rendered[term].set()
            return path.replace(".png", ".mp4")

        with (
            patch("app.services.material.generate_images_firefly", side_effect=generate) as gen,
            patch("app.services.material._render_openai_image_video", side_effect=render),
            patch("app.services.material._persist_material_sources") as persist,
            patch.object(material.logging_utils, "bind_log_scope", wraps=material.logging_utils.bind_log_scope) as bind,
        ):
            paths = self._run_firefly_test_terms(["one", "two", "three", "unused"], 11)
        self.assertEqual(completion_order, ["three", "two", "one"])
        self.assertEqual([os.path.basename(path) for path in paths], ["one.mp4", "two.mp4", "three.mp4"])
        self.assertEqual(gen.call_count, 3)
        self.assertEqual(bind.call_count, 3)
        self.assertEqual([record["search_term"] for record in persist.call_args.args[1]], ["one", "two", "three"])

    def test_firefly_parallel_uses_next_unused_terms_in_follow_up_wave(self):
        config.app["firefly_concurrency"] = 2
        first_wave = set()
        lock = threading.Lock()

        def generate(search_term, **kwargs):
            with lock:
                if search_term == "four":
                    self.assertEqual(first_wave, {"one", "two", "three"})
                else:
                    first_wave.add(search_term)
            return [] if search_term == "one" else [self._firefly_test_item(search_term)]

        with (
            patch("app.services.material.generate_images_firefly", side_effect=generate) as gen,
            patch("app.services.material._render_openai_image_video", side_effect=lambda path, duration: path.replace(".png", ".mp4")),
            patch("app.services.material._persist_material_sources"),
        ):
            paths = self._run_firefly_test_terms(["one", "two", "three", "four", "unused"], 11)
        self.assertEqual([os.path.basename(path) for path in paths], ["two.mp4", "three.mp4", "four.mp4"])
        self.assertEqual(sorted(call.kwargs["search_term"] for call in gen.call_args_list), ["four", "one", "three", "two"])

    def test_firefly_parallel_paid_failure_cancels_pending_without_waiting(self):
        config.app["firefly_concurrency"] = 2
        second_started = threading.Event()
        release_second = threading.Event()
        requested = []
        pool = ThreadPoolExecutor(max_workers=2)

        def generate(search_term, **kwargs):
            requested.append(search_term)
            if search_term == "one":
                self.assertTrue(second_started.wait(5))
                raise material.OpenAIImagePaidResultError("paid result unavailable")
            if search_term == "two":
                second_started.set()
                self.assertTrue(release_second.wait(10))
            return [self._firefly_test_item(search_term)]

        with (
            ThreadPoolExecutor(max_workers=1) as caller,
            patch("app.services.material.ThreadPoolExecutor", return_value=pool),
            patch.object(pool, "shutdown", wraps=pool.shutdown) as shutdown,
            patch("app.services.material.generate_images_firefly", side_effect=generate),
            patch("app.services.material._render_openai_image_video", side_effect=lambda path, duration: path.replace(".png", ".mp4")),
            patch("app.services.material._persist_material_sources") as persist,
        ):
            future = caller.submit(self._run_firefly_test_terms, ["one", "two", "unused-three", "unused-four"], 20)
            try:
                with self.assertRaises(material.OpenAIImagePaidResultError):
                    future.result(timeout=5)
                persist.assert_called_once_with("firefly-concurrency-test", [])
                self.assertTrue(any(call.kwargs == {"wait": False, "cancel_futures": True} for call in shutdown.call_args_list))
            finally:
                release_second.set()
                pool.shutdown(wait=True, cancel_futures=True)
        self.assertEqual(sorted(requested), ["one", "two"])

    def test_firefly_parallel_paid_failure_persists_completed_sources(self):
        config.app["firefly_concurrency"] = 2
        second_started = threading.Event()
        first_completed = threading.Event()
        pool = ThreadPoolExecutor(max_workers=2)
        real_submit = pool.submit

        def submit(*args, **kwargs):
            future = real_submit(*args, **kwargs)
            if args[1] == 0:
                future.add_done_callback(lambda done: first_completed.set())
            return future

        def generate(search_term, **kwargs):
            if search_term == "one":
                self.assertTrue(second_started.wait(5))
                return [self._firefly_test_item(search_term)]
            second_started.set()
            self.assertTrue(first_completed.wait(5))
            raise material.OpenAIImagePaidResultError("paid result unavailable")

        with (
            patch("app.services.material.ThreadPoolExecutor", return_value=pool),
            patch.object(pool, "submit", side_effect=submit),
            patch("app.services.material.generate_images_firefly", side_effect=generate),
            patch("app.services.material._render_openai_image_video", side_effect=lambda path, duration: path.replace(".png", ".mp4")),
            patch("app.services.material._persist_material_sources") as persist,
        ):
            try:
                with self.assertRaises(material.OpenAIImagePaidResultError):
                    self._run_firefly_test_terms(["one", "two"], 10)
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
        self.assertEqual([record["search_term"] for record in persist.call_args.args[1]], ["one"])

    def test_firefly_concurrency_one_keeps_serial_fallback_and_stop(self):
        config.app["firefly_concurrency"] = 1

        def generate(search_term, **kwargs):
            return [] if search_term == "one" else [self._firefly_test_item(search_term)]

        with (
            patch("app.services.material.ThreadPoolExecutor") as executor,
            patch("app.services.material.generate_images_firefly", side_effect=generate) as gen,
            patch("app.services.material._render_openai_image_video", side_effect=lambda path, duration: path.replace(".png", ".mp4")),
            patch("app.services.material._persist_material_sources"),
        ):
            paths = self._run_firefly_test_terms(["one", "two", "three", "unused"], 6)
        executor.assert_not_called()
        self.assertEqual([call.kwargs["search_term"] for call in gen.call_args_list], ["one", "two", "three"])
        self.assertEqual([os.path.basename(path) for path in paths], ["two.mp4", "three.mp4"])

    def test_firefly_nonfinite_duration_never_starts_requests(self):
        config.app["firefly_concurrency"] = 4
        for duration in (float("nan"), float("inf"), float("-inf")):
            with (
                self.subTest(duration_is_finite=False),
                patch("app.services.material.generate_images_firefly") as gen,
                patch("app.services.material._persist_material_sources"),
            ):
                self.assertEqual(self._run_firefly_test_terms(["one"], duration), [])
            gen.assert_not_called()


    def test_firefly_full_scene_is_sent_unchanged_and_keywords_use_template(self):
        scene = "Hands fitting a wooden shelf, soft window light, no text"
        config.app["firefly_prompt_template"] = "workshop photograph of {term}"
        for is_scene, expected in ((True, scene), (False, f"workshop photograph of {scene}")):
            with (
                self.subTest(is_scene=is_scene),
                patch("app.services.material.requests.post", return_value=_webhook_response({"success": True, "image_url": "https://image.test/result"})) as post,
                patch("app.services.material.requests.get", return_value=_download_response(_png_bytes())),
            ):
                images = material.generate_images_firefly(scene, 5, save_dir=self.save_dir, scene_prompt=is_scene)
            self.assertEqual(len(images), 1)
            self.assertEqual(post.call_args.kwargs["json"]["prompt"], expected)

    def test_firefly_scene_bypass_reaches_serial_and_parallel_workers(self):
        for concurrency in (1, 2):
            for is_scene in (False, True):
                config.app["firefly_concurrency"] = concurrency
                with (
                    self.subTest(concurrency=concurrency, is_scene=is_scene),
                    patch("app.services.material.generate_images_firefly", side_effect=lambda search_term, **kwargs: [self._firefly_test_item(search_term)]) as generate,
                    patch("app.services.material._render_openai_image_video", side_effect=lambda file, duration: file.replace(".png", ".mp4")),
                    patch("app.services.material._persist_material_sources"),
                ):
                    paths = material._download_videos_firefly_on_demand(
                        task_id="scene-forwarding", search_terms=["one", "two", "unused"],
                        video_aspect=material.VideoAspect.portrait, audio_duration=10,
                        max_clip_duration=5, material_directory=self.save_dir, scene_prompts=is_scene,
                    )
                self.assertEqual([os.path.basename(file) for file in paths], ["one.mp4", "two.mp4"])
                self.assertEqual(generate.call_count, 2)
                for call in generate.call_args_list:
                    if is_scene:
                        self.assertTrue(call.kwargs["scene_prompt"])
                    else:
                        self.assertNotIn("scene_prompt", call.kwargs)

    def test_firefly_scene_mode_is_forwarded_from_public_material_entry(self):
        config.app["material_directory"] = self.save_dir
        for is_scene in (False, True):
            with self.subTest(is_scene=is_scene), patch("app.services.material._download_videos_firefly_on_demand", return_value=["clip.mp4"]) as download:
                self.assertEqual(material.download_videos("scene-test", ["scene"], source="firefly", audio_duration=5, firefly_scene_prompts=is_scene), ["clip.mp4"])
            if is_scene:
                self.assertTrue(download.call_args.kwargs["scene_prompts"])
            else:
                self.assertNotIn("scene_prompts", download.call_args.kwargs)


    def _firefly_cache_task_dir(self, task_id):
        directory = os.path.join(self.save_dir, "tasks", task_id)
        os.makedirs(directory, exist_ok=True)
        return directory

    def _run_cached_firefly(self, task_id, terms=None, duration=5):
        return material._download_videos_firefly_on_demand(
            task_id=task_id, search_terms=terms or ["wooden shelf"],
            video_aspect=material.VideoAspect.portrait, audio_duration=duration,
            max_clip_duration=5, material_directory=self.save_dir,
        )

    def test_firefly_retry_reuses_saved_png_without_another_post(self):
        with (
            patch.object(material.utils, "task_dir", side_effect=self._firefly_cache_task_dir),
            patch("app.services.material.requests.post", return_value=_webhook_response({"success": True, "image_url": "https://image.test/result"})) as post,
            patch("app.services.material.requests.get", return_value=_download_response(_png_bytes())),
            patch("app.services.material._render_openai_image_video", side_effect=lambda file, duration: file + ".mp4"),
            patch("app.services.material._persist_material_sources"),
        ):
            first = self._run_cached_firefly("retry-task")
            second = self._run_cached_firefly("retry-task")
            manifest = material.task_artifacts.read_firefly_image_manifest("retry-task")
        self.assertEqual(first, second)
        self.assertEqual(post.call_count, 1)
        request = post.call_args.kwargs["json"]
        expected_key = hashlib.sha256(f'{request["prompt"]}|{request["model"]}|{request["width"]}x{request["height"]}'.encode("utf-8")).hexdigest()
        self.assertEqual(set(manifest), {expected_key})
        self.assertTrue(os.path.isfile(manifest[expected_key]))

    def test_firefly_cache_is_not_shared_between_tasks(self):
        with (
            patch.object(material.utils, "task_dir", side_effect=self._firefly_cache_task_dir),
            patch("app.services.material.requests.post", return_value=_webhook_response({"success": True, "image_url": "https://image.test/result"})) as post,
            patch("app.services.material.requests.get", return_value=_download_response(_png_bytes())),
            patch("app.services.material._render_openai_image_video", side_effect=lambda file, duration: file + ".mp4"),
            patch("app.services.material._persist_material_sources"),
        ):
            first = self._run_cached_firefly("task-one")
            second = self._run_cached_firefly("task-two")
        self.assertNotEqual(first, second)
        self.assertEqual(post.call_count, 2)

    def test_firefly_retry_regenerates_missing_or_corrupt_png(self):
        for missing in (False, True):
            task_id = f"invalid-cache-{missing}"
            with (
                self.subTest(missing=missing),
                patch.object(material.utils, "task_dir", side_effect=self._firefly_cache_task_dir),
                patch("app.services.material.requests.post", return_value=_webhook_response({"success": True, "image_url": "https://image.test/result"})) as post,
                patch("app.services.material.requests.get", return_value=_download_response(_png_bytes())),
                patch("app.services.material._render_openai_image_video", side_effect=lambda file, duration: file + ".mp4"),
                patch("app.services.material._persist_material_sources"),
            ):
                first = self._run_cached_firefly(task_id)
                image_path = next(iter(material.task_artifacts.read_firefly_image_manifest(task_id).values()))
                if missing:
                    os.unlink(image_path)
                else:
                    with open(image_path, "wb") as image_file:
                        image_file.write(b"not a PNG")
                second = self._run_cached_firefly(task_id)
                cached = next(iter(material.task_artifacts.read_firefly_image_manifest(task_id).values()))
            self.assertNotEqual(first, second)
            self.assertNotEqual(image_path, cached)
            self.assertEqual(post.call_count, 2)
            self.assertIsNotNone(material._firefly_retry_image(cached))

    def test_firefly_retry_key_changes_with_prompt_model_and_dimensions(self):
        with (
            patch.object(material.utils, "task_dir", side_effect=self._firefly_cache_task_dir),
            patch("app.services.material.requests.post", return_value=_webhook_response({"success": True, "image_url": "https://image.test/result"})) as post,
            patch("app.services.material.requests.get", return_value=_download_response(_png_bytes())),
        ):
            material.generate_images_firefly("shelf", 5, save_dir=self.save_dir, task_id="cache-key")
            material.generate_images_firefly("shelf", 5, save_dir=self.save_dir, task_id="cache-key")
            config.app["firefly_image_model"] = "flux-2-pro"
            material.generate_images_firefly("shelf", 5, save_dir=self.save_dir, task_id="cache-key")
            material.generate_images_firefly("shelf", 5, video_aspect=material.VideoAspect.landscape, save_dir=self.save_dir, task_id="cache-key")
            material.generate_images_firefly("different shelf", 5, save_dir=self.save_dir, task_id="cache-key")
            manifest = material.task_artifacts.read_firefly_image_manifest("cache-key")
        self.assertEqual(post.call_count, 4)
        self.assertEqual(len(manifest), 4)

    def test_firefly_first_attempt_does_not_reuse_fresh_png_for_duplicate_terms(self):
        for concurrency in (1, 2):
            config.app["firefly_concurrency"] = concurrency
            with (
                self.subTest(concurrency=concurrency),
                patch.object(material.utils, "task_dir", side_effect=self._firefly_cache_task_dir),
                patch("app.services.material.requests.post", return_value=_webhook_response({"success": True, "image_url": "https://image.test/result"})) as post,
                patch("app.services.material.requests.get", return_value=_download_response(_png_bytes())),
                patch("app.services.material._render_openai_image_video", side_effect=lambda file, duration: file + ".mp4"),
                patch("app.services.material._persist_material_sources"),
            ):
                paths = self._run_cached_firefly(f"fresh-duplicates-{concurrency}", ["same term", "same term"], 10)
            self.assertEqual(post.call_count, 2)
            self.assertEqual(len(set(paths)), 2)

    def test_firefly_png_is_cached_before_local_render_failure(self):
        with (
            patch.object(material.utils, "task_dir", side_effect=self._firefly_cache_task_dir),
            patch("app.services.material.requests.post", return_value=_webhook_response({"success": True, "image_url": "https://image.test/result"})) as post,
            patch("app.services.material.requests.get", return_value=_download_response(_png_bytes())),
            patch("app.services.material._render_openai_image_video", side_effect=[None, "recovered.mp4"]),
            patch("app.services.material._persist_material_sources"),
        ):
            with self.assertRaises(material.OpenAIImagePaidResultError):
                self._run_cached_firefly("render-retry")
            self.assertEqual(self._run_cached_firefly("render-retry"), ["recovered.mp4"])
        self.assertEqual(post.call_count, 1)

    def test_firefly_manifest_write_failure_stops_further_generation(self):
        with (
            patch.object(material.utils, "task_dir", side_effect=self._firefly_cache_task_dir),
            patch("app.services.material.requests.post", return_value=_webhook_response({"success": True, "image_url": "https://image.test/result"})) as post,
            patch("app.services.material.requests.get", return_value=_download_response(_png_bytes())),
            patch.object(material.task_artifacts, "record_firefly_image", side_effect=OSError("disk unavailable")),
            patch("app.services.material._persist_material_sources") as persist,
        ):
            with self.assertRaisesRegex(material.OpenAIImagePaidResultError, "manifest could not be persisted"):
                self._run_cached_firefly("cache-write-error", ["first", "must not generate"], 10)
        self.assertEqual(post.call_count, 1)
        persist.assert_called_once_with("cache-write-error", [])
        self.assertEqual(len([name for name in os.listdir(self.save_dir) if name.endswith(".png")]), 1)

if __name__ == "__main__":
    unittest.main()
