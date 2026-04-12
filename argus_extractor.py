"""
Argus event extractor.

Reads events from the jsonlines sink and returns a filtered list of
ArgusEvent objects. Supports optional time-range and event-type filtering.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import List, Optional

from argus_events import ArgusEvent, EventType

_DEFAULT_SINK = "/var/log/argus/events.jsonl"


def read_events(
    sink_path: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    event_types: Optional[List[EventType]] = None,
) -> List[ArgusEvent]:
    """Read events from the jsonlines sink.

    Args:
        sink_path: Path to the events.jsonl file. Defaults to
            $ARGUS_EVENTS_PATH or /var/log/argus/events.jsonl.
        since: Include only events at or after this timestamp (UTC-aware).
        until: Include only events at or before this timestamp (UTC-aware).
        event_types: Whitelist of EventType values to include. None = all.

    Returns:
        List of ArgusEvent objects matching the filters, in file order.
    """
    path = sink_path or os.environ.get("ARGUS_EVENTS_PATH", _DEFAULT_SINK)
    if not os.path.exists(path):
        return []

    events: List[ArgusEvent] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                event = ArgusEvent.from_dict(raw)
            except Exception as e:
                print(f"[Argus Extractor] Skipping malformed line {lineno}: {e}")
                continue

            if event_types is not None and event.event_type not in event_types:
                continue

            if since is not None or until is not None:
                try:
                    ts = datetime.fromisoformat(event.timestamp)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if since is not None and ts < since:
                        continue
                    if until is not None and ts > until:
                        continue
                except (ValueError, TypeError):
                    pass  # unparseable or tz-incompatible timestamp — include anyway

            events.append(event)

    return events
