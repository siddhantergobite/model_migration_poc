"""Optional, privacy-safe Langfuse instrumentation for the parent gateway."""

from __future__ import annotations

import importlib.util
import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator


logger = logging.getLogger(__name__)
_client: Any | None = None


def langfuse_status() -> dict[str, Any]:
    """Return configuration state without exposing credentials or checking the network."""
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    sdk_available = importlib.util.find_spec("langfuse") is not None
    configured = bool(public_key and secret_key and os.getenv("LANGFUSE_BASE_URL", "").strip())
    explicitly_disabled = os.getenv("LANGFUSE_TRACING_ENABLED", "true").strip().casefold() in {
        "0", "false", "no", "off",
    }
    return {
        "configured": configured,
        "sdk_available": sdk_available,
        "enabled": configured and sdk_available and not explicitly_disabled,
        "base_url": os.getenv("LANGFUSE_BASE_URL", "").strip() or None,
    }


def _get_client() -> Any:
    global _client
    if _client is None:
        from langfuse import get_client

        _client = get_client()
    return _client


def check_langfuse_connection() -> bool:
    """Check the configured project credentials against the self-hosted service."""
    if not langfuse_status()["enabled"]:
        return False
    try:
        return bool(_get_client().auth_check())
    except Exception:
        logger.exception("Langfuse authentication check failed.")
        return False


def safe_observation_update(observation: Any | None, **values: Any) -> None:
    """Keep observability SDK failures from affecting a tutor response."""
    if observation is None:
        return
    try:
        observation.update(**values)
    except Exception:
        logger.exception("Could not update Langfuse observation; continuing without tracing.")


@contextmanager
def observation_scope(
    *,
    name: str,
    as_type: str = "span",
    model: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[Any | None]:
    """Create a Langfuse observation when configured; tracing failures stay non-fatal."""
    if not langfuse_status()["enabled"]:
        yield None
        return

    try:
        observation_context = _get_client().start_as_current_observation(
            as_type=as_type,
            name=name,
            model=model,
            metadata=metadata or {},
        )
        observation = observation_context.__enter__()
    except Exception:
        logger.exception("Could not start Langfuse observation; continuing without tracing.")
        yield None
        return

    try:
        yield observation
    except BaseException as error:
        safe_observation_update(observation, level="ERROR", status_message=type(error).__name__)
        try:
            observation_context.__exit__(type(error), error, error.__traceback__)
        except Exception:
            logger.debug("Could not close the failed Langfuse observation.", exc_info=True)
        raise
    else:
        try:
            observation_context.__exit__(None, None, None)
        except Exception:
            logger.exception("Could not close Langfuse observation; continuing without tracing.")

