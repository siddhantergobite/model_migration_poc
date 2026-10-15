"""Azure OpenAI Responses API client used only by the V2 parent server."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from model_migration.langfuse_tracing import observation_scope, safe_observation_update


class AzureResponsesError(RuntimeError):
    pass


class AzureResponsesClient:
    def __init__(self, endpoint: str | None = None, api_key: str | None = None, timeout: int = 90) -> None:
        self.endpoint = (endpoint if endpoint is not None else os.getenv("AZURE_OPENAI_ENDPOINT", "")).strip().rstrip("/")
        self.api_key = (api_key if api_key is not None else os.getenv("AZURE_OPENAI_API_KEY", "")).strip()
        self.timeout = timeout

    def is_configured(self) -> bool:
        if not self.endpoint or not self.api_key:
            return False
        parsed = urlsplit(self.endpoint)
        host = (parsed.hostname or "").casefold()
        return parsed.scheme == "https" and (
            host.endswith(".openai.azure.com") or host.endswith(".services.ai.azure.com")
        )

    def complete(
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
        max_output_tokens: int = 2000,
        json_mode: bool = False,
    ) -> str:
        with observation_scope(
            name="azure.responses",
            as_type="generation",
            model=model.strip() or None,
            metadata={"provider": "azure_openai", "json_mode": bool(json_mode)},
        ) as generation:
            try:
                answer = self._complete_untraced(
                    model=model,
                    instructions=instructions,
                    input_text=input_text,
                    max_output_tokens=max_output_tokens,
                    json_mode=json_mode,
                )
            except Exception as error:
                safe_observation_update(generation, level="ERROR", status_message=type(error).__name__)
                raise
            safe_observation_update(
                generation,
                output={
                    "status": "success",
                    "answer": answer,
                    "output_characters": len(answer),
                },
            )
            return answer

    def _complete_untraced(
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
        max_output_tokens: int = 2000,
        json_mode: bool = False,
    ) -> str:
        if not self.is_configured():
            raise AzureResponsesError("Azure OpenAI is not configured. Check the Azure endpoint and API key on the parent server.")
        if not model.strip():
            raise AzureResponsesError("An Azure deployment name is required.")

        parsed = urlsplit(self.endpoint)
        base_url = self.endpoint
        if not parsed.path.rstrip("/").casefold().endswith("/openai/v1"):
            base_url = f"{base_url}/openai/v1"
        payload: dict[str, Any] = {
            "model": model.strip(),
            "instructions": instructions,
            "input": input_text,
            "reasoning": {"effort": "minimal"},
            "max_output_tokens": max_output_tokens,
            "store": False,
        }
        if json_mode:
            payload["text"] = {"format": {"type": "json_object"}}
        request = urllib.request.Request(
            f"{base_url}/responses",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "api-key": self.api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                message = f"Azure OpenAI rejected the parent server credentials or deployment access (HTTP {error.code})."
            elif error.code == 404:
                message = f"Azure deployment '{model}' or the Responses endpoint was not found (HTTP 404). Check the deployment name."
            elif error.code == 429:
                message = "Azure OpenAI quota or rate limit was reached (HTTP 429)."
            else:
                message = f"Azure OpenAI returned HTTP {error.code}."
            raise AzureResponsesError(message) from None
        except (urllib.error.URLError, TimeoutError) as error:
            reason = getattr(error, "reason", error)
            raise AzureResponsesError(f"The parent server could not reach Azure OpenAI: {reason}") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise AzureResponsesError("Azure OpenAI returned an unreadable response.") from None

        status = result.get("status")
        if status not in (None, "completed"):
            details = result.get("incomplete_details") or {}
            reason = details.get("reason") if isinstance(details, dict) else None
            if reason == "max_output_tokens":
                raise AzureResponsesError("Azure OpenAI used its response token budget before completing.")
            raise AzureResponsesError(f"Azure OpenAI response did not complete (status: {status}).")
        answer = self._response_text(result)
        if not answer:
            raise AzureResponsesError("Azure OpenAI returned no answer text.")
        return answer

    @staticmethod
    def _response_text(result: dict[str, Any]) -> str:
        parts: list[str] = []
        for item in result.get("output", []):
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            if item.get("role") not in (None, "assistant"):
                continue
            for content in item.get("content", []):
                if isinstance(content, dict) and content.get("type") in {"output_text", "text"}:
                    text = content.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
        return "\n".join(parts).strip()

