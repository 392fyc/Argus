"""Tests for the finding filters (Argus #49 requirement 4): the severity
threshold, skip_paths, and not repeating a finding that already has a thread."""

import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import patch_suggestion_format as psf  # noqa: E402


def finding(header="Possible Bug", content="The retry loop never stops when the token refresh "
            "returns 401, so every request hangs forever.", path="src/client.py"):
    return {"relevant_file": path, "issue_header": header, "issue_content": content,
            "start_line": 10, "end_line": 12}


def thread_for(issue, resolved=False, replied=False, judged=False, by_human=None):
    """by_human defaults to True for a resolved thread (a person closed it)."""
    if by_human is None:
        by_human = resolved
    return {"path": issue["relevant_file"], "resolved": resolved,
            "resolved_by_human": resolved and by_human, "replied": replied,
            "judged": judged, "first_bot_body": psf.format_review_finding_body(issue)}


# ── severity threshold ────────────────────────────────────────────

def test_default_threshold_keeps_medium_and_above_and_drops_nits():
    kept, dropped = psf.filter_findings([
        finding("Critical Bug"), finding("Security"), finding("Possible Bug"),
        finding("Possible Issue"), finding("Performance"),
        finding("Naming"), finding("Typo"), finding("Maintainability")])
    assert [f["issue_header"] for f in kept] == [
        "Critical Bug", "Security", "Possible Bug", "Possible Issue", "Performance"]
    assert [f["issue_header"] for f, _ in dropped] == ["Naming", "Typo", "Maintainability"]
    assert all("severity" in reason for _, reason in dropped)


def test_threshold_is_configurable_and_case_insensitive():
    items = [finding("Performance"), finding("Possible Issue"), finding("Naming")]
    kept, _ = psf.filter_findings(items, min_severity="major")
    assert [f["issue_header"] for f in kept] == ["Possible Issue"]
    kept, _ = psf.filter_findings(items, min_severity="Minor")
    assert len(kept) == 3
    kept, _ = psf.filter_findings(items, min_severity="Trivial")
    assert len(kept) == 3


def test_chinese_headers_are_ranked_not_dropped():
    # zh-CN is configured, so the model may write the header in Chinese.
    keep = ["严重问题", "安全漏洞", "可能的错误", "性能问题", "并发隐患", "某个没见过的标题"]
    nits = ["次要建议", "代码风格", "命名问题", "细节优化", "拼写错误"]
    kept, dropped = psf.filter_findings([finding(h) for h in keep + nits])
    assert [f["issue_header"] for f in kept] == keep
    assert [f["issue_header"] for f, _ in dropped] == nits


def test_an_unrecognised_header_is_medium_and_survives_the_default_threshold():
    assert psf._get_review_severity("Race condition")[1] == "Medium"
    assert psf._get_review_severity("")[1] == "Medium"
    assert psf._get_review_severity(None)[1] == "Medium"
    kept, _ = psf.filter_findings([finding("Race condition")])
    assert len(kept) == 1


def test_english_nit_words_still_rank_minor_without_false_hits():
    assert psf._get_review_severity("Nit: spacing")[1] == "Minor"
    assert psf._get_review_severity("Initialization order")[1] == "Medium"
    assert psf._get_review_severity("Typo")[1] == "Trivial"


def test_the_badge_and_the_blocking_classifier_agree():
    for header in ("Possible Bug", "Naming", "Race condition", "严重问题", "次要建议"):
        assert psf._classify_finding_severity(header) == psf._get_review_severity(header)[1]


def test_unknown_threshold_falls_back_to_medium():
    assert psf._min_severity_name("loud") == "Medium"
    assert psf._min_severity_name(None) == "Medium"
    assert psf._min_severity_name(" CRITICAL ") == "Critical"


def test_threshold_setting_reads_configuration_table(monkeypatch):
    class Settings:
        def get(self, key):
            return {"MIN_SEVERITY": "Major"} if key == "argus_review" else None

    import types
    fake = types.ModuleType("pr_agent.config_loader")
    fake.get_settings = lambda: Settings()
    monkeypatch.setitem(sys.modules, "pr_agent", types.ModuleType("pr_agent"))
    monkeypatch.setitem(sys.modules, "pr_agent.config_loader", fake)
    assert psf._argus_review_setting("min_severity", "Medium") == "Major"
    assert psf._argus_review_setting("missing", "dflt") == "dflt"


# ── skip_paths ────────────────────────────────────────────────────

