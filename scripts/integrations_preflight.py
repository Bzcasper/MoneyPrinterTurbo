"""Prestart connectivity check for MPT's isolated Redis queue and state store."""
from __future__ import annotations

import sys

from app.config.runtime_integrations import redis_settings


def preflight() -> int:
    settings = redis_settings({})
    if settings["enabled"]:
        import redis

        client = redis.Redis(
            host=settings["host"], port=settings["port"], db=settings["db"],
            password=settings["password"], socket_connect_timeout=3,
            socket_timeout=3,
        )
        if not client.ping():
            raise RuntimeError("Dedicated Redis did not respond")
        # No output from credentials, and no destructive operations.
        print("MPT_REDIS_PREFLIGHT_OK")
    else:
        print("MPT_REDIS_DISABLED")

    from app.services import twelvelabs
    if twelvelabs.is_enabled():
        from twelvelabs import TwelveLabs  # noqa: F401
        print("MPT_TWELVELABS_SDK_READY_KEY_CONFIGURED_API_NOT_CALLED")
    else:
        print("MPT_TWELVELABS_DISABLED_NO_KEY")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(preflight())
    except Exception as exc:
        # Secrets remain in private environment files; only error class is logged.
        print(f"MPT_INTEGRATIONS_PREFLIGHT_FAILED:{type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
