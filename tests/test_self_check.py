"""
Tests for M3 self-check: analyzer, formatter, orchestrator.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest

from argus_events import EventType, ArgusEvent
from argus_log_analyzer import (
    analyze,
    ProblemCluster,
    TYPE_ERROR_SPIKE,
    TYPE_ESCALATE_OVERRATE,
    TYPE_RESOLUTION_GAP,
    TYPE_ACCESS_PATTERN,
    TYPE_DATA_GAP,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
    SEVERITY_LOW,
)
from argus_issue_formatter import format_issue
from argus_self_check import run


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_events(path: str, events: list[ArgusEvent]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e.to_dict()) + "\n")


def _make_event(event_type: EventType, **payload) -> ArgusEvent:
    return ArgusEvent(
        event_type=event_type,
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
    )


# ── TestAnalyzer ──────────────────────────────────────────────────────────────

class TestAnalyzer:
    def test_empty_sink_returns_data_gap(self, tmp_path):
        sink = str(tmp_path / "events.jsonl")
        _write_events(sink, [])
        clusters = analyze(sink_path=sink)
        assert len(clusters) == 1
        assert clusters[0].type == TYPE_DATA_GAP

    def test_no_sink_file_returns_empty(self, tmp_path):
        """Missing sink = infrastructure/config issue; no cluster filed."""
        sink = str(tmp_path / "nonexistent.jsonl")
        clusters = analyze(sink_path=sink)
        assert clusters == []

    def test_error_spike_threshold(self, tmp_path):
        sink = str(tmp_path / "events.jsonl")
        events = [
            _make_event(EventType.ERROR, message=f"err {i}") for i in range(4)
        ]
        _write_events(sink, events)
        clusters = analyze(sink_path=sink)
        types = [c.type for c in clusters]
        assert TYPE_ERROR_SPIKE in types

    def test_below_error_threshold_no_spike(self, tmp_path):
        sink = str(tmp_path / "events.jsonl")
        events = [
            _make_event(EventType.ERROR, message="err") for _ in range(2)
        ]
        _write_events(sink, events)
        clusters = analyze(sink_path=sink)
        types = [c.type for c in clusters]
        assert TYPE_ERROR_SPIKE not in types

    def test_escalate_overrate(self, tmp_path):
        sink = str(tmp_path / "events.jsonl")
        events = (
            [_make_event(EventType.REPLY_CLASSIFIED, verdict="ESCALATE", reason="genuine", thread_path=f"f{i}") for i in range(4)]
            + [_make_event(EventType.REPLY_CLASSIFIED, verdict="ACCEPT", thread_path="fA")]
        )
        _write_events(sink, events)
        clusters = analyze(sink_path=sink)
        types = [c.type for c in clusters]
        assert TYPE_ESCALATE_OVERRATE in types

    def test_escalate_lm_error_excluded(self, tmp_path):
        """LLM errors must not count toward escalate rate."""
        sink = str(tmp_path / "events.jsonl")
        events = [
            _make_event(EventType.REPLY_CLASSIFIED, verdict="ESCALATE", reason="LLM error: timeout", thread_path="f1"),
            _make_event(EventType.REPLY_CLASSIFIED, verdict="ESCALATE", reason="LLM error: timeout", thread_path="f2"),
            _make_event(EventType.REPLY_CLASSIFIED, verdict="ESCALATE", reason="LLM error: timeout", thread_path="f3"),
            _make_event(EventType.REPLY_CLASSIFIED, verdict="ACCEPT", thread_path="fA"),
        ]
        _write_events(sink, events)
        clusters = analyze(sink_path=sink)
        types = [c.type for c in clusters]
        assert TYPE_ESCALATE_OVERRATE not in types

    def test_resolution_gap(self, tmp_path):
        sink = str(tmp_path / "events.jsonl")
        events = [
            _make_event(EventType.REPLY_CLASSIFIED, verdict="ACCEPT", thread_path=f"file{i}:10")
            for i in range(4)
        ]
        # No THREAD_RESOLVED events
        _write_events(sink, events)
        clusters = analyze(sink_path=sink)
        types = [c.type for c in clusters]
        assert TYPE_RESOLUTION_GAP in types

    def test_resolution_gap_cleared_by_resolved(self, tmp_path):
        sink = str(tmp_path / "events.jsonl")
        paths = [f"file{i}:10" for i in range(4)]
        events = (
            [_make_event(EventType.REPLY_CLASSIFIED, verdict="ACCEPT", thread_path=p) for p in paths]
            + [_make_event(EventType.THREAD_RESOLVED, thread_path=p) for p in paths]
        )
        _write_events(sink, events)
        clusters = analyze(sink_path=sink)
        types = [c.type for c in clusters]
        assert TYPE_RESOLUTION_GAP not in types

    def test_access_pattern(self, tmp_path):
        sink = str(tmp_path / "events.jsonl")
        events = [
            _make_event(EventType.REQUEST_BLOCKED, actor=f"user{i}") for i in range(6)
        ]
        _write_events(sink, events)
        clusters = analyze(sink_path=sink)
        types = [c.type for c in clusters]
        assert TYPE_ACCESS_PATTERN in types

    def test_signature_is_stable(self, tmp_path):
        sink = str(tmp_path / "events.jsonl")
        events = [_make_event(EventType.ERROR, message="err") for _ in range(4)]
        _write_events(sink, events)
        c1 = analyze(sink_path=sink)
        c2 = analyze(sink_path=sink)
        assert c1[0].signature == c2[0].signature

    def test_clusters_sorted_by_severity(self, tmp_path):
        sink = str(tmp_path / "events.jsonl")
        # 5 errors → SEVERITY_HIGH (threshold), 6 blocked → SEVERITY_LOW
        events = (
            [_make_event(EventType.ERROR, message="err") for _ in range(5)]
            + [_make_event(EventType.REQUEST_BLOCKED, actor=f"u{i}") for i in range(6)]
        )
        _write_events(sink, events)
        clusters = analyze(sink_path=sink)
        severities = [c.severity for c in clusters]
        # High (error_spike) must come before Low (access_pattern)
        assert severities.index(SEVERITY_HIGH) < severities.index(SEVERITY_LOW)


# ── TestFormatter ─────────────────────────────────────────────────────────────

class TestFormatter:
    def _make_cluster(self, cluster_type=TYPE_ERROR_SPIKE, severity=SEVERITY_HIGH):
        return ProblemCluster(
            type=cluster_type,
            severity=severity,
            title="Test cluster",
            evidence=["3 errors observed"],
            hypothesis="UNVERIFIED: something broke",
        )

    def test_title_contains_signature(self):
        cluster = self._make_cluster()
        title, _, _ = format_issue(cluster, "2026-01-01T00:00:00Z", "2026-01-04T00:00:00Z")
        assert cluster.signature in title

    def test_body_contains_unverified(self):
        cluster = self._make_cluster()
        _, body, _ = format_issue(cluster, "2026-01-01T00:00:00Z", "2026-01-04T00:00:00Z")
        assert "UNVERIFIED" in body

    def test_body_contains_evidence(self):
        cluster = self._make_cluster()
        _, body, _ = format_issue(cluster, "2026-01-01T00:00:00Z", "2026-01-04T00:00:00Z")
        assert "3 errors observed" in body

    def test_labels_contain_source_self_check(self):
        cluster = self._make_cluster()
        _, _, labels = format_issue(cluster, "2026-01-01T00:00:00Z", "2026-01-04T00:00:00Z")
        assert "source:self-check" in labels

    def test_labels_contain_type(self):
        cluster = self._make_cluster(cluster_type=TYPE_ERROR_SPIKE)
        _, _, labels = format_issue(cluster, "2026-01-01T00:00:00Z", "2026-01-04T00:00:00Z")
        assert "type:bug" in labels

    def test_labels_contain_priority(self):
        cluster = self._make_cluster(severity=SEVERITY_HIGH)
        _, _, labels = format_issue(cluster, "2026-01-01T00:00:00Z", "2026-01-04T00:00:00Z")
        assert any(l.startswith("priority:") for l in labels)


# ── TestSelfCheck orchestrator ────────────────────────────────────────────────

class TestSelfCheck:
    def test_dry_run_files_nothing(self, tmp_path):
        sink = str(tmp_path / "events.jsonl")
        events = [_make_event(EventType.ERROR, message="e") for _ in range(4)]
        _write_events(sink, events)
        filed = run(days=3, dry_run=True, sink_path=sink)
        assert filed == 0

    def test_no_problems_no_issues(self, tmp_path):
        sink = str(tmp_path / "events.jsonl")
        _write_events(sink, [])
        # Empty sink creates a data_gap cluster; mock dedup as True so it's skipped
        with patch("argus_self_check._is_duplicate", return_value=True), \
             patch("argus_self_check._file_issue") as mock_file:
            filed = run(days=3, dry_run=False, sink_path=sink)
        mock_file.assert_not_called()
        assert filed == 0

    def test_max_issues_cap(self, tmp_path):
        sink = str(tmp_path / "events.jsonl")
        # Generate enough events for multiple clusters
        events = (
            [_make_event(EventType.ERROR, message=f"err {i}") for i in range(4)]
            + [_make_event(EventType.REQUEST_BLOCKED, actor=f"u{i}") for i in range(6)]
        )
        _write_events(sink, events)
        with patch("argus_self_check._is_duplicate", return_value=False), \
             patch("argus_self_check._file_issue", return_value=42):
            filed = run(days=3, max_issues=1, dry_run=False, sink_path=sink)
        assert filed == 1

    def test_dedup_skips_existing(self, tmp_path):
        sink = str(tmp_path / "events.jsonl")
        events = [_make_event(EventType.ERROR, message="err") for _ in range(4)]
        _write_events(sink, events)
        with patch("argus_self_check._is_duplicate", return_value=True), \
             patch("argus_self_check._file_issue") as mock_file:
            filed = run(days=3, dry_run=False, sink_path=sink)
        mock_file.assert_not_called()
        assert filed == 0
