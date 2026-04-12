"""
Argus Issue formatter.

Converts a ProblemCluster into a GitHub Issue title + body + label list
that matches the Argus repo's issue conventions.

Usage::

    from argus_issue_formatter import format_issue
    title, body, labels = format_issue(cluster, since_iso, until_iso)
"""

from __future__ import annotations

from typing import List, Tuple

from argus_log_analyzer import (
    ProblemCluster,
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
    SEVERITY_LOW,
    TYPE_ERROR_SPIKE,
    TYPE_ESCALATE_OVERRATE,
    TYPE_RESOLUTION_GAP,
    TYPE_ACCESS_PATTERN,
    TYPE_DATA_GAP,
)


_SEVERITY_EMOJI = {
    SEVERITY_CRITICAL: "🔴",
    SEVERITY_HIGH: "🟠",
    SEVERITY_MEDIUM: "🟡",
    SEVERITY_LOW: "🔵",
}

_TYPE_TO_ISSUE_TYPE = {
    TYPE_ERROR_SPIKE: "type:bug",
    TYPE_ESCALATE_OVERRATE: "type:bug",
    TYPE_RESOLUTION_GAP: "type:bug",
    TYPE_ACCESS_PATTERN: "type:analysis",
    TYPE_DATA_GAP: "type:bug",
}

_SEVERITY_TO_PRIORITY = {
    SEVERITY_CRITICAL: "priority:p0",
    SEVERITY_HIGH: "priority:p1",
    SEVERITY_MEDIUM: "priority:p2",
    SEVERITY_LOW: "priority:p2",
}

_TYPE_TO_DESCRIPTION = {
    TYPE_ERROR_SPIKE: "error spike detected in structured event log",
    TYPE_ESCALATE_OVERRATE: "reply classifier ESCALATE rate exceeds threshold",
    TYPE_RESOLUTION_GAP: "threads accepted but not formally resolved",
    TYPE_ACCESS_PATTERN: "blocked request pattern observed",
    TYPE_DATA_GAP: "no events recorded — sensor health check needed",
}


def format_issue(
    cluster: ProblemCluster,
    since_iso: str,
    until_iso: str,
) -> Tuple[str, str, List[str]]:
    """Return (title, body, labels) for a ProblemCluster.

    Args:
        cluster: The detected problem.
        since_iso: ISO 8601 start of analysis window (for traceability).
        until_iso: ISO 8601 end of analysis window.

    Returns:
        Tuple of (issue_title, issue_body_markdown, label_list).
    """
    emoji = _SEVERITY_EMOJI.get(cluster.severity, "⚪")
    short_desc = _TYPE_TO_DESCRIPTION.get(cluster.type, cluster.type)

    title = f"[self-check] {emoji} {short_desc} — sig:{cluster.signature}"

    evidence_md = "\n".join(f"- {e}" for e in cluster.evidence)
    body = f"""\
## Self-check finding

**Severity**: {cluster.severity}
**Type**: `{cluster.type}`
**Analysis window**: `{since_iso}` → `{until_iso}`
**Dedup signature**: `{cluster.signature}`

> This Issue was auto-filed by the Argus self-check agent. It has NOT been
> manually verified. All hypotheses are marked accordingly.

---

## Observed evidence

{evidence_md}

## Hypothesis (UNVERIFIED)

{cluster.hypothesis}

---

## Suggested next step

1. Pull recent structured events: `python argus_self_check.py --days 3 --dry-run`
2. Inspect the relevant event type in the jsonl sink
3. Confirm or refute the hypothesis before filing a fix PR

## Labels

This issue is tagged `source:self-check` — it was generated automatically.
To suppress future reports of this type, resolve or close this Issue
(the dedup check will not re-file while an open Issue with the same signature exists).
"""

    labels = [
        "source:self-check",
        _TYPE_TO_ISSUE_TYPE.get(cluster.type, "type:analysis"),
        _SEVERITY_TO_PRIORITY.get(cluster.severity, "priority:p2"),
    ]
    return title, body, labels
