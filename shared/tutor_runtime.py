"""Tutor answers routed through the parent gateway with MongoDB context."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any


class TutorProviderError(RuntimeError):
    pass


def _gateway_token(application_id: str | None = None) -> str:
    if application_id:
        suffix = re.sub(r"[^A-Za-z0-9]", "_", application_id).upper()
        token = os.getenv(f"MODEL_GATEWAY_TOKEN_{suffix}", "").strip()
        if token:
            return token
    return os.getenv("MODEL_GATEWAY_TOKEN", "").strip()


def active_model_status(application_id: str | None = None) -> dict[str, Any]:
    """Read the active model from the required parent gateway."""
    parent_url = os.getenv("MODEL_PARENT_URL", "").strip().rstrip("/")
    if not parent_url:
        raise TutorProviderError("MODEL_PARENT_URL is required. Tutor apps use the parent gateway only.")

    token = _gateway_token(application_id)
    if not token:
        raise TutorProviderError("MODEL_GATEWAY_TOKEN is required when MODEL_PARENT_URL is set.")
    request = urllib.request.Request(
        f"{parent_url}/api/v1/status",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            **({"X-Application-ID": application_id} if application_id else {}),
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise TutorProviderError("The parent gateway rejected MODEL_GATEWAY_TOKEN.") from None
        raise TutorProviderError(f"The parent model gateway returned HTTP {error.code}.") from None
    except (urllib.error.URLError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        raise TutorProviderError(f"The parent model gateway is unavailable: {reason}") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise TutorProviderError("The parent model gateway returned an unreadable status.") from None
    if not isinstance(result.get("model"), str) or not result["model"]:
        raise TutorProviderError("The parent model gateway returned no active model.")
    return {"configured": True, **result}


def answer_question(
    *,
    persona: dict[str, Any],
    question: str,
    student_records: list[dict[str, Any]],
) -> tuple[str, str]:
    if not student_records:
        raise TutorProviderError("MongoDB returned no records for this question.")
    parent_url = os.getenv("MODEL_PARENT_URL", "").strip().rstrip("/")
    if not parent_url:
        raise TutorProviderError("MODEL_PARENT_URL is required. Tutor apps use the parent gateway only.")
    return _call_parent(persona, question, student_records, parent_url)


def _call_parent(
    persona: dict[str, Any],
    question: str,
    student_records: list[dict[str, Any]],
    parent_url: str,
) -> tuple[str, str]:
    tutor_id = persona.get("id")
    if not isinstance(tutor_id, str) or not tutor_id:
        raise TutorProviderError("This tutor has no registered parent application ID.")
    token = _gateway_token(tutor_id)
    if not token:
        token_env = f"MODEL_GATEWAY_TOKEN_{re.sub(r'[^A-Za-z0-9]', '_', tutor_id).upper()}"
        raise TutorProviderError(f"{token_env} or MODEL_GATEWAY_TOKEN is required when MODEL_PARENT_URL is set.")
    payload = {
        "application_id": tutor_id,
        "tutor_id": tutor_id,
        "question": question,
        "student_records": student_records,
    }
    request = urllib.request.Request(
        f"{parent_url}/api/v1/respond",
        data=json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise TutorProviderError("The parent gateway rejected MODEL_GATEWAY_TOKEN.") from None
        try:
            details = json.loads(error.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            details = {}
        message = details.get("error") if isinstance(details, dict) else None
        raise TutorProviderError(message or f"The parent model gateway returned HTTP {error.code}.") from None
    except (urllib.error.URLError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        raise TutorProviderError(f"The parent model gateway is unavailable: {reason}") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise TutorProviderError("The parent model gateway returned an unreadable response.") from None
    answer = result.get("answer")
    mode = result.get("mode")
    if not isinstance(answer, str) or not answer.strip() or not isinstance(mode, str):
        raise TutorProviderError("The parent model gateway returned an incomplete answer.")
    return answer.strip(), mode


