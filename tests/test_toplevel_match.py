"""Unit tests for the Strategy 4 top-level-comment matching helpers in
patch_suggestion_format.py (Line B / Mercury #476).

These exercise the three PURE helpers that decide whether a top-level PR
comment rebuts a given review thread:

    _tokenize_for_match
    _extract_thread_match_keys
    _match_toplevel_comment_to_thread

They take no network / LLM, so no pr_agent stubbing is needed (the heavy
imports in patch_suggestion_format are lazy/inside functions). Finding bodies
are generated with the real format_review_finding_body() so the tests stay
faithful to the production thread layout.

The dominant risk for Strategy 4 is auto-resolving a GENUINE unaddressed
blocker via a loose match, so the matcher requires BOTH an independent
file-path signal AND a finding-header signal. The disambiguation + independence
tests below guard exactly that.
"""

import sys
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import patch_suggestion_format as psf  # noqa: E402


FILE_A = "scripts/gh-project-flow.md"

# Finding A — cache/writeback theme
FINDING_A = psf.format_review_finding_body({
    "issue_header": "Verify cache before writeback",
    "issue_content": ("The propagate step reads stale ranking data and "
                      "overwrites fresh writes without a guard."),
    "relevant_file": FILE_A,
    "start_line": 40,
    "end_line": 44,
})

# Finding B — SAME file, deliberately DISJOINT vocabulary from A
FINDING_B = psf.format_review_finding_body({
    "issue_header": "Hardcoded mount location",
    "issue_content": ("Replace the absolute NAS directory with an environment "
                      "variable lookup."),
    "relevant_file": FILE_A,
    "start_line": 88,
    "end_line": 88,
})

# A top-level PR comment rebutting Finding A (quotes the file + A's vocabulary)
COMMENT_REBUTS_A = (
    "DISAGREE — on `scripts/gh-project-flow.md` the cache writeback is "
    "actually guarded: `verify_cache()` runs before propagate, so this is a "
    "false positive."
)


def test_tokenize_basics():
    toks = psf._tokenize_for_match("Verify the CACHE_before  writeback!!")
    assert "verify" in toks
    assert "cache_before" in toks  # underscore kept as one token
    assert "writeback" in toks
    assert "the" not in toks       # <3 chars dropped
    assert psf._tokenize_for_match("") == set()
    assert psf._tokenize_for_match(None) == set()


def test_header_tokens_exclude_path_and_boilerplate():
    """Core correctness guard: header_tokens must be INDEPENDENT of the path
    signal and must NOT leak the <details> agent-prompt boilerplate."""
    header_tokens, path_source = psf._extract_thread_match_keys(
        FINDING_A, FILE_A)
    assert path_source == FILE_A
    # path tokens are SUBTRACTED out of header_tokens (independence)
    assert "scripts" not in header_tokens
    assert "flow" not in header_tokens
    assert "project" not in header_tokens
    # agent-prompt boilerplate lives in the <details> block -> excluded
    for boiler in ("investigate", "described", "above", "action", "required"):
        assert boiler not in header_tokens, boiler
    # genuine finding vocabulary survives
    for kw in ("cache", "writeback", "propagate", "guard"):
        assert kw in header_tokens, kw


def test_positive_match():
    keys = psf._extract_thread_match_keys(FINDING_A, FILE_A)
    assert psf._match_toplevel_comment_to_thread(COMMENT_REBUTS_A, *keys) is True


def test_file_path_miss():
    """Same finding vocabulary but the comment names a DIFFERENT file with no
    shared path token -> path signal absent -> no match."""
    keys = psf._extract_thread_match_keys(FINDING_A, FILE_A)
    comment = ("The cache writeback before propagate is guarded in "
               "`src/unrelated/other_module.py`.")
    assert psf._match_toplevel_comment_to_thread(comment, *keys) is False


def test_generic_path_word_is_not_a_path_signal():
    """A comment that uses a generic directory word ('scripts') from the path
    but does NOT name the file (no full path, no basename) must NOT satisfy the
    file-path signal — even with strong header overlap. Guards the dominant
    false-resolve risk surfaced by the Codex audit (Line B)."""
    keys = psf._extract_thread_match_keys(FINDING_A, FILE_A)
    comment = ("Lots of scripts in this PR; the cache writeback before "
               "propagate logic is fine and already guarded.")
    assert psf._match_toplevel_comment_to_thread(comment, *keys) is False


