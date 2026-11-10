"""Argus #58: what the replay of Mercury #475 showed.

1. PR-Agent drops .gitignore from the diff (its bad-extension list), so a review never saw ignore-rule changes.
2. A review a person asked for on an unchanged head dropped everything it found ("no new code").
"""
import pathlib
import sys
from types import SimpleNamespace

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import argus_context as ac  # noqa: E402


# ── .gitignore reaches the review ─────────────────────────────────

class FakeSettings:
    def __init__(self, values):
        self.values = dict(values)

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


def test_gitignore_is_removed_from_both_bad_extension_lists():
    s = FakeSettings({"bad_extensions.default": ["exe", "gitignore", "png"],
                      "bad_extensions.extra": ["GitIgnore", "lock"]})
    removed = ac.keep_reviewed_extensions(s)
    assert sorted(x.lower() for x in removed) == ["gitignore", "gitignore"]
    assert s.get("bad_extensions.default") == ["exe", "png"]
    assert s.get("bad_extensions.extra") == ["lock"]


def test_nothing_changes_when_the_list_has_no_gitignore():
    s = FakeSettings({"bad_extensions.default": ["exe"]})
    assert ac.keep_reviewed_extensions(s) == []
    assert s.get("bad_extensions.default") == ["exe"]


def test_real_pr_agent_settings_let_a_gitignore_change_through():
    pytest.importorskip("pr_agent")
    from pr_agent.config_loader import get_settings
    from pr_agent.algo.language_handler import filter_bad_extensions, is_valid_file

    settings = get_settings()
    saved = list(settings.get("bad_extensions.default"))
    saved_extra = list(settings.get("bad_extensions.extra") or [])
    try:
        assert not is_valid_file(".gitignore", list(saved))       # PR-Agent's default drops it
        ac.keep_reviewed_extensions(settings)
        files = [SimpleNamespace(filename=".gitignore"), SimpleNamespace(filename="logo.png")]
        assert [f.filename for f in filter_bad_extensions(files)] == [".gitignore"]
    finally:
        settings.set("bad_extensions.default", saved)
        settings.set("bad_extensions.extra", saved_extra)


# ── a requested review reports on an unchanged head ───────────────

def test_only_an_automatic_run_drops_findings_on_an_unchanged_head():
    import patch_suggestion_format as psf
    assert psf.should_drop_for_no_new_code(True, True) is True
    assert psf.should_drop_for_no_new_code(True, False) is False     # a person asked: report
    assert psf.should_drop_for_no_new_code(False, True) is False
    assert psf.should_drop_for_no_new_code(False, False) is False


def test_auto_command_flag_comes_from_pr_agent_settings():
    pytest.importorskip("pr_agent")
    import patch_suggestion_format as psf
    from pr_agent.config_loader import get_settings
    settings = get_settings()
    saved = settings.config.get("is_auto_command", False)
    try:
        settings.set("config.is_auto_command", False)
        assert psf._is_auto_command() is False                        # a comment-triggered review
        settings.set("config.is_auto_command", True)
        assert psf._is_auto_command() is True                         # PR opened or pushed
    finally:
        settings.set("config.is_auto_command", saved)


def test_position_alone_no_longer_drops_a_finding():
    """The former 'Fix C' (drop anything within a few lines of an open thread) is gone."""
    source = (REPO_ROOT / "patch_suggestion_format.py").read_text(encoding="utf-8")
    assert "Dedup: dropped" not in source
    assert "for off in range(-5, 6)" not in source


def test_a_requested_review_with_findings_on_an_unchanged_head_does_not_say_no_new_code():
    import patch_suggestion_format as psf
    major = {"issue_header": "Major: x", "issue_content": "y", "relevant_file": "a.py"}
    effective = psf.should_drop_for_no_new_code(True, False)      # unchanged head, a person asked
    event, reason = psf._decide_review_event([major], [], 2, has_inline_comments=True,
                                             no_new_code=effective, thread_state_trusted=True,
                                             findings_valid=True)
    assert "no new code" not in reason
    assert "critical/major" in reason
