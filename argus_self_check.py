"""
Argus self-check orchestrator.

Runs the log analyzer over a configurable time window, formats findings as
GitHub Issues, deduplicates against open Issues, and files new ones (up to
a per-run cap).

Usage::

    # Preview (no Issues filed):
    python argus_self_check.py --days 3 --dry-run

    # Production run (files up to 5 Issues):
    python argus_self_check.py --days 3

    # Custom window:
    python argus_self_check.py --days 7 --max-issues 3

Environment variables:
    ARGUS_EVENTS_PATH  Path to events.jsonl (default: /var/log/argus/events.jsonl)
    GH_TOKEN           GitHub token with issues:write scope (used by gh CLI)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from typing import List

from argus_log_analyzer import analyze, ProblemCluster
from argus_issue_formatter import format_issue

_REPO = "392fyc/Argus"
_DEFAULT_DAYS = 3
_DEFAULT_MAX_ISSUES = 5


def _is_duplicate(signature: str) -> bool:
    """Return True if an open Issue with the given signature already exists."""
    try:
        result = subprocess.run(
            [
                "gh", "issue", "list",
                "--repo", _REPO,
                "--state", "open",
                "--search", f"sig:{signature}",
                "--limit", "5",
                "--json", "number",
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print("[self-check] WARNING: gh CLI not found — skipping dedup check")
        return False  # fail-open
    if result.returncode != 0:
        print(f"[self-check] WARNING: dedup check failed: {result.stderr.strip()}")
        return False  # fail-open: allow filing rather than silently skip
    import json
    try:
        issues = json.loads(result.stdout)
        return len(issues) > 0
    except Exception:
        return False


def _file_issue(title: str, body: str, labels: List[str]) -> int:
    """Create a GitHub Issue.

    Returns:
        Positive integer  — new issue number (success, number known)
        0                 — issue filed but URL was not parseable (success, number unknown)
        -1                — gh CLI call failed (issue NOT filed)
    """
    label_args = []
    for lbl in labels:
        label_args += ["--label", lbl]
    try:
        result = subprocess.run(
            [
                "gh", "issue", "create",
                "--repo", _REPO,
                "--title", title,
                "--body", body,
            ] + label_args,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print("[self-check] ERROR: gh CLI not found — cannot file issue")
        return -1
    if result.returncode != 0:
        print(f"[self-check] ERROR filing issue: {result.stderr.strip()}")
        return -1
    # gh prints the issue URL; extract the number
    url = result.stdout.strip()
    try:
        return int(url.rstrip("/").rsplit("/", 1)[-1])
    except ValueError:
        # Filed successfully but number not parseable from URL
        print(f"[self-check] Issue filed (URL: {url})")
        return 0  # >= 0 → caller counts as filed


def run(
    days: int = _DEFAULT_DAYS,
    max_issues: int = _DEFAULT_MAX_ISSUES,
    dry_run: bool = False,
    sink_path: str | None = None,
) -> int:
    """Run the self-check and return the number of Issues filed (0 in dry-run).

    Safeguards enforced here (in addition to issue.md safeguards):
    - Only targets _REPO (392fyc/Argus) — hardcoded, not configurable
    - Caps at max_issues per run
    - Deduplicates by signature against open Issues
    - Never modifies code or configuration files
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    since_iso = since.isoformat()
    until_iso = now.isoformat()

    print(f"[self-check] Analyzing {days}-day window: {since_iso} → {until_iso}")
    clusters: List[ProblemCluster] = analyze(since=since, until=now, sink_path=sink_path)
    print(f"[self-check] {len(clusters)} problem cluster(s) found")

    if not clusters:
        print("[self-check] No problems detected — done.")
        return 0

    filed = 0
    for cluster in clusters:
        if filed >= max_issues:
            print(f"[self-check] Reached max_issues cap ({max_issues}); stopping.")
            break

        title, body, labels = format_issue(cluster, since_iso, until_iso)
        print(f"\n[self-check] Cluster: {cluster.type} | {cluster.severity}")
        print(f"  Title: {title[:80]}")
        print(f"  Evidence: {'; '.join(cluster.evidence[:2])[:120]}")

        if dry_run:
            print("  [dry-run] Would file issue — skipping")
            continue

        if _is_duplicate(cluster.signature):
            print(f"  [dedup] Open issue with sig:{cluster.signature} exists — skipping")
            continue

        issue_num = _file_issue(title, body, labels)
        if issue_num > 0:
            print(f"  Filed: {_REPO}#{issue_num}")
            filed += 1
        elif issue_num == 0:
            print(f"  Filed: {_REPO} (issue number unknown — check repo)")
            filed += 1
        else:
            print(f"  ERROR: failed to file issue for cluster {cluster.type}")

    print(f"\n[self-check] Done. {filed} issue(s) filed.")
    return filed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Argus self-check: analyze structured events and file Issues."
    )
    parser.add_argument(
        "--days", type=int, default=_DEFAULT_DAYS,
        help=f"Analysis window in days (default: {_DEFAULT_DAYS})"
    )
    parser.add_argument(
        "--max-issues", type=int, default=_DEFAULT_MAX_ISSUES,
        help=f"Max Issues to file per run (default: {_DEFAULT_MAX_ISSUES})"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print findings without filing any Issues"
    )
    parser.add_argument(
        "--sink-path", type=str, default=None,
        help="Override path to events.jsonl (overrides ARGUS_EVENTS_PATH)"
    )
    args = parser.parse_args()

    filed = run(
        days=args.days,
        max_issues=args.max_issues,
        dry_run=args.dry_run,
        sink_path=args.sink_path,
    )
    # Exit 2 when issues were filed (signals "findings detected" to adaptive scheduler).
    # Exit 0 when quiet (no issues). Unhandled exceptions propagate as exit 1.
    sys.exit(2 if filed > 0 else 0)


if __name__ == "__main__":
    main()
