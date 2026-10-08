"""Runtime-only integration config; never copy environment secrets into config.toml.

MoneyPrinterTurbo's WebUI persists config.app through save_config(), so Redis
passwords and TwelveLabs API keys must be read only by their consumers.
"""
from __future__ import annotations

import os


def _boolean(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return bool(default)
    if raw.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if raw.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} requires a boolean value")


def redis_settings(base: dict) -> dict:
    """Build the same config for both queue and task-state clients."""
    host = os.environ.get("MPT_REDIS_HOST") or base.get("redis_host", "localhost")
    port = int(os.environ.get("MPT_REDIS_PORT") or base.get("redis_port", 6379))
    db = int(os.environ.get("MPT_REDIS_DB") or base.get("redis_db", 0))
    if not 1 <= port <= 65535 or not 0 <= db <= 15:
        raise ValueError("Redis port or DB outside supported limits")
    return {
        "enabled": _boolean("MPT_REDIS_ENABLED", base.get("enable_redis", False)),
        "host": host,
        "port": port,
        "db": db,
        "password": os.environ.get("MPT_REDIS_PASSWORD") or base.get("redis_password") or None,
    }
