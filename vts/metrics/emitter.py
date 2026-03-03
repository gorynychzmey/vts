"""Thread-safe JSONL metrics emitter."""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class MetricsEmitter:
    """Writes one JSON line per event to a JSONL file and logs it via Python logging.

    Thread-safe: file writes are serialized via a threading.Lock.
    All events are accumulated in-memory for final aggregation.
    """

    def __init__(
        self,
        *,
        task_id: str,
        run_id: str,
        jsonl_path: Path | None = None,
        enabled: bool = True,
        prompt_version: str = "",
    ) -> None:
        self.task_id = task_id
        self.run_id = run_id
        self.prompt_version = prompt_version
        self._path = jsonl_path
        self._enabled = enabled
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        """Emit a metrics event.  Always includes ts/task_id/run_id/prompt_version."""
        if not self._enabled:
            return
        full: dict[str, Any] = {
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "task_id": self.task_id,
            "run_id": self.run_id,
            "prompt_version": event.pop("prompt_version", self.prompt_version),
        }
        full.update(event)
        self._events.append(full)
        line = json.dumps(full, ensure_ascii=False, separators=(",", ":"))
        log.info("metrics %s", line)
        if self._path is not None:
            try:
                with self._lock:
                    self._path.parent.mkdir(parents=True, exist_ok=True)
                    with self._path.open("a", encoding="utf-8") as f:
                        f.write(line + "\n")
            except OSError as exc:
                log.warning("metrics: failed to write to %s: %s", self._path, exc)

    def all_events(self) -> list[dict[str, Any]]:
        return list(self._events)