def test_header_miss():
    """Right file, but a generic comment with <2 finding-token overlap -> no
    match (header signal below threshold)."""
    keys = psf._extract_thread_match_keys(FINDING_A, FILE_A)
    comment = "Thanks for the review of `scripts/gh-project-flow.md`, looks good to me."
    assert psf._match_toplevel_comment_to_thread(comment, *keys) is False


def test_same_file_disambiguation():
    """Two findings on the SAME file with disjoint vocab: a comment rebutting A
    must match A's thread and NOT B's thread. Guards against same-file
    cross-matching (the dominant false-resolve risk)."""
    keys_a = psf._extract_thread_match_keys(FINDING_A, FILE_A)
    keys_b = psf._extract_thread_match_keys(FINDING_B, FILE_A)
    assert psf._match_toplevel_comment_to_thread(COMMENT_REBUTS_A, *keys_a) is True
    assert psf._match_toplevel_comment_to_thread(COMMENT_REBUTS_A, *keys_b) is False


def test_basename_only_path_reference():
    """A comment that names just the basename (no dir prefix) still satisfies
    the file-path signal via the basename branch."""
    keys = psf._extract_thread_match_keys(FINDING_A, FILE_A)
    comment = ("On gh-project-flow.md the cache writeback before propagate is "
               "already guarded.")
    assert psf._match_toplevel_comment_to_thread(comment, *keys) is True


def test_path_fallback_from_body():
    """thread_path empty -> path_source is recovered from the `In file \\`...\\``
    reference inside the finding body."""
    header_tokens, path_source = psf._extract_thread_match_keys(
        FINDING_A, "")
    assert path_source == FILE_A
    assert psf._match_toplevel_comment_to_thread(
        COMMENT_REBUTS_A, header_tokens, path_source) is True


def test_empty_comment_is_no_match():
    keys = psf._extract_thread_match_keys(FINDING_A, FILE_A)
    assert psf._match_toplevel_comment_to_thread("", *keys) is False
    assert psf._match_toplevel_comment_to_thread(None, *keys) is False


def test_filename_mentioned_boundaries():
    """_filename_mentioned matches a filename only as a BOUNDED token, so a name
    is never matched as the prefix/suffix of a longer name (#476 Line B)."""
    fm = psf._filename_mentioned
    assert fm("foo.py", "the bug is in foo.py here") is True
    assert fm("foo.py", "see `foo.py` for details") is True       # backtick boundary
    assert fm("foo.py", "patched in dir/foo.py already") is True   # path-sep boundary
    assert fm("foo.py", "foo.py.bak is the stale copy") is False   # trailing '.' extension
    assert fm("foo.py", "myfoo.py is a different file") is False   # leading alnum
    assert fm("foo.py", "the compiled foo.pyc is fine") is False   # trailing alnum
    assert fm("", "anything") is False
    assert fm("foo.py", "no mention of it") is False


def test_sibling_backup_filename_does_not_false_match():
    """A comment that names a CONFUSINGLY-similar sibling (the thread's file plus
    a '.bak' suffix) must NOT satisfy the path signal, even with full header
    overlap — the basename/full-path must match as a bounded token (#476 Line B)."""
    keys = psf._extract_thread_match_keys(FINDING_A, FILE_A)
    comment = ("DISAGREE — in the backup `scripts/gh-project-flow.md.bak` the "
               "cache writeback before propagate is already guarded.")
    assert psf._match_toplevel_comment_to_thread(comment, *keys) is False


def test_prefixed_filename_does_not_false_match():
    """A filename whose basename is only a SUFFIX of a longer name must not match
    (the char before the basename is alphanumeric) (#476 Line B)."""
    keys = psf._extract_thread_match_keys(FINDING_A, FILE_A)
    comment = ("The cache writeback before propagate logic in "
               "`xgh-project-flow.md` is guarded.")
    assert psf._match_toplevel_comment_to_thread(comment, *keys) is False


def test_none_path_source_does_not_crash():
    """path_source=None must NOT raise (None.rsplit) — the basename branch
    guards with (path_source or '') and returns no match (#476 Line B)."""
    header_tokens, _ = psf._extract_thread_match_keys(FINDING_A, FILE_A)
    assert psf._match_toplevel_comment_to_thread(
        COMMENT_REBUTS_A, header_tokens, None) is False
