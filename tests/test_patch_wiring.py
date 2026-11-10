"""Wiring tests against the real PR-Agent classes (Argus #49).

They need the pr-agent package (present in the Docker image, absent from a bare
developer environment) and skip themselves when it is not importable. Network
and the model are never touched: the PRReviewer constructor and the GitHub
reads are replaced with stubs, so what is checked is only that Argus's patches
attach to the real class attributes and behave as intended.
"""

import pathlib
import sys
from types import SimpleNamespace

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pytest.importorskip("pr_agent")

from pr_agent.tools import pr_reviewer  # noqa: E402
from pr_agent.git_providers import github_provider  # noqa: E402

import argus_context  # noqa: E402
import patch_suggestion_format as psf  # noqa: E402


@pytest.fixture
def patched(monkeypatch):
    """Apply the review patches to the real classes, restoring them afterwards."""
    cls = pr_reviewer.PRReviewer

    def stub_init(self, pr_url, is_answer=False, is_auto=False, args=None, ai_handler=None):
        self.git_provider = SimpleNamespace(
            repo="acme/widgets", pr=SimpleNamespace(base=SimpleNamespace(ref="release")))
        self.vars = {"extra_instructions": "Reply in Chinese."}
        self.token_handler = SimpleNamespace(encoder=SimpleNamespace(encode=lambda t: t.split()))
        self.prediction = None

    for name in ("__init__", "parse_incremental", "_prepare_pr_review", "run"):
        monkeypatch.setattr(cls, name, cls.__dict__[name])  # restored on teardown
    monkeypatch.setattr(cls, "__init__", stub_init)
    monkeypatch.setattr(github_provider.GithubProvider, "auto_approve",
                        github_provider.GithubProvider.__dict__["auto_approve"])
    for name in ("publish_persistent_comment", "publish_comment"):
        monkeypatch.setattr(github_provider.GithubProvider, name,
                            github_provider.GithubProvider.__dict__[name])
    psf.apply_patch()
    return cls


def test_dash_i_is_ignored_so_every_review_covers_the_whole_pr(patched):
    reviewer = patched.__new__(patched)
    assert patched.parse_incremental(reviewer, ["-i"]).is_incremental is False
    assert patched.parse_incremental(reviewer, []).is_incremental is False
    assert patched.parse_incremental(reviewer, None).is_incremental is False


def test_pr_agent_auto_approve_is_disabled(patched):
    assert github_provider.GithubProvider.auto_approve(object()) is False


def test_project_context_is_appended_and_the_token_budget_recomputed(patched, monkeypatch):
    seen = {}

    class FakeTokenHandler:
        def __init__(self, pr, vars, system, user):
            seen["vars"] = dict(vars)

    monkeypatch.setattr("pr_agent.algo.token_handler.TokenHandler", FakeTokenHandler)
    monkeypatch.setattr(argus_context, "load_review_context",
                        lambda provider, token_getter, **kw: argus_context.ContextResult(text="PROJECT RULES"))
    reviewer = patched.__new__(patched)
    patched.__init__(reviewer, "https://github.com/acme/widgets/pull/1")
    extra = reviewer.vars["extra_instructions"]
    assert extra.startswith("Reply in Chinese.")
    assert "PROJECT RULES" in extra and "whole pull request diff" in extra
    assert isinstance(reviewer.token_handler, FakeTokenHandler)
    assert "PROJECT RULES" in seen["vars"]["extra_instructions"]


def test_a_context_failure_leaves_the_review_running(patched, monkeypatch):
    def boom(provider, token_getter, **kw):
        raise RuntimeError("github down")

    monkeypatch.setattr(argus_context, "load_review_context", boom)
    reviewer = patched.__new__(patched)
    patched.__init__(reviewer, "https://github.com/acme/widgets/pull/1")
    assert reviewer.vars["extra_instructions"] == "Reply in Chinese."
    assert not isinstance(reviewer.token_handler, type(None))
    assert hasattr(reviewer.token_handler, "encoder")  # still the original handler


def test_filters_run_before_pr_agent_renders_the_review(patched, monkeypatch):
    yaml = pytest.importorskip("yaml")
    keep = {"relevant_file": "a.py", "issue_header": "Possible Bug",
            "issue_content": "Division by zero when the list is empty.", "start_line": 1, "end_line": 2}
    nit = {"relevant_file": "b.py", "issue_header": "Naming", "issue_content": "rename", "start_line": 1, "end_line": 1}
    prediction = yaml.safe_dump({"review": {"key_issues_to_review": [keep, nit],
                                            "security_concerns": "No"}}, sort_keys=False)
    rendered = {}

    def original_prepare(self):
        rendered["prediction"] = self.prediction
        return "markdown"

    # Re-apply on top of a recording stand-in for PR-Agent's own renderer.
    monkeypatch.setattr(patched, "_prepare_pr_review", original_prepare)
    psf.apply_patch()
    monkeypatch.setattr(psf, "_fetch_argus_threads", lambda provider, pr_num: [])
    reviewer = patched.__new__(patched)
    reviewer.git_provider = SimpleNamespace(pr_num=3)
    reviewer.prediction = prediction
    reviewer._argus_ctx = argus_context.ContextResult(
        config=argus_context.ReviewConfig(skip_paths=("gen/**",)))
    assert patched._prepare_pr_review(reviewer) == "markdown"
    kept = yaml.safe_load(rendered["prediction"])["review"]["key_issues_to_review"]
    assert [f["relevant_file"] for f in kept] == ["a.py"]


def test_context_gets_a_token_budget_from_max_model_tokens(patched, monkeypatch):
    from pr_agent.config_loader import get_settings
    seen = {}

    def loader(provider, token_getter, max_tokens=None, count_tokens=None):
        seen["max_tokens"] = max_tokens
        seen["count"] = count_tokens("a b c")
        return argus_context.ContextResult()

    class FakeTokenHandler:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr("pr_agent.algo.token_handler.TokenHandler", FakeTokenHandler)
    monkeypatch.setattr(argus_context, "load_review_context", loader)
    reviewer = patched.__new__(patched)
    patched.__init__(reviewer, "https://github.com/acme/widgets/pull/1")
    assert seen["max_tokens"] == int(get_settings().config.max_model_tokens * 0.25)
    assert seen["count"] == 3  # counted with the reviewer's own token encoder
