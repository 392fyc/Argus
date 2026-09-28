"""Unit tests for the review-decision policy in patch_suggestion_format.py.

_decide_review_event is pure: it maps findings, thread state and iteration to
a GitHub review event. The first-pass rule is the one most likely to regress:
a clean first review must approve, while any finding on the first pass must
stay a COMMENT so the author sees it before approval.
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


def test_unparsed_review_output_never_approves():
    # A failed or malformed parse leaves findings empty; that is not "no issues".
    assert decide([], valid=False)[0] == "COMMENT"
    assert decide([], iteration=2, valid=False)[0] == "COMMENT"


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


def test_clean_first_pass_approves():
    event, reason = decide([], has_inline_comments=False)
    assert event == "APPROVE"
    assert "Initial review" in reason


def test_first_pass_treats_no_string_and_none_as_no_findings():
    assert decide("No")[0] == "APPROVE"
    assert decide(None)[0] == "APPROVE"


def test_clean_first_pass_with_degraded_thread_read_only_comments():
    # A failed thread/iteration read reports iteration 1 with no threads; it
    # must never be mistaken for a clean first review.
    assert decide([], trusted=False)[0] == "COMMENT"


def test_decision_defaults_to_untrusted_inputs():
    assert psf._decide_review_event([], [], 1)[0] == "COMMENT"


def test_first_pass_with_unresolved_threads_requests_changes():
    assert decide([], unresolved=[{"path": "a.py"}], iteration=1)[0] == "REQUEST_CHANGES"


def test_first_pass_keeps_dict_findings_when_mixed_with_strings():
    assert decide([MAJOR, "x"], has_inline_comments=True)[0] == "REQUEST_CHANGES"


def test_first_pass_with_minor_inline_findings_comments():
    assert decide([MINOR], has_inline_comments=True)[0] == "COMMENT"


def test_first_pass_with_minor_findings_not_posted_inline_still_comments():
    assert decide([MINOR], has_inline_comments=False)[0] == "COMMENT"


def test_first_pass_with_major_finding_requests_changes():
    assert decide([MAJOR], has_inline_comments=True)[0] == "REQUEST_CHANGES"


def test_unresolved_threads_request_changes_before_approval():
    assert decide([], unresolved=[{"path": "a.py"}], iteration=2)[0] == "REQUEST_CHANGES"


def test_later_pass_with_new_inline_comments_defers_approval():
    assert decide([MINOR], iteration=2, has_inline_comments=True)[0] == "COMMENT"


def test_later_pass_without_new_code_approves_when_threads_resolved():
    event, reason = decide([MINOR], iteration=2, no_new_code=True, has_inline_comments=False)
    assert event == "APPROVE"
    assert "no new code" in reason


def test_first_pass_without_new_code_and_major_finding_does_not_approve():
    assert decide([MAJOR], no_new_code=True)[0] == "COMMENT"


def test_later_clean_pass_approves():
    assert decide([], iteration=2)[0] == "APPROVE"


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


def test_later_passes_withhold_approval_on_degraded_thread_read():
    assert decide([], iteration=2, trusted=False)[0] == "COMMENT"
    assert decide([], iteration=2, no_new_code=True, trusted=False)[0] == "COMMENT"


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


def test_no_new_code_pass_withholds_approval_when_output_unparsed():
    assert decide([], iteration=2, no_new_code=True, valid=False)[0] == "COMMENT"


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
