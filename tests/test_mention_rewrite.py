"""Unit tests for mention_rewrite.py — the @argus-review → slash-command
intent classifier (Argus #23).

The bug these guard: a re-review trigger written as a fix-summary body followed
by a trailing ``@argus-review review`` line was misclassified as a question and
routed to ``/ask`` (because the OLD classifier looked at the first word of the
whole stripped body), so the intended incremental ``/review`` never fired and
the PR stalled in a nit-loop (Mercury PR #253). The classifier now keys off the
verb that FOLLOWS a mention, with the LAST explicit-command mention winning.

Pure functions — no pr_agent / network, so no stubbing needed.
"""

import sys
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mention_rewrite import should_rewrite_mention, rewrite_mention  # noqa: E402


# ── should_rewrite_mention ────────────────────────────────────────

def test_should_rewrite_direct_mention():
    assert should_rewrite_mention("@argus-review review") is True


def test_should_rewrite_bot_suffix():
    assert should_rewrite_mention("@argus-review[bot] review") is True


def test_should_not_rewrite_no_mention():
    assert should_rewrite_mention("just a normal comment") is False


def test_should_not_rewrite_empty():
    assert should_rewrite_mention("") is False
    assert should_rewrite_mention(None) is False


def test_should_not_rewrite_quote_only():
    body = "> @argus-review review\n\nI disagree with this finding."
    assert should_rewrite_mention(body) is False


def test_should_rewrite_mixed_quote_and_direct():
    body = "> @argus-review said earlier\n\n@argus-review review"
    assert should_rewrite_mention(body) is True


# ── rewrite_mention: existing conventions still hold ──────────────

def test_explicit_review():
    assert rewrite_mention("@argus-review review") == ("/review", "explicit-trailing-verb")


def test_explicit_review_incremental_flag():
    assert rewrite_mention("@argus-review review -i") == ("/review -i", "explicit-trailing-verb")


def test_explicit_slash_form():
    assert rewrite_mention("@argus-review /review") == ("/review", "explicit-trailing-verb")


def test_explicit_ask_verb():
    cmd, method = rewrite_mention("@argus-review ask why is the cache stale?")
    assert cmd == "/ask why is the cache stale?"
    assert method == "explicit-trailing-verb"


def test_freeform_question_falls_back_to_ask():
    assert rewrite_mention("@argus-review why is X bad?") == (
        "/ask why is X bad?", "freeform-ask-fallback")


# ── rewrite_mention: the #23 regression cases ─────────────────────

def test_trailing_review_after_fix_summary():
    """A fix summary as context + a trailing review trigger must route to /review,
    not /ask. This is the exact failure mode from Mercury PR #253."""
    body = (
        "iter-4 3 findings 处理 (commit 0677596):\n\n"
        "1. **🔵 语义冲突** — fixed by renaming the var\n"
        "2. **🟡 perf** — added a guard\n\n"
        "@argus-review review"
    )
    assert rewrite_mention(body) == ("/review", "explicit-trailing-verb")


def test_leading_and_trailing_mentions_last_wins():
    """Two mentions: a leading context mention (non-command verb) and a trailing
    explicit review trigger. The trailing explicit command wins."""
    body = (
        "@argus-review iter-4 3 findings 处理 (commit 0677596):\n\n"
        "1. **🔵 语义冲突** — resolved\n\n"
        "@argus-review review"
    )
    assert rewrite_mention(body) == ("/review", "explicit-trailing-verb")


def test_trailing_review_incremental_after_body():
    body = "Fixed all the nits.\n\n@argus-review review -i"
    assert rewrite_mention(body) == ("/review -i", "explicit-trailing-verb")


def test_quoted_trailing_trigger_is_ignored():
    """A review verb inside a quote is not a real trigger; the genuine freeform
    mention drives the result."""
    body = "@argus-review what changed?\n\n> @argus-review review"
    cmd, method = rewrite_mention(body)
    assert cmd.startswith("/ask")
    assert method == "freeform-ask-fallback"


def test_last_explicit_command_wins_over_earlier():
    body = "@argus-review describe\n\n@argus-review review"
    assert rewrite_mention(body) == ("/review", "explicit-trailing-verb")


# ── rewrite_mention: edge cases ───────────────────────────────────

def test_empty_after_strip():
    assert rewrite_mention("@argus-review") == ("", "empty")


def test_none_and_empty_body_safe():
    # rewrite_mention is independently safe even though the call site gates on
    # should_rewrite_mention (which already rejects None/empty).
    assert rewrite_mention(None) == ("", "empty")
    assert rewrite_mention("") == ("", "empty")


def test_unknown_slash_passthrough():
    cmd, method = rewrite_mention("@argus-review /custom_thing foo")
    assert cmd == "/custom_thing foo"
    assert method == "slash-passthrough"


def test_case_insensitive_mention():
    assert rewrite_mention("@Argus-Review review") == ("/review", "explicit-trailing-verb")


def test_freeform_multiword_question_preserved():
    cmd, method = rewrite_mention("@argus-review is this thread safe under concurrency?")
    assert cmd == "/ask is this thread safe under concurrency?"
    assert method == "freeform-ask-fallback"
