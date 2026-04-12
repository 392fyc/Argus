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

# Tail-scan limit: only read the last N bytes of a large events file.
# Events are appended chronologically, so recent events are always at the end.
# 5 MB covers ~7 days of typical Argus activity with headroom to spare.
# Applied only when `since` is provided (i.e., bounded analysis windows).
_MAX_TAIL_BYTES = 5 * 1024 * 1024  # 5 MB


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
    try:
        fh = open(path, encoding="utf-8")
    except OSError as e:
        print(f"[Argus Extractor] Cannot open sink: {e}")
        return events
    with fh:
        # Tail-scan optimisation: when a lower-bound timestamp is given and the
        # file exceeds _MAX_TAIL_BYTES, seek near the end so we skip old events
        # that are guaranteed to fall outside the analysis window.
        # Events are appended in chronological order, so recent events are at
        # the end. The first (possibly partial) line after the seek is discarded.
        if since is not None:
            try:
                file_size = os.path.getsize(path)
                if file_size > _MAX_TAIL_BYTES:
                    fh.seek(max(0, file_size - _MAX_TAIL_BYTES))
                    fh.readline()  # discard partial line at seek boundary
            except OSError:
                pass  # fall back to full scan on any seek error

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
                # Normalize caller's datetimes to UTC-aware to prevent TypeError
                _since = since.replace(tzinfo=timezone.utc) if since is not None and since.tzinfo is None else since
                _until = until.replace(tzinfo=timezone.utc) if until is not None and until.tzinfo is None else until
                try:
                    ts = datetime.fromisoformat(event.timestamp)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if _since is not None and ts < _since:
                        continue
                    if _until is not None and ts > _until:
                        continue
                except (ValueError, TypeError):
                    pass  # unparseable timestamp — include anyway

            events.append(event)

    return events
