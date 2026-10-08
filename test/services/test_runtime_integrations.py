"""Isolated integration settings do not leak API passwords into persisted UI config."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.config.runtime_integrations import redis_settings
from app.services import twelvelabs


def test_redis_legacy_config_without_env():
    with patch.dict("os.environ", {}, clear=True):
        result = redis_settings({
            "enable_redis": False, "redis_host": "legacy",
            "redis_port": 6379, "redis_db": 0, "redis_password": "",
        })
    assert result == {
        "enabled": False, "host": "legacy", "port": 6379,
        "db": 0, "password": None,
    }


def test_redis_env_overrides_without_mutating_webui_config():
    base = {"enable_redis": False, "redis_host": "localhost",
            "redis_password": ""}
    with patch.dict("os.environ", {
        "MPT_REDIS_ENABLED": "1",
        "MPT_REDIS_HOST": "127.0.0.1",
        "MPT_REDIS_PORT": "6381",
        "MPT_REDIS_DB": "0",
        "MPT_REDIS_PASSWORD": "private_from_file",
    }):
        selected = redis_settings(base)
    assert selected == {
        "enabled": True, "host": "127.0.0.1", "port": 6381,
        "db": 0, "password": "private_from_file",
    }
    assert base == {"enable_redis": False, "redis_host": "localhost",
                    "redis_password": ""}


@pytest.mark.parametrize("invalid", ["bad", "sometimes", "enabled"])
def test_invalid_redis_bool_fails_closed(invalid):
    with patch.dict("os.environ", {"MPT_REDIS_ENABLED": invalid}):
        with pytest.raises(ValueError, match="boolean"):
            redis_settings({})


def test_twelvelabs_key_from_private_environment_is_not_in_config_app():
    before = dict(twelvelabs.config.app)
    with patch.dict("os.environ", {
        "MPT_TWELVELABS_API_KEY": "test_only_api_key",
        "MPT_TWELVELABS_RERANK_TERMS": "1",
    }):
        assert twelvelabs.is_enabled()
        assert twelvelabs._environment_keys() == ["test_only_api_key"]
        assert twelvelabs._rerank_enabled()
    assert dict(twelvelabs.config.app) == before


def test_twelvelabs_no_key_is_a_safe_noop():
    with patch.dict("os.environ", {}, clear=True):
        with patch.dict(twelvelabs.config.app, {
            "twelvelabs_api_keys": [],
            "twelvelabs_rerank_terms": False,
        }):
            assert not twelvelabs.is_enabled()
            assert twelvelabs.embed_text("sample song") is None
            terms = ["light", "water", "street"]
            assert twelvelabs.rerank_terms_by_subject("ocean", terms) is terms
