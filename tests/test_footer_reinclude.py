"""Unit tests for the config-dotfile footer re-include in
build_review_body_additions (Line A / Mercury #476).

pr-agent's is_valid_file() drops config dotfiles like .gitignore (their
split('.')[-1] token is in bad_extensions.default), so they never reach
diff_files and are omitted from the '📒 Files reviewed' footer. The new
extra_filenames param re-includes an allowlist of such dotfiles in the footer
for DISPLAY ONLY (content is never sent to the model).

build_review_body_additions is a pure formatter (no network), so these run
without pr_agent / GitHub access.
"""

import sys
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import patch_suggestion_format as psf  # noqa: E402


class _FakeFile:
    def __init__(self, filename):
        self.filename = filename


def test_none_reproduces_prepatch_footer_byte_for_byte():
    """Regression guard: extra_filenames=None (the default) must produce a
    footer identical to omitting the argument — i.e. byte-for-byte unchanged
    behavior for every existing caller."""
    diff = [_FakeFile("a.py"), _FakeFile("b.py")]
    body_omitted = psf.build_review_body_additions([], 0, diff)
    body_none = psf.build_review_body_additions([], 0, diff, extra_filenames=None)
    assert body_omitted == body_none
    # the reviewed files appear; no dotfile leaked in
    assert "* `a.py`" in body_none
    assert "Files reviewed (2)" in body_none
    assert ".gitignore" not in body_none


def test_extra_filename_is_added_to_footer():
    diff = [_FakeFile("a.py")]
    body = psf.build_review_body_additions([], 0, diff, extra_filenames=[".gitignore"])
    assert "* `a.py`" in body
    assert "* `.gitignore`" in body
    assert "Files reviewed (2)" in body


def test_extra_filename_dedup():
    """A dotfile already present in diff_files must not be duplicated."""
    diff = [_FakeFile(".gitignore"), _FakeFile("a.py")]
    body = psf.build_review_body_additions([], 0, diff, extra_filenames=[".gitignore"])
    assert body.count("* `.gitignore`") == 1
    assert "Files reviewed (2)" in body


def test_thirty_file_cap_is_respected():
    """The footer caps at 30 files; extras are not appended past the cap so a
    genuinely reviewed file is never evicted."""
    diff = [_FakeFile(f"f{i}.py") for i in range(30)]
    body = psf.build_review_body_additions([], 0, diff, extra_filenames=[".gitignore"])
    assert "Files reviewed (30)" in body
    assert "* `.gitignore`" not in body  # cap full -> extra not added


def test_empty_and_malformed_extra_is_noop():
    diff = [_FakeFile("a.py")]
    base = psf.build_review_body_additions([], 0, diff)
    assert psf.build_review_body_additions([], 0, diff, extra_filenames=[]) == base
    # falsy entries are skipped without error
    body = psf.build_review_body_additions([], 0, diff, extra_filenames=["", None])
    assert body == base
