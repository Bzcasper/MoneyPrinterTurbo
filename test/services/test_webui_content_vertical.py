# -*- coding: utf-8 -*-
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import streamlit as st
from streamlit.testing.v1 import AppTest

from app.config import config
from app.services import verticals, voice

ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"


class _GroupedSelectHarness:
    """与 test_webui_grouped_select 相同的组件桩，仅控制视频来源选择。"""

    def __init__(self):
        self.selected = None
        self.calls = []
        self.declaration = None
        self._original_component = st.components.v2.component

    def declare(self, name, *args, **kwargs):
        if name != "mpt_grouped_select":
            return self._original_component(name, *args, **kwargs)

        self.declaration = kwargs

        def render(**render_kwargs):
            self.calls.append(render_kwargs)
            return SimpleNamespace(selected=self.selected)

        return render


@contextmanager
def _running_app(harness):
    test_app_config = dict(
        config.app,
        video_source="firefly",
        firefly_webhook_url="http://nuc.test/webhook",
        firefly_webhook_token="",
        material_concurrency=1,
        video_clip_concurrency=1,
    )
    test_ui_config = dict(config.ui, language="en")
    with (
        patch(
            "streamlit.components.v2.component",
            side_effect=harness.declare,
        ),
        patch.object(config, "app", test_app_config),
        patch.object(config, "ui", test_ui_config),
        patch.object(config, "try_save_config", return_value=True),
        patch.object(
            voice,
            "get_all_azure_voices",
            return_value=["en-US-JennyNeural-Female"],
        ),
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        app.session_state["ui_language"] = "en"
        app.run()
        assert [str(item.value) for item in app.exception] == []
        yield app


def _select_vertical(app, key):
    box = next(
        item
        for item in app.selectbox
        if item.key.startswith("content_vertical_select_")
    )
    box.set_value(key).run()
    assert [str(item.value) for item in app.exception] == []


def test_jewelry_preset_fills_script_prompt_and_production_settings():
    """选择珠宝垂类后，脚本提示词与生产设置必须一次到位。"""
    with _running_app(_GroupedSelectHarness()) as app:
        _select_vertical(app, "jewelry")

        assert (
            app.session_state["video_script_prompt"] == verticals.JEWELRY_SCRIPT_PROMPT
        )
        assert (
            app.session_state["firefly_prompt_template_input"]
            == verticals.JEWELRY_IMAGE_TEMPLATE
        )
        assert app.session_state["paragraph_number_input"] == 4
        assert app.session_state["voice_mode_control_en"] == "tts"
        assert app.session_state["bgm_type_select_en"] == "random"
        assert app.session_state["video_clip_duration_select_en"] == 4
        assert app.session_state["video_aspect_for_firefly_en"] == "9:16"
        notes = app.session_state["content_vertical_notes"]
        assert any("AriaNeural" in note for note in notes)


def test_type_beat_preset_disables_voiceover_and_keeps_portrait():
    """Type Beat 垂类必须关闭配音、走自定义 BGM，并保持竖屏。"""
    with _running_app(_GroupedSelectHarness()) as app:
        _select_vertical(app, "type_beat")

        assert app.session_state["voice_mode_control_en"] == "none"
        assert app.session_state["bgm_type_select_en"] == "custom"
        assert app.session_state["video_aspect_for_firefly_en"] == "9:16"
        notes = app.session_state["content_vertical_notes"]
        assert any("beat" in note.lower() for note in notes)


def test_diy_preset_sets_landscape_and_tts():
    """DIY 垂类走横屏 YouTube 格式并保留配音。"""
    with _running_app(_GroupedSelectHarness()) as app:
        _select_vertical(app, "diy")

        assert app.session_state["voice_mode_control_en"] == "tts"
        assert app.session_state["video_aspect_for_firefly_en"] == "16:9"
        assert app.session_state["paragraph_number_input"] == 5


def test_firefly_source_passes_generate_whitelist():
    """视频来源白名单必须包含 firefly，否则生成会被“无效来源”拦截。"""
    source = "webui/Main.py"
    text = (ROOT_DIR / source).read_text()
    whitelist_block = text.split("params.video_source not in [", 1)[1]
    whitelist = whitelist_block.split("]", 1)[0]
    assert '"firefly"' in whitelist


def test_firefly_webhook_token_setting_is_password_and_persists():
    with _running_app(_GroupedSelectHarness()) as app:
        app.session_state["settings_dialog_open"] = True
        app.session_state["settings_dialog_target_tab"] = "material"
        app.run()
        assert [str(item.value) for item in app.exception] == []
        field = next(item for item in app.text_input if item.key == "firefly_webhook_token_input")
        assert field.proto.type == field.proto.PASSWORD
        field.set_value("  example-webhook-token  ").run()
        assert config.app["firefly_webhook_token"] == "example-webhook-token"
        assert [str(item.value) for item in app.exception] == []
