# -*- coding: utf-8 -*-
import io
import os
import shutil
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
