"""Unit tests for the review-decision policy in patch_suggestion_format.py.

Argus only gives advice (Argus #49): whatever the findings and thread state,
the GitHub review event is COMMENT, never REQUEST_CHANGES and never APPROVE.
_decide_review_event is pure; it still returns a reason, and those reasons are
what these tests keep honest (an advisory tone, the right cause named).
"""

import sys
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import patch_suggestion_format as psf  # noqa: E402


MINOR = {"issue_header": "Naming", "issue_content": "nit"}
MAJOR = {"issue_header": "Possible bug", "issue_content": "breaks X"}


def decide(findings, unresolved=None, iteration=1, trusted=True, valid=True, **kw):
    return psf._decide_review_event(findings, unresolved or [], iteration,
                                    thread_state_trusted=trusted,
                                    findings_valid=valid, **kw)


def test_unparsed_review_output_is_not_reported_as_no_issues():
    # A failed or malformed parse leaves findings empty; that is not "no issues".
    for it in (1, 2):
        event, reason = decide([], iteration=it, valid=False)
        assert event == "COMMENT"
        assert "could not be parsed" in reason
        assert "no findings" not in reason and "No issues" not in reason


def test_normalize_findings_accepts_only_recognized_shapes():
    assert psf._normalize_findings([]) == ([], True)
    assert psf._normalize_findings(None) == ([], False)
    assert psf._normalize_findings("No") == ([], True)
    assert psf._normalize_findings(" none ") == ([], True)
    assert psf._normalize_findings([MAJOR]) == ([MAJOR], True)
    assert psf._normalize_findings(psf._MISSING) == ([], False)
    assert psf._normalize_findings("Something looks off") == ([], False)
    assert psf._normalize_findings([MAJOR, "x"]) == ([MAJOR], False)
    assert psf._normalize_findings({"a": 1}) == ([], False)


def test_clean_first_pass_comments_without_approving():
    event, reason = decide([], has_inline_comments=False)
    assert event == "COMMENT"
    assert "Initial review" in reason and "no findings" in reason


def test_first_pass_treats_no_string_and_none_as_no_findings():
    assert "no findings" in decide("No")[1]
    assert "no findings" in decide(None)[1]


def test_clean_first_pass_with_degraded_thread_read_only_comments():
    # A failed thread/iteration read reports iteration 1 with no threads; it
    # must never be mistaken for a clean first review.
    assert decide([], trusted=False)[0] == "COMMENT"


def test_decision_defaults_to_untrusted_inputs():
    assert psf._decide_review_event([], [], 1)[0] == "COMMENT"


def test_first_pass_with_unresolved_threads_only_suggests():
    event, reason = decide([], unresolved=[{"path": "a.py"}], iteration=1)
    assert event == "COMMENT"
    assert "advice only" in reason


def test_first_pass_keeps_dict_findings_when_mixed_with_strings():
    event, reason = decide([MAJOR, "x"], has_inline_comments=True)
    assert event == "COMMENT"
    assert "1 critical/major" in reason


def test_first_pass_with_minor_inline_findings_comments():
    assert decide([MINOR], has_inline_comments=True)[0] == "COMMENT"


def test_first_pass_with_minor_findings_not_posted_inline_still_comments():
    assert decide([MINOR], has_inline_comments=False)[0] == "COMMENT"


def test_major_finding_never_requests_changes():
    event, reason = decide([MAJOR], has_inline_comments=True)
    assert event == "COMMENT"
    assert "advice only" in reason and "does not block the merge" in reason
    assert "changes requested" not in reason.lower()


def test_unresolved_threads_never_request_changes():
    event, reason = decide([], unresolved=[{"path": "a.py"}], iteration=2)
    assert event == "COMMENT"
    assert "still open" in reason


def test_later_pass_with_new_inline_comments_defers_approval():
    assert decide([MINOR], iteration=2, has_inline_comments=True)[0] == "COMMENT"


def test_later_pass_without_new_code_only_comments_when_threads_resolved():
    event, reason = decide([MINOR], iteration=2, no_new_code=True, has_inline_comments=False)
    assert event == "COMMENT"
    assert "no new code" in reason


def test_first_pass_without_new_code_and_major_finding_does_not_approve():
    assert decide([MAJOR], no_new_code=True)[0] == "COMMENT"


def test_later_clean_pass_comments_without_approving():
    event, reason = decide([], iteration=2)
    assert event == "COMMENT"
    assert "No issues found" in reason


def test_max_iterations_escalates_instead_of_blocking():
    event, _ = decide([], unresolved=[{"path": "a.py"}], iteration=psf.MAX_ITERATIONS)
    assert event == "COMMENT"


def test_review_banner_reads_engine_and_model_at_runtime():
    body = psf.build_review_body_additions([], 0)
    assert "gpt-5.3-codex" not in body
    assert "PR-Agent 0.34" not in body
    engine_version, model_name = psf._runtime_engine_info()
    assert f"**Engine**: PR-Agent {engine_version} + Argus patches" in body
    assert f"**Model**: {model_name}" in body


def test_runtime_engine_info_falls_back_when_lookups_fail(monkeypatch):
    import builtins
    import importlib.metadata

    real_import = builtins.__import__

    def fail_pr_agent(name, *args, **kwargs):
        if name.startswith("pr_agent"):
            raise ImportError("pr_agent unavailable")
        return real_import(name, *args, **kwargs)

    def fail_version(_name):
        raise importlib.metadata.PackageNotFoundError("pr-agent")

    monkeypatch.setattr(builtins, "__import__", fail_pr_agent)
    monkeypatch.setattr(importlib.metadata, "version", fail_version)
    assert psf._runtime_engine_info() == ("unknown", "unknown")


