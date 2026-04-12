"""Tests for argus_events + argus_extractor (schema + round-trip)."""

import json
import os
import tempfile
from datetime import datetime, timezone, timedelta

import pytest

from argus_events import ArgusEvent, EventType, EventEmitter
from argus_extractor import read_events


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def tmp_sink(tmp_path):
    return str(tmp_path / "events.jsonl")


@pytest.fixture
def emitter(tmp_sink):
    return EventEmitter(sink_path=tmp_sink)


def _make_event(event_type: EventType, **kwargs) -> ArgusEvent:
    return ArgusEvent(
        event_type=event_type,
        timestamp=datetime.now(timezone.utc).isoformat(),
        pr_number=kwargs.get("pr_number", 42),
        repo=kwargs.get("repo", "392fyc/Mercury"),
        severity=kwargs.get("severity"),
        payload=kwargs.get("payload", {}),
    )


# ── Schema tests ──────────────────────────────────────────────────────

class TestArgusEventSchema:
    def test_all_event_types_defined(self):
        expected = {
            "review_started", "finding_posted", "reply_classified",
            "thread_resolved", "error", "mention_rewritten", "request_blocked",
        }
        assert {e.value for e in EventType} == expected

    def test_to_dict_round_trip(self):
        event = _make_event(EventType.REVIEW_STARTED, severity="Medium",
                            payload={"iteration": 1})
        d = event.to_dict()
        assert d["event_type"] == "review_started"
        assert d["pr_number"] == 42
        assert d["payload"]["iteration"] == 1
        restored = ArgusEvent.from_dict(d)
        assert restored.event_type == EventType.REVIEW_STARTED
        assert restored.pr_number == 42
        assert restored.payload["iteration"] == 1

    def test_from_dict_rejects_unknown_event_type(self):
        d = _make_event(EventType.ERROR).to_dict()
        d["event_type"] = "not_a_real_type"
        with pytest.raises(ValueError):
            ArgusEvent.from_dict(d)

    @pytest.mark.parametrize("event_type", list(EventType))
    def test_each_event_type_serializes(self, event_type):
        event = _make_event(event_type)
        d = event.to_dict()
        assert d["event_type"] == event_type.value
        restored = ArgusEvent.from_dict(d)
        assert restored.event_type == event_type


# ── EventEmitter tests ────────────────────────────────────────────────

class TestEventEmitter:
    def test_emit_writes_jsonline(self, emitter, tmp_sink):
        emitter.emit(EventType.REQUEST_BLOCKED, pr_number=1, repo="r/r", actor="eve")
        assert os.path.exists(tmp_sink)
        with open(tmp_sink) as fh:
            lines = [l for l in fh.read().splitlines() if l]
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event_type"] == "request_blocked"
        assert record["payload"]["actor"] == "eve"

    def test_emit_multiple_events_appends(self, emitter, tmp_sink):
        emitter.emit(EventType.REVIEW_STARTED, pr_number=10)
        emitter.emit(EventType.FINDING_POSTED, pr_number=10)
        emitter.emit(EventType.ERROR, pr_number=10, message="boom")
        with open(tmp_sink) as fh:
            lines = [l for l in fh.read().splitlines() if l]
        assert len(lines) == 3
        types = [json.loads(l)["event_type"] for l in lines]
        assert types == ["review_started", "finding_posted", "error"]

    def test_emit_does_not_raise_on_bad_path(self):
        bad_emitter = EventEmitter(sink_path="/nonexistent/deep/path/events.jsonl")
        # Should not raise
        bad_emitter.emit(EventType.ERROR)

    def test_emit_payload_kwargs(self, emitter, tmp_sink):
        emitter.emit(EventType.REPLY_CLASSIFIED, pr_number=5, verdict="ACCEPT",
                     thread_path="src/foo.py")
        with open(tmp_sink) as fh:
            record = json.loads(fh.readline())
        assert record["payload"]["verdict"] == "ACCEPT"
        assert record["payload"]["thread_path"] == "src/foo.py"


# ── Extractor round-trip tests ────────────────────────────────────────

class TestReadEvents:
    def test_empty_file_returns_empty_list(self, tmp_sink):
        open(tmp_sink, "w").close()
        assert read_events(sink_path=tmp_sink) == []

    def test_missing_file_returns_empty_list(self, tmp_path):
        assert read_events(sink_path=str(tmp_path / "missing.jsonl")) == []

    def test_full_round_trip(self, emitter, tmp_sink):
        emitter.emit(EventType.REVIEW_STARTED, pr_number=7, repo="392fyc/Argus",
                     iteration=2)
        emitter.emit(EventType.FINDING_POSTED, pr_number=7, count=3)
        events = read_events(sink_path=tmp_sink)
        assert len(events) == 2
        assert events[0].event_type == EventType.REVIEW_STARTED
        assert events[0].pr_number == 7
        assert events[0].payload["iteration"] == 2
        assert events[1].event_type == EventType.FINDING_POSTED
        assert events[1].payload["count"] == 3

    def test_filter_by_event_type(self, emitter, tmp_sink):
        emitter.emit(EventType.REVIEW_STARTED, pr_number=1)
        emitter.emit(EventType.ERROR, pr_number=1, message="oops")
        emitter.emit(EventType.FINDING_POSTED, pr_number=1)
        errors = read_events(sink_path=tmp_sink, event_types=[EventType.ERROR])
        assert len(errors) == 1
        assert errors[0].event_type == EventType.ERROR

    def test_filter_by_time_range(self, emitter, tmp_sink):
        now = datetime.now(timezone.utc)
        # Write an event, then filter it out with a future `since`
        emitter.emit(EventType.MENTION_REWRITTEN, pr_number=2)
        future = now + timedelta(hours=1)
        events = read_events(sink_path=tmp_sink, since=future)
        assert events == []

    def test_filter_since_includes_matching(self, emitter, tmp_sink):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        emitter.emit(EventType.THREAD_RESOLVED, pr_number=3)
        events = read_events(sink_path=tmp_sink, since=past)
        assert len(events) == 1

    def test_malformed_line_skipped(self, tmp_sink):
        with open(tmp_sink, "w") as fh:
            fh.write('not json\n')
            fh.write(json.dumps(ArgusEvent(
                event_type=EventType.ERROR,
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload={},
            ).to_dict()) + "\n")
        events = read_events(sink_path=tmp_sink)
        assert len(events) == 1
        assert events[0].event_type == EventType.ERROR
