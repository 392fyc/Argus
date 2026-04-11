"""
Argus structured event schema and emitter.

Writes machine-readable events to a jsonlines sink independently of the
existing print-based log, so neither the existing log format nor the
PR-Agent pipeline is affected.

Sink path: $ARGUS_EVENTS_PATH (default: /var/log/argus/events.jsonl)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class EventType(str, Enum):
    REVIEW_STARTED = "review_started"
    FINDING_POSTED = "finding_posted"
    REPLY_CLASSIFIED = "reply_classified"
    THREAD_RESOLVED = "thread_resolved"
    ERROR = "error"
    MENTION_REWRITTEN = "mention_rewritten"
    REQUEST_BLOCKED = "request_blocked"


@dataclass
class ArgusEvent:
    event_type: EventType
    timestamp: str
    pr_number: Optional[int] = None
    repo: Optional[str] = None
    severity: Optional[str] = None  # Critical | Major | Medium | Minor | None
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["event_type"] = self.event_type.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ArgusEvent":
        d = dict(d)
        d["event_type"] = EventType(d["event_type"])
        return cls(**d)


_DEFAULT_SINK = "/var/log/argus/events.jsonl"


class EventEmitter:
    """Writes ArgusEvent records as jsonlines to a configurable sink file."""

    def __init__(self, sink_path: Optional[str] = None):
        self._path = sink_path or os.environ.get("ARGUS_EVENTS_PATH", _DEFAULT_SINK)
        self._ready: Optional[bool] = None  # lazy init

    def _ensure_sink(self) -> bool:
        if self._ready is not None:
            return self._ready
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            self._ready = True
        except OSError as e:
            print(f"[Argus Events] Cannot create sink directory: {e}")
            self._ready = False
        return self._ready

    def emit(
        self,
        event_type: EventType,
        pr_number: Optional[int] = None,
        repo: Optional[str] = None,
        severity: Optional[str] = None,
        **payload_kwargs: Any,
    ) -> None:
        """Emit a structured event. Never raises — errors are print-logged only."""
        if not self._ensure_sink():
            return
        event = ArgusEvent(
            event_type=event_type,
            timestamp=datetime.now(timezone.utc).isoformat(),
            pr_number=pr_number,
            repo=repo,
            severity=severity,
            payload=payload_kwargs,
        )
        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.to_dict()) + "\n")
        except Exception as e:
            print(f"[Argus Events] Emit failed: {e}")


# Module-level singleton — imported by entrypoint-guard and patch_suggestion_format
emitter = EventEmitter()