def test_later_passes_name_a_degraded_thread_read():
    for kw in ({}, {"no_new_code": True}):
        event, reason = decide([], iteration=2, trusted=False, **kw)
        assert event == "COMMENT"
        assert "thread state could not be read" in reason


def test_normalize_findings_accepts_yaml_parsed_no_values():
    import yaml
    for text in ("key_issues_to_review: No", "key_issues_to_review: false",
                 "key_issues_to_review: []"):
        raw = yaml.safe_load(text)["key_issues_to_review"]
        assert psf._normalize_findings(raw) == ([], True), text
    # A bare key parses to None and may be truncated output.
    truncated = yaml.safe_load("security_concerns: No\nkey_issues_to_review:")
    assert psf._normalize_findings(truncated["key_issues_to_review"]) == ([], False)
    assert psf._normalize_findings(True) == ([], False)


def test_no_new_code_pass_names_unparsed_output():
    event, reason = decide([], iteration=2, no_new_code=True, valid=False)
    assert event == "COMMENT" and "could not be parsed" in reason


def test_security_review_clear_only_for_explicit_no():
    assert psf._security_review_clear("No") is True
    assert psf._security_review_clear(False) is True
    assert psf._security_review_clear(None) is False
    assert psf._security_review_clear(psf._MISSING) is False
    assert psf._security_review_clear("SQL injection via raw query in db.py") is False


def test_security_review_required_defaults_true_outside_container():
    assert psf._security_review_required() is True


def test_blank_strings_are_not_explicit_no_values():
    import yaml
    truncated = yaml.safe_load("security_concerns: |-\nkey_issues_to_review: |-\n")
    assert psf._normalize_findings(truncated["key_issues_to_review"]) == ([], False)
    assert psf._security_review_clear(truncated["security_concerns"]) is False
    assert psf._normalize_findings("   ") == ([], False)


class _FakeResponse:
    def __init__(self, items, link=None, broken=False):
        self._items = items
        self.headers = {"Link": link} if link else {}
        self._broken = broken

    def json(self):
        if self._broken:
            raise ValueError("bad json")
        return self._items


def test_has_more_pages_flags_possible_unread_pages():
    assert psf._has_more_pages(_FakeResponse([{}] * 3)) is False
    assert psf._has_more_pages(_FakeResponse([{}] * 100)) is True
    assert psf._has_more_pages(_FakeResponse([{}], link='<https://x?page=2>; rel="next"')) is True
    assert psf._has_more_pages(_FakeResponse([], broken=True)) is True


def test_security_concern_is_quoted_in_the_reason():
    event, reason = decide([], security_concern="Repo write protection can be bypassed")
    assert event == "COMMENT"
    assert "security concern reported" in reason
    assert "Repo write protection can be bypassed" in reason
    event, reason = decide([], iteration=2, no_new_code=True, security_concern="x")
    assert event == "COMMENT" and "security concern" in reason


def test_withheld_reason_names_the_failing_input():
    assert "thread state could not be read" in decide([], trusted=False)[1]
    assert "could not be parsed" in decide([], valid=False)[1]
    assert "could not be parsed" in decide([], iteration=2, valid=False)[1]


def test_withheld_reason_truncates_long_security_text():
    reason = psf._withheld_reason(True, True, "a " * 400)
    assert reason.endswith("…")
    assert len(reason) < 400


def test_security_reason_takes_precedence_and_is_added_to_comment_paths():
    assert psf._withheld_reason(False, False, "x").startswith("security concern")
    assert "security concern" in decide([MINOR], has_inline_comments=True, security_concern="x")[1]
    assert "security concern" in decide([MINOR], iteration=2, has_inline_comments=True,
                                        security_concern="x")[1]


def test_event_is_comment_for_every_input_combination():
    import itertools
    findings_options = ([], [MINOR], [MAJOR], [MAJOR, MINOR], "No", None)
    for findings, unresolved, iteration, inline, no_new, trusted, valid, sec in itertools.product(
            findings_options, ([], [{"path": "a.py"}]), (1, 2, psf.MAX_ITERATIONS),
            (False, True), (False, True), (False, True), (False, True), (None, "leak")):
        event, reason = psf._decide_review_event(
            findings, unresolved, iteration, has_inline_comments=inline, no_new_code=no_new,
            escalated_threads=[{"path": "b.py"}], thread_state_trusted=trusted,
            findings_valid=valid, security_concern=sec)
        assert event == "COMMENT"
        assert isinstance(reason, str) and reason


def test_advisory_event_clamps_any_requested_event():
    for requested in ("APPROVE", "REQUEST_CHANGES", "COMMENT", "PENDING", None, ""):
        assert psf._advisory_event(requested) == "COMMENT"


def test_no_code_path_names_a_blocking_event():
    # Source-level guard: outside comments and docstrings, patch_suggestion_format
    # never mentions REQUEST_CHANGES or APPROVE, so nothing can pass them to GitHub.
    import re
    src = (REPO_ROOT / "patch_suggestion_format.py").read_text(encoding="utf-8")
    code = re.sub(r'"""[\s\S]*?"""|#[^\n]*', "", src)
    assert "REQUEST_CHANGES" not in code
    assert "APPROVE" not in code