def test_skip_paths_drop_matching_findings_only():
    items = [finding(path="docs/guide.md"), finding(path="src/a.py"), finding(path="deep/x.lock")]
    kept, dropped = psf.filter_findings(items, skip_patterns=("docs/**", "*.lock"))
    assert [f["relevant_file"] for f in kept] == ["src/a.py"]
    assert {reason for _, reason in dropped} == {"skip_paths"}


# ── repeated findings ─────────────────────────────────────────────

def test_rephrased_finding_matches_its_thread():
    original = finding()
    again = finding(content="Retry loop does not stop when the token refresh gives 401; "
                    "requests hang forever.")
    assert psf.finding_matches_thread(again, thread_for(original))


def test_different_problem_in_same_file_does_not_match():
    original = finding()
    other = finding("Security", "User input is concatenated into the SQL statement, which "
                    "allows injection through the search parameter.")
    assert not psf.finding_matches_thread(other, thread_for(original))


def test_same_text_on_another_file_does_not_match():
    original = finding()
    assert not psf.finding_matches_thread(finding(path="src/other.py"), thread_for(original))


def test_generic_header_words_alone_do_not_match():
    a = finding("Possible Bug", "Cache entries are never evicted after the shutdown hook runs.")
    b = finding("Possible Bug", "Timezone conversion drops the offset for negative values.")
    assert not psf.finding_matches_thread(b, thread_for(a))


def test_finding_for_resolved_thread_is_dropped():
    original = finding()
    kept, dropped = psf.filter_findings(
        [finding(content="Retry loop does not stop when token refresh gives 401; "
                 "requests hang forever.")],
        threads=[thread_for(original, resolved=True)])
    assert kept == []
    assert dropped[0][1] == "repeats a closed thread"


def test_finding_for_answered_or_rejected_thread_is_dropped():
    original = finding()
    for kw in ({"replied": True}, {"judged": True}):
        kept, dropped = psf.filter_findings([original], threads=[thread_for(original, **kw)])
        assert kept == []
        assert dropped[0][1] == "repeats a closed thread"


def test_finding_for_open_thread_is_dropped_as_open_repeat():
    original = finding()
    kept, dropped = psf.filter_findings([original], threads=[thread_for(original)])
    assert kept == [] and dropped[0][1] == "repeats an open thread"


def test_thread_closed_by_argus_itself_does_not_suppress_the_finding():
    # Argus resolves threads when lines change or go outdated; that does not
    # mean the problem was fixed, so the finding must be allowed through.
    original = finding()
    kept, dropped = psf.filter_findings(
        [original], threads=[thread_for(original, resolved=True, by_human=False)])
    assert kept == [original] and dropped == []


def test_argus_closed_thread_still_counts_when_a_person_replied_or_argus_judged():
    original = finding()
    for kw in ({"replied": True}, {"judged": True}):
        kept, dropped = psf.filter_findings(
            [original], threads=[thread_for(original, resolved=True, by_human=False, **kw)])
        assert kept == [] and dropped[0][1] == "repeats a closed thread"


def test_thread_resolved_by_a_person_suppresses():
    original = finding()
    kept, dropped = psf.filter_findings([original], threads=[thread_for(original, resolved=True)])
    assert kept == [] and dropped[0][1] == "repeats a closed thread"


def test_new_finding_is_kept_next_to_unrelated_threads():
    old = finding()
    new = finding("Security", "The webhook secret is logged in plain text at startup.",
                  path="src/server.py")
    kept, dropped = psf.filter_findings([new], threads=[thread_for(old, resolved=True)])
    assert kept == [new] and dropped == []


def test_repeat_suppression_can_be_turned_off():
    original = finding()
    kept, _ = psf.filter_findings([original], threads=[thread_for(original, resolved=True)],
                                  suppress_repeats=False)
    assert kept == [original]


def test_dismissal_survives_a_new_round_with_a_different_line_number():
    # The thread was left on lines 10-12; the model now reports it on line 40.
    original = finding()
    moved = dict(finding(), start_line=40, end_line=41)
    assert psf.finding_matches_thread(moved, thread_for(original, replied=True))


# ── prediction rewrite ────────────────────────────────────────────

def test_prediction_is_untouched_when_nothing_is_dropped():
    pytest.importorskip("yaml")
    text = "review:\n  key_issues_to_review: []\n  security_concerns: 'No'\n"
    calls = []

    def load(*a, **k):
        import yaml
        return yaml.safe_load(a[0])

    out, dropped = psf._filter_prediction(text, load, lambda f: (f, calls.append(1) or []))
    assert out == text and dropped == []


