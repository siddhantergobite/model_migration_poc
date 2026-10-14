"""Persistent registry for client application contracts and credentials."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any


class ApplicationRegistryError(RuntimeError):
    pass


class ApplicationRegistry:
    """Small local-POC registry; credential values are stored as SHA-256 hashes."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ApplicationRegistryError(f"Could not load the client application registry: {error}") from error
        if not isinstance(value, dict) or not isinstance(value.get("applications"), list):
            raise ApplicationRegistryError("The client application registry has an invalid structure.")
        return value

    def list_applications(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._read().get("applications", []))

    def get_application(self, application_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = next(
                (entry for entry in self._read().get("applications", [])
                 if entry.get("application_id") == application_id),
                None,
            )
            return copy.deepcopy(item) if item else None

    def _write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=self.path.parent,
                prefix=f"{self.path.name}.", suffix=".tmp", delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)

    def register(self, application: dict[str, Any]) -> dict[str, Any]:
        application_id = application.get("application_id")
        if not isinstance(application_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}", application_id):
            raise ApplicationRegistryError("Application IDs must be 2-64 letters, numbers, underscores, or hyphens.")
        with self._lock:
            state = self._read()
            applications = state.get("applications", [])
            if any(item.get("application_id") == application_id for item in applications):
                raise ApplicationRegistryError("That application ID is already registered.")
            state["schema_version"] = max(2, int(state.get("schema_version", 1)))
            applications.append(copy.deepcopy(application))
            state["applications"] = applications
            self._write(state)
            return copy.deepcopy(application)

