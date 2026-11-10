"""Old REQUEST_CHANGES reviews left by earlier Argus versions are dismissed (Argus #49)."""

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import requests  # noqa: E402

import patch_suggestion_format as psf  # noqa: E402


class Resp:
    def __init__(self, payload=None, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p


class Provider:
    repo = "acme/widgets"


def review(rid, login, state, kind=None):
    kind = kind or ("Bot" if login.endswith("[bot]") else "User")
    return {"id": rid, "user": {"login": login, "type": kind}, "state": state}


def wire(monkeypatch, reviews, put_status=200, fail_put_for=()):
    puts = []
    monkeypatch.setattr(psf, "_get_github_token", lambda provider: "tok")
    monkeypatch.setattr(requests, "get", lambda url, **k: Resp(reviews))

    def put(url, headers=None, json=None, timeout=None):
        puts.append((url, json))
        if any(url.endswith(f"/{rid}/dismissals") for rid in fail_put_for):
            return Resp(status=403)
        return Resp(status=put_status)

    monkeypatch.setattr(requests, "put", put)
    return puts


def test_only_argus_change_requests_are_dismissed(monkeypatch):
    puts = wire(monkeypatch, [
        review(1, "argus-review[bot]", "CHANGES_REQUESTED"),
        review(2, "argus-review[bot]", "COMMENTED"),
        review(3, "argus-review[bot]", "APPROVED"),
        review(4, "alice", "CHANGES_REQUESTED"),                  # a person's review: untouched
        review(5, "argus-review[bot]", "DISMISSED"),
        review(6, "argus-review", "CHANGES_REQUESTED"),           # a person named like the bot: untouched
        review(7, "argus-review[bot]", "CHANGES_REQUESTED", "User"),  # not an App account: untouched
        review(8, "argus-review[bot]", "CHANGES_REQUESTED"),
    ])
    assert psf._dismiss_stale_change_requests(Provider(), 9) == 2
    assert [u for u, _ in puts] == [
        "https://api.github.com/repos/acme/widgets/pulls/9/reviews/1/dismissals",
        "https://api.github.com/repos/acme/widgets/pulls/9/reviews/8/dismissals"]
    assert all(body == {"message": psf.DISMISS_MESSAGE} for _, body in puts)


def test_a_failed_dismissal_is_skipped_and_the_rest_still_run(monkeypatch):
    wire(monkeypatch, [review(1, "argus-review[bot]", "CHANGES_REQUESTED"),
                       review(2, "argus-review[bot]", "CHANGES_REQUESTED")], fail_put_for=(1,))
    assert psf._dismiss_stale_change_requests(Provider(), 9) == 1


def test_listing_failure_or_missing_token_dismisses_nothing(monkeypatch):
    wire(monkeypatch, [])
    monkeypatch.setattr(requests, "get", lambda url, **k: Resp([], status=500))
    assert psf._dismiss_stale_change_requests(Provider(), 9) == 0
    monkeypatch.setattr(psf, "_get_github_token", lambda provider: None)
    assert psf._dismiss_stale_change_requests(Provider(), 9) == 0


def test_a_network_error_while_dismissing_is_logged_not_raised(monkeypatch):
    wire(monkeypatch, [review(1, "argus-review[bot]", "CHANGES_REQUESTED")])

    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "put", boom)
    assert psf._dismiss_stale_change_requests(Provider(), 9) == 0


def test_no_change_requests_means_no_calls(monkeypatch):
    puts = wire(monkeypatch, [review(1, "argus-review[bot]", "COMMENTED")])
    assert psf._dismiss_stale_change_requests(Provider(), 9) == 0 and puts == []
