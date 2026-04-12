"""
Argus log analyzer.

Reads structured events from the M2 schema and groups them into ProblemCluster
objects. Each cluster carries observed facts (evidence) and an UNVERIFIED
hypothesis suitable for filing as a GitHub Issue.

Usage::

    from datetime import datetime, timezone, timedelta
    from argus_log_analyzer import analyze

    since = datetime.now(timezone.utc) - timedelta(days=3)
    clusters = analyze(since=since)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from argus_events import EventType
from argus_extractor import read_events


# ── Problem severity ──────────────────────────────────────────────────────────

SEVERITY_CRITICAL = "critical"
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"


# ── Problem types ─────────────────────────────────────────────────────────────

TYPE_ERROR_SPIKE = "error_spike"
TYPE_ESCALATE_OVERRATE = "escalate_overrate"
TYPE_RESOLUTION_GAP = "resolution_gap"
TYPE_ACCESS_PATTERN = "access_pattern"
TYPE_DATA_GAP = "data_gap"


@dataclass
class ProblemCluster:
    """One distinct problem detected in the analysis window."""

    type: str
    severity: str
    title: str
    evidence: List[str] = field(default_factory=list)  # observed facts
    hypothesis: str = ""  # UNVERIFIED hypothesis
    signature: str = ""  # sha256 used for deduplication

    def __post_init__(self) -> None:
        if not self.signature:
            raw = f"{self.type}:{self.title}".encode()
            self.signature = hashlib.sha256(raw).hexdigest()[:16]


# ── Defaults ─────────────────────────────────────────────────────────────────

_DEFAULT_SINK = "/var/log/argus/events.jsonl"

# ── Thresholds ────────────────────────────────────────────────────────────────

_ERROR_SPIKE_THRESHOLD = 3          # errors in window → spike
_ESCALATE_RATIO_THRESHOLD = 0.30    # ≥30 % escalates → over-escalating
_MIN_REPLIES_FOR_ESCALATE = 3       # require ≥3 replies before checking ratio
_ACCEPT_WITHOUT_RESOLVE_THRESHOLD = 3  # ACCEPT verdicts without THREAD_RESOLVED
_BLOCKED_THRESHOLD = 5              # blocked requests → flag pattern


def analyze(
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    sink_path: Optional[str] = None,
) -> List[ProblemCluster]:
    """Return a list of ProblemCluster objects for the given time window.

    Args:
        since: Start of analysis window (UTC-aware). None = no lower bound.
        until: End of analysis window (UTC-aware). None = now.
        sink_path: Override for the events.jsonl path. Uses ARGUS_EVENTS_PATH
            or the default /var/log/argus/events.jsonl when None.

    Returns:
        List of ProblemCluster objects ordered by severity (critical first).
        Returns [] without filing a cluster if the sink file does not exist —
        a missing file is an infrastructure/config issue, not an Argus problem.
    """
    import os as _os
    resolved_path = sink_path or _os.environ.get("ARGUS_EVENTS_PATH", _DEFAULT_SINK)
    if not _os.path.exists(resolved_path):
        print(
            f"[self-check] Sink not found: {resolved_path} — "
            "verify ARGUS_EVENTS_PATH and re-run."
        )
        return []

    events = read_events(sink_path=resolved_path, since=since, until=until)

    clusters: List[ProblemCluster] = []

    # ── 1. Data gap: sink exists but no events in window ──────────────────────
    if not events:
        clusters.append(ProblemCluster(
            type=TYPE_DATA_GAP,
            severity=SEVERITY_HIGH,
            title="No events recorded in analysis window",
            evidence=[
                "Sink file exists but zero events found in the requested time window.",
                f"Sink: {resolved_path}",
            ],
            hypothesis=(
                "UNVERIFIED: Argus may have been idle (no PRs reviewed), "
                "the emitter may be failing silently, or the analysis window "
                "may not align with recent activity."
            ),
        ))
        return clusters

    # ── 2. ERROR spike ────────────────────────────────────────────────────────
    error_events = [e for e in events if e.event_type == EventType.ERROR]
    if len(error_events) >= _ERROR_SPIKE_THRESHOLD:
        messages = list({
            e.payload.get("message", "<no message>") for e in error_events
        })
        clusters.append(ProblemCluster(
            type=TYPE_ERROR_SPIKE,
            severity=SEVERITY_HIGH if len(error_events) >= 5 else SEVERITY_MEDIUM,
            title=f"ERROR spike: {len(error_events)} errors in window",
            evidence=[
                f"Observed {len(error_events)} ERROR events.",
                f"Distinct messages ({len(messages)}): "
                + "; ".join(messages[:5])
                + ("…" if len(messages) > 5 else ""),
            ],
            hypothesis=(
                "UNVERIFIED: repeated errors may indicate a broken dependency "
                "(e.g., missing app installation token, GitHub API rate limit, "
                "or unhandled exception in reply handler)."
            ),
        ))

    # ── 3. Over-escalating classifier ────────────────────────────────────────
    reply_events = [
        e for e in events if e.event_type == EventType.REPLY_CLASSIFIED
    ]
    escalate_events = [
        e for e in reply_events
        if e.payload.get("verdict") == "ESCALATE"
        and "LLM error" not in e.payload.get("reason", "")
    ]
    if (
        len(reply_events) >= _MIN_REPLIES_FOR_ESCALATE
        and len(escalate_events) / len(reply_events) >= _ESCALATE_RATIO_THRESHOLD
    ):
        ratio_pct = round(len(escalate_events) / len(reply_events) * 100)
        clusters.append(ProblemCluster(
            type=TYPE_ESCALATE_OVERRATE,
            severity=SEVERITY_MEDIUM,
            title=f"Reply classifier ESCALATE rate {ratio_pct}% exceeds threshold",
            evidence=[
                f"Observed {len(reply_events)} REPLY_CLASSIFIED events.",
                f"{len(escalate_events)} verdicts were ESCALATE ({ratio_pct}%).",
                f"Threshold: {int(_ESCALATE_RATIO_THRESHOLD * 100)}%.",
            ],
            hypothesis=(
                "UNVERIFIED: the LLM prompt may be too conservative, or the "
                "sample of replies genuinely required escalation (e.g., all from "
                "a complex PR). Check individual thread_path values for patterns."
            ),
        ))

    # ── 4. Resolution gap (ACCEPT without subsequent THREAD_RESOLVED) ────────
    # Use latest-timestamp comparison per thread_path so that a thread which
    # was resolved *before* a later ACCEPT is correctly flagged as unresolved.
    _min_dt = datetime.min.replace(tzinfo=timezone.utc)

    last_accept: dict = {}
    for e in reply_events:
        if e.payload.get("verdict") == "ACCEPT":
            path = e.payload.get("thread_path")
            if path:
                try:
                    ts = datetime.fromisoformat(e.timestamp)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    ts = _min_dt
                if path not in last_accept or ts > last_accept[path]:
                    last_accept[path] = ts

    last_resolved: dict = {}
    for e in events:
        if e.event_type == EventType.THREAD_RESOLVED:
            path = e.payload.get("thread_path")
            if path:
                try:
                    ts = datetime.fromisoformat(e.timestamp)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    ts = _min_dt
                if path not in last_resolved or ts > last_resolved[path]:
                    last_resolved[path] = ts

    # Unresolved: last ACCEPT is more recent than last RESOLVED (or never resolved)
    unresolved_accepts = {
        path for path, accept_ts in last_accept.items()
        if path not in last_resolved or accept_ts > last_resolved[path]
    }
    if len(unresolved_accepts) >= _ACCEPT_WITHOUT_RESOLVE_THRESHOLD:
        clusters.append(ProblemCluster(
            type=TYPE_RESOLUTION_GAP,
            severity=SEVERITY_MEDIUM,
            title=(
                f"{len(unresolved_accepts)} threads accepted but not resolved"
            ),
            evidence=[
                f"{len(last_accept)} ACCEPT verdicts, "
                f"{len(last_resolved)} THREAD_RESOLVED events.",
                f"{len(unresolved_accepts)} thread paths have ACCEPT more recent "
                "than their last THREAD_RESOLVED (or were never resolved).",
                "Sample paths: "
                + ", ".join(sorted(unresolved_accepts)[:3])
                + ("…" if len(unresolved_accepts) > 3 else ""),
            ],
            hypothesis=(
                "UNVERIFIED: _resolve_thread() may have failed silently after "
                "posting the Acknowledged reply, or the GraphQL mutation response "
                "was not awaited correctly."
            ),
        ))

    # ── 5. Access control pattern ─────────────────────────────────────────────
    blocked_events = [
        e for e in events if e.event_type == EventType.REQUEST_BLOCKED
    ]
    if len(blocked_events) >= _BLOCKED_THRESHOLD:
        actors = list({e.payload.get("actor", "<unknown>") for e in blocked_events})
        clusters.append(ProblemCluster(
            type=TYPE_ACCESS_PATTERN,
            severity=SEVERITY_LOW,
            title=f"{len(blocked_events)} blocked requests in window",
            evidence=[
                f"Observed {len(blocked_events)} REQUEST_BLOCKED events.",
                f"Distinct actors ({len(actors)}): "
                + ", ".join(actors[:5])
                + ("…" if len(actors) > 5 else ""),
            ],
            hypothesis=(
                "UNVERIFIED: may indicate unauthorized probe activity, a "
                "misconfigured whitelist, or a legitimate user not yet added "
                "to ARGUS_ALLOWED_USERS."
            ),
        ))

    # Sort: critical → high → medium → low
    _order = {
        SEVERITY_CRITICAL: 0,
        SEVERITY_HIGH: 1,
        SEVERITY_MEDIUM: 2,
        SEVERITY_LOW: 3,
    }
    clusters.sort(key=lambda c: _order.get(c.severity, 9))
    return clusters