def test_prediction_is_rewritten_with_only_the_kept_findings():
    yaml = pytest.importorskip("yaml")
    keep, nit = finding("Possible Bug"), finding("Naming", "rename x", path="src/b.py")
    text = yaml.safe_dump({"review": {"estimated_effort_to_review_[1-5]": 2,
                                      "key_issues_to_review": [keep, nit],
                                      "security_concerns": "No"}}, sort_keys=False)

    def load(t, **k):
        return yaml.safe_load(t)

    out, dropped = psf._filter_prediction(
        text, load, lambda f: psf.filter_findings(f))
    data = yaml.safe_load(out)
    assert data["review"]["key_issues_to_review"] == [keep]
    assert data["review"]["security_concerns"] == "No"
    assert list(data["review"]) == ["estimated_effort_to_review_[1-5]",
                                    "key_issues_to_review", "security_concerns"]
    assert [f["issue_header"] for f, _ in dropped] == ["Naming"]


def test_prediction_with_everything_dropped_reads_as_an_explicit_empty_list():
    yaml = pytest.importorskip("yaml")
    text = yaml.safe_dump({"review": {"key_issues_to_review": [finding("Typo", "x")],
                                      "security_concerns": "No"}}, sort_keys=False)
    out, _ = psf._filter_prediction(text, lambda t, **k: yaml.safe_load(t),
                                    lambda f: psf.filter_findings(f))
    findings, valid = psf._normalize_findings(
        yaml.safe_load(out)["review"]["key_issues_to_review"])
    assert findings == [] and valid is True


def test_unparseable_or_oddly_shaped_prediction_is_left_alone():
    assert psf._filter_prediction("junk", lambda t, **k: None, lambda f: (f, [])) == ("junk", [])
    assert psf._filter_prediction("x", lambda t, **k: {"review": "No"}, lambda f: (f, [])) == ("x", [])
    odd = {"review": {"key_issues_to_review": [finding(), "stray"]}}
    assert psf._filter_prediction("x", lambda t, **k: odd, lambda f: (f, [(f[0], "r")])) == ("x", [])


# ── reading existing threads ──────────────────────────────────────

class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p


class _Provider:
    repo = "acme/widgets"


def test_fetch_argus_threads_classifies_resolved_replied_and_judged(monkeypatch):
    import requests
    bot, human = {"login": "argus-review"}, {"login": "alice"}
    nodes = [
        {"isResolved": True, "resolvedBy": human, "path": "a.py", "comments": {"nodes": [
            {"author": bot, "body": "finding A"}]}},
        {"isResolved": True, "resolvedBy": bot, "path": "e.py", "comments": {"nodes": [
            {"author": bot, "body": "finding E"}]}},
        {"isResolved": True, "resolvedBy": None, "path": "f.py", "comments": {"nodes": [
            {"author": bot, "body": "finding F"}]}},
        {"isResolved": False, "path": "b.py", "comments": {"nodes": [
            {"author": bot, "body": "finding B"}, {"author": human, "body": "no, because X"}]}},
        {"isResolved": False, "path": "c.py", "comments": {"nodes": [
            {"author": bot, "body": "finding C"},
            {"author": bot, "body": "❓ Follow-up — please explain"}]}},
        {"isResolved": False, "path": "d.py", "comments": {"nodes": [
            {"author": human, "body": "a human-started thread"}]}},
    ]
    monkeypatch.setattr(psf, "_get_github_token", lambda provider: "t")
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp(
        {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}}))
    threads = psf._fetch_argus_threads(_Provider(), 5)
    assert [(t["path"], t["resolved"], t["resolved_by_human"], t["replied"], t["judged"])
            for t in threads] == [
        ("a.py", True, True, False, False),
        ("e.py", True, False, False, False),   # closed by Argus itself
        ("f.py", True, False, False, False),   # resolver unknown: not treated as a person
        ("b.py", False, False, True, False),
        ("c.py", False, False, False, True)]


def test_fetch_argus_threads_returns_none_on_any_failure(monkeypatch):
    import requests
    monkeypatch.setattr(psf, "_get_github_token", lambda provider: "t")
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp({"errors": [{"message": "x"}]}))
    assert psf._fetch_argus_threads(_Provider(), 5) is None
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp({}, status=502))
    assert psf._fetch_argus_threads(_Provider(), 5) is None
    monkeypatch.setattr(psf, "_get_github_token", lambda provider: None)
    assert psf._fetch_argus_threads(_Provider(), 5) is None
