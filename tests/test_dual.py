"""Tests for argus_dual (Mercury #821): the Claude call and its failure modes, the
merge of the two answers, the inline cap, the comparison section, and the budget
guard letting /review run on the Claude line alone."""

import asyncio
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

yaml = pytest.importorskip("yaml")

import argus_budget as ab  # noqa: E402
import argus_dual as ad  # noqa: E402
import patch_suggestion_format as psf  # noqa: E402

JST = timezone(timedelta(hours=9))
CLAUDE_CFG = ad.parse_claude_budget({"monthly_usd": 10.0})
GPT_CFG = ab.BudgetConfig(monthly_usd=10.0)
DUAL = ad.DualConfig(enabled=True)


def run(coro):
    return asyncio.run(coro)


def load_yaml(text, **_):
    return yaml.safe_load(text)


@pytest.fixture
def clock():
    return lambda: datetime(2026, 11, 20, 12, tzinfo=JST)


@pytest.fixture
def ledger(tmp_path, clock):
    return ab.BudgetLedger(path=str(tmp_path / "budget-anthropic.json"), config=CLAUDE_CFG, clock=clock)


@pytest.fixture(autouse=True)
def event_sink(tmp_path, monkeypatch):
    monkeypatch.setattr(ab.emitter, "_path", str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(ab.emitter, "_ready", None)


@pytest.fixture(autouse=True)
def no_alternate():
    yield
    ab.set_review_alternate(None)


def finding(path, start, end, header="Possible Bug", content="列表为空时除以零。"):
    return {"relevant_file": path, "issue_header": header, "issue_content": content,
            "start_line": start, "end_line": end}


def answer(findings, security="No"):
    return yaml.safe_dump({"review": {"estimated_effort_to_review_[1-5]": 2,
                                      "key_issues_to_review": findings,
                                      "security_concerns": security}},
                          sort_keys=False, allow_unicode=True)


# ── configuration ─────────────────────────────────────────────────

def test_dual_review_is_off_unless_enabled_is_exactly_true():
    assert ad.parse_dual_config(None).enabled is False
    assert ad.parse_dual_config({"enabled": "yes"}).enabled is False
    cfg = ad.parse_dual_config({"ENABLED": True, "model": "claude-opus-5-5"})
    assert cfg.enabled is True and cfg.model == "claude-opus-5-5" and cfg.effort == "high"


@pytest.mark.parametrize("raw", [{"max_tokens": 10}, {"max_tokens": True}, {"effort": "extreme"}])
def test_bad_dual_values_fall_back(raw):
    cfg = ad.parse_dual_config(dict(raw, enabled=True))
    assert cfg.max_tokens == 32000 and cfg.effort == "high"


def test_claude_ledger_uses_claude_prices_and_needs_its_own_cap():
    assert ad.parse_claude_budget(None).monthly_usd is None
    assert [p.model for p in ad.parse_claude_budget(None).prices] == ["claude-opus-5-5"]
    own = ad.parse_claude_budget({"prices": [{"model": "claude-x", "input": 1, "output": 2}]})
    assert [p.model for p in own.prices] == ["claude-x"]


def test_claude_cost_prices_cache_reads_and_writes_separately():
    price = ab.ModelPrice("claude-opus-5-5", 4.0, 0.20, 20.0)
    usage = {"input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000,
             "cache_creation_input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert ad.claude_cost(usage, price) == pytest.approx(4.0 + 0.20 + 5.0 + 20.0)
    assert ad.claude_cost({"input_tokens": 0, "output_tokens": 0}, price) is None
    assert ad.claude_cost(None, price) is None


# ── the Claude call ───────────────────────────────────────────────

class FakeStream:
    def __init__(self, outcome):
        self.outcome = outcome

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_final_message(self):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if callable(self.outcome):
            return await self.outcome()
        return self.outcome


class FakeClient:
    def __init__(self, outcome):
        self.calls = []
        self.closed = False
        outer = self

        class Messages:
            def stream(self, **kwargs):
                outer.calls.append(kwargs)
                return FakeStream(outcome)

        self.messages = Messages()

    async def close(self):
        self.closed = True


def message(text="review:\n  key_issues_to_review: []\n", stop="end_turn", output=1000):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking="..."), SimpleNamespace(type="text", text=text)],
        stop_reason=stop, usage={"input_tokens": 10_000, "output_tokens": output})


def call(ledger, outcome, api_key="k", timeout=300):
    client = FakeClient(outcome)
    result = run(ad.claude_review("sys", "user", DUAL, ledger, timeout, api_key=api_key,
                                  client_factory=lambda key, t: client))
    return result, client


def test_successful_call_sends_adaptive_thinking_and_effort_and_is_charged(ledger):
    text, client = call(ledger, message())
    assert text.startswith("review:")
    kwargs = client.calls[0]
    assert kwargs["model"] == "claude-opus-5-5" and kwargs["system"] == "sys"
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"] == {"effort": "high"}
    assert "temperature" not in kwargs and "budget_tokens" not in str(kwargs)
    assert client.closed
    spent, _ = ledger.snapshot()
    assert spent == pytest.approx((10_000 * 4.0 + 1000 * 20.0) / 1_000_000)


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_refusal_or_truncation_fails_the_line_but_is_still_charged(ledger, stop):
    with pytest.raises(ad.LineFailed) as e:
        call(ledger, message(stop=stop))
    assert e.value.status == stop
    assert ledger.snapshot()[0] > 0


def test_empty_answer_fails_the_line(ledger):
    with pytest.raises(ad.LineFailed) as e:
        call(ledger, message(text="  "))
    assert e.value.status == "empty"


def test_no_key_makes_no_call(ledger):
    with pytest.raises(ad.LineFailed) as e:
        run(ad.claude_review("s", "u", DUAL, ledger, 300, api_key="",
                             client_factory=lambda *a: pytest.fail("no client without a key")))
    assert e.value.status == "no_key"


def test_no_cap_makes_no_call(tmp_path, clock):
    uncapped = ab.BudgetLedger(path=str(tmp_path / "b.json"), config=ad.parse_claude_budget(None), clock=clock)
    with pytest.raises(ad.LineFailed) as e:
        run(ad.claude_review("s", "u", DUAL, uncapped, 300, api_key="k",
                             client_factory=lambda *a: pytest.fail("no client without a cap")))
    assert e.value.status == "budget"


class StatusError(Exception):
    def __init__(self, status_code, text):
        super().__init__(text)
        self.status_code = status_code
        self.body = {"error": {"message": text}}


@pytest.mark.parametrize("exc", [
    StatusError(402, "payment required"),
    StatusError(400, "You have reached your specified API usage limits."),
    StatusError(400, "Your credit balance is too low to access the Anthropic API."),
])
def test_billing_errors_mark_the_claude_period_used_up(ledger, exc):
    with pytest.raises(ad.LineFailed) as e:
        call(ledger, exc)
    assert e.value.status == "quota"
    assert ledger.is_exhausted()


@pytest.mark.parametrize("exc", [StatusError(429, "rate limited"), StatusError(529, "overloaded"),
                                 StatusError(400, "bad request")])
def test_other_errors_fail_the_line_without_using_up_the_budget(ledger, exc):
    with pytest.raises(ad.LineFailed) as e:
        call(ledger, exc)
    assert e.value.status == "error"
    assert not ledger.is_exhausted()


def test_timeout_is_charged_an_estimate_and_fails_the_line(ledger):
    async def slow():
        await asyncio.sleep(5)

    with pytest.raises(ad.LineFailed) as e:
        call(ledger, slow, timeout=-14.9)   # the overall bound is timeout + 15 s
    assert e.value.status == "timeout"
    assert ledger.snapshot()[0] > 0


# ── merging ───────────────────────────────────────────────────────

def test_nearby_chinese_findings_match_only_when_they_describe_the_same_defect():
    a = finding("src/a.py", 10, 12, content="items 为空列表时 total / len(items) 会除以零。")
    b = finding("./src/a.py", 13, 14, content="len(items) 为 0 时这里除以零并抛出异常。")
    assert ad.same_finding(a, b)
    assert not ad.same_finding(a, finding("src/b.py", 10, 12, content=a["issue_content"]))
    assert not ad.same_finding(a, finding("src/a.py", 40, 41, content="另一个问题。"))


def test_nearby_but_different_defects_stay_separate():
    null_crash = finding("a.py", 20, 20, content="user 可能为 None，访问 user.name 会崩溃。")
    permission = finding("a.py", 22, 22, header="Security", content="删除接口没有检查调用者的管理员权限。")
    assert not ad.same_finding(null_crash, permission)
    merged = ad.merge_findings([null_crash], [permission])
    assert sorted(f["argus_source"] for f in merged) == ["claude", "gpt"]


def test_findings_without_lines_match_on_shared_identifiers():
    a = finding("a.py", 10, 10, content="parse_config ignores reset_day overflow_guard")
    b = finding("a.py", None, None, content="reset_day overflow_guard in parse_config")
    assert ad.same_finding(a, b)


def test_distant_findings_stay_separate_even_with_shared_identifiers():
    a = finding("a.py", 10, 12, content="save_record swallows TimeoutError and retry_queue loses the item")
    b = finding("a.py", 80, 82, content="retry_queue drops save_record on TimeoutError in the fallback branch")
    assert not ad.same_finding(a, b)
    merged = ad.merge_findings([a], [b])
    assert sorted(f["argus_source"] for f in merged) == ["claude", "gpt"]


def test_merge_tags_sources_counts_and_ranks_by_severity_then_agreement():
    major, medium = "Possible Bug", "Edge Case"
    assert psf._finding_severity({"issue_header": major}) in ("Major", "Critical")
    assert psf._finding_severity({"issue_header": medium}) == "Medium"
    gpt = [finding("a.py", 1, 2, header=medium), finding("b.py", 5, 5, header=major)]
    claude = [finding("a.py", 2, 3, header=major), finding("c.py", 9, 9, header=medium)]
    text, stats = ad.merge_predictions(answer(gpt), answer(claude), load_yaml)
    merged = yaml.safe_load(text)["review"]["key_issues_to_review"]
    assert [(f["relevant_file"], f["argus_source"]) for f in merged] == [
        ("a.py", "both"), ("b.py", "gpt"), ("c.py", "claude")]
    assert merged[0]["issue_header"] == major          # the more severe version is kept
    assert (stats["both"], stats["gpt_only"], stats["claude_only"]) == (1, 1, 1)
    assert (stats["gpt_findings"], stats["claude_findings"]) == (2, 2)
    assert stats["gpt_status"] == stats["claude_status"] == "ok"
    assert yaml.safe_load(text)["review"]["estimated_effort_to_review_[1-5]"] == 2


@pytest.mark.parametrize("gpt_sec, claude_sec, expected", [
    ("No", "No", "No"),
    ("No", "SQL 注入", "SQL 注入"),
    ("XSS", "SQL 注入", "GPT：XSS\n\nClaude：SQL 注入"),
    (None, "No", None),           # GPT cut off before the field: stays incomplete
])
def test_security_concerns_are_united(gpt_sec, claude_sec, expected):
    text, _ = ad.merge_predictions(answer([], gpt_sec), answer([], claude_sec), load_yaml)
    assert yaml.safe_load(text)["review"].get("security_concerns") == expected


def test_a_concern_survives_an_unusable_finding_list():
    broken = yaml.safe_dump({"review": {"key_issues_to_review": "garbled", "security_concerns": "令牌写进了日志"}},
                            allow_unicode=True)
    text, stats = ad.merge_predictions(answer([finding("a.py", 1, 1)], "No"), broken, load_yaml)
    assert stats["claude_status"] == "unparsed"
    assert yaml.safe_load(text)["review"]["security_concerns"] == "令牌写进了日志"


def test_unparsable_gpt_answer_falls_back_to_claude_alone():
    text, stats = ad.merge_predictions("review:\n  key_issues_to_review:\n", answer([finding("a.py", 1, 1)]), load_yaml)
    assert stats["gpt_status"] == "unparsed" and stats["claude_status"] == "ok"
    assert stats["claude_only"] == 1
    assert ["argus_source" in f for f in yaml.safe_load(text)["review"]["key_issues_to_review"]] == [False]


def test_single_line_findings_carry_no_source_tag():
    text, stats = ad.merge_predictions(answer([finding("a.py", 1, 1)]), None, load_yaml, claude_status="timeout")
    assert stats["claude_status"] == "timeout" and stats["gpt_only"] == 1
    merged = yaml.safe_load(text)["review"]["key_issues_to_review"]
    assert ["argus_source" in f for f in merged] == [False]
    assert ad.source_note(merged[0]) == ""


def test_both_lines_unusable_returns_the_gpt_text_unchanged():
    raw = "review:\n  key_issues_to_review:\n"
    text, stats = ad.merge_predictions(raw, None, load_yaml, claude_status="error")
    assert text == raw and stats["gpt_status"] == "unparsed"


def test_single_line_findings_are_ranked_by_severity():
    minor_first = [finding("a.py", 1, 1, header="Edge Case"), finding("b.py", 1, 1, header="Possible Bug")]
    text, _ = ad.merge_predictions(answer(minor_first), None, load_yaml, claude_status="no_key")
    assert [f["relevant_file"] for f in yaml.safe_load(text)["review"]["key_issues_to_review"]] == ["b.py", "a.py"]


def test_cap_keeps_the_first_findings():
    items = list(range(7))
    assert ad.cap_findings(items, 5) == ([0, 1, 2, 3, 4], [5, 6])
    assert ad.cap_findings(items, None) == (items, [])


# ── what the PR shows ─────────────────────────────────────────────

def test_comparison_section_states_each_line_and_lists_cut_findings():
    stats = {"gpt_status": "ok", "claude_status": "budget", "gpt_findings": 3, "claude_findings": 0,
             "both": 0, "gpt_only": 3, "claude_only": 0}
    cut = [dict(finding("a.py\n", 7, 7, content="第一行\n- 第二行"), argus_source="gpt")]
    text = ad.comparison_section(stats, "openai/responses/gpt-6.2-sol", "claude-opus-5-5", cut)
    assert "双线对照" in text and "额度已用完或未设上限" in text and "单线" in text
    assert "`a.py:7`" in text and "仅 GPT" in text
    assert "第一行 - 第二行" in text            # one list item, not two
    both_down = dict(stats, gpt_status="unparsed", claude_status="error")
    assert "两条线路都没有给出可用结果" in ad.comparison_section(both_down, "g", "c")
    assert "$" not in text and "USD" not in text


def test_inline_finding_names_its_source_after_the_agent_prompt():
    plain = finding("a.py", 1, 2)
    tagged = dict(plain, argus_source="both")
    body = psf.format_review_finding_body(tagged)
    assert body.startswith(psf.format_review_finding_body(plain))
    assert body.rstrip().endswith("<sub>来源：GPT 与 Claude 一致</sub>")
    # Thread matching reads only the text before <details>; the note is after it.
    assert psf._extract_thread_match_keys(body, "a.py") == psf._extract_thread_match_keys(
        psf.format_review_finding_body(plain), "a.py")


def test_review_body_is_unchanged_without_dual_review():
    base = psf.build_review_body_additions([], 0)
    assert psf.build_review_body_additions([], 0, dual_section=None, second_model=None) == base
    with_dual = psf.build_review_body_additions([], 0, dual_section="X-SECTION", second_model="claude-opus-5-5")
    assert "X-SECTION" in with_dual and "+ claude-opus-5-5" in with_dual


# ── the budget guard ──────────────────────────────────────────────

class FakeAgent:
    def __init__(self):
        self.handled = []

    async def _handle_request(self, pr_url, request, notify=None):
        self.handled.append(request)
        return True


@pytest.fixture
def posted():
    items = []
    ab.set_poster(lambda owner, repo, number, body: items.append(body))
    yield items
    ab.set_poster(None)


def wire(gpt_ledger, monkeypatch, handle=None):
    attrs = {"_handle_request": handle} if handle else {}
    agent_cls = type("PRAgent", (FakeAgent,), attrs)

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(ab, "_load_runtime", lambda: (SimpleNamespace(acompletion=noop), agent_cls))
    ab.apply_patch(lambda: gpt_ledger)
    return agent_cls


@pytest.fixture
def gpt_ledger(tmp_path, clock):
    return ab.BudgetLedger(path=str(tmp_path / "budget.json"), config=GPT_CFG, clock=clock)


def test_spent_gpt_budget_still_lets_review_run_on_claude(gpt_ledger, monkeypatch, posted):
    agent_cls = wire(gpt_ledger, monkeypatch)
    gpt_ledger.record_cost(10)
    ab.set_review_alternate(lambda: True)
    agent = agent_cls()
    assert run(agent._handle_request("https://github.com/a/b/pull/1", "/review")) is True
    assert run(agent._handle_request("https://github.com/a/b/pull/1", "/describe")) is False
    assert agent.handled == ["/review"]
    assert len(posted) == 1 and "Claude" in posted[0] and "单线" in posted[0]


def test_without_a_second_line_a_spent_budget_stops_review_as_before(gpt_ledger, monkeypatch, posted):
    agent_cls = wire(gpt_ledger, monkeypatch)
    gpt_ledger.record_cost(10)
    ab.set_review_alternate(lambda: False)
    agent = agent_cls()
    assert run(agent._handle_request("https://github.com/a/b/pull/1", "/review")) is False
    assert posted == [ab.exhausted_comment(None, GPT_CFG, gpt_ledger.next_reset(), "budget used up")]
    assert "本期内 Argus 不再审查此 PR 或其他 PR，也不会再发这条提示。" in posted[0]


def test_gpt_budget_spent_during_a_review_says_review_goes_on_with_claude(gpt_ledger, monkeypatch, posted):
    async def spends(self, pr_url, request, notify=None):
        gpt_ledger.record_cost(10)
        ab._note_outcome(None)          # the Claude line answered
        return True

    agent_cls = wire(gpt_ledger, monkeypatch, handle=spends)
    ab.set_review_alternate(lambda: True)
    run(agent_cls()._handle_request("https://github.com/a/b/pull/1", "/review"))
    assert len(posted) == 1 and "Claude" in posted[0]


# ── inside PRReviewer (needs pr-agent) ────────────────────────────

class Reviewer:
    def __init__(self, gpt_outcome):
        self.gpt_outcome = gpt_outcome
        self.git_provider = SimpleNamespace(pr_num=4, repo=SimpleNamespace(full_name="acme/w"))
        self.token_handler = None
        self.vars = {}

    async def _get_prediction(self, model):
        if isinstance(self.gpt_outcome, BaseException):
            raise self.gpt_outcome
        return self.gpt_outcome


@pytest.fixture
def pr_agent_stubs(monkeypatch):
    pytest.importorskip("pr_agent")
    monkeypatch.setattr("pr_agent.algo.pr_processing.get_pr_diff", lambda *a, **k: "diff")
    monkeypatch.setattr(ad, "_render_prompts", lambda reviewer: ("sys", "user"))
    monkeypatch.setattr(ad, "_config", DUAL)


def prepare(reviewer, claude_outcome):
    async def claude():
        if isinstance(claude_outcome, BaseException):
            raise claude_outcome
        return claude_outcome
    return run(ad.dual_prepare_prediction(reviewer, "openai/responses/gpt-6.2-sol", None, claude))


def test_both_lines_answer_and_are_merged(pr_agent_stubs):
    reviewer = Reviewer(answer([finding("a.py", 1, 1)]))
    prepare(reviewer, answer([finding("a.py", 1, 2)]))
    merged = yaml.safe_load(reviewer.prediction)["review"]["key_issues_to_review"]
    assert [f["argus_source"] for f in merged] == ["both"]
    assert reviewer._argus_dual["claude_model"] == "claude-opus-5-5"


def test_gpt_down_reviews_with_claude_and_counts_as_a_success(pr_agent_stubs):
    state = {"error": RuntimeError("gpt"), "succeeded": 0, "timed_out": False}
    token = ab._run_state.set(state)
    try:
        reviewer = Reviewer(RuntimeError("gpt down"))
        prepare(reviewer, answer([finding("a.py", 1, 1)]))
    finally:
        ab._run_state.reset(token)
    assert state["succeeded"] == 1
    assert reviewer._argus_dual["gpt_status"] == "error"
    assert ["argus_source" in f for f in yaml.safe_load(reviewer.prediction)["review"]["key_issues_to_review"]] == [False]


def test_claude_down_reviews_with_gpt(pr_agent_stubs):
    reviewer = Reviewer(answer([finding("a.py", 1, 1)]))
    prepare(reviewer, ad.LineFailed("timeout"))
    assert reviewer._argus_dual["claude_status"] == "timeout"
    assert ["argus_source" in f for f in yaml.safe_load(reviewer.prediction)["review"]["key_issues_to_review"]] == [False]


def test_both_down_raises_the_gpt_error(pr_agent_stubs):
    with pytest.raises(RuntimeError, match="gpt down"):
        prepare(Reviewer(RuntimeError("gpt down")), ad.LineFailed("budget"))


def test_gpt_down_and_claude_unparsable_raises_the_gpt_error(pr_agent_stubs):
    with pytest.raises(RuntimeError, match="gpt down"):
        prepare(Reviewer(RuntimeError("gpt down")), "not yaml: [")


def test_patch_attaches_to_the_real_reviewer_and_stays_off_when_disabled(monkeypatch):
    pytest.importorskip("pr_agent")
    from pr_agent.tools import pr_reviewer
    cls = pr_reviewer.PRReviewer
    monkeypatch.setattr(cls, "_prepare_prediction", cls.__dict__["_prepare_prediction"])
    monkeypatch.setattr(ad, "_config", ad.DualConfig(enabled=False))
    calls = []

    async def original(self, model):
        calls.append(model)

    monkeypatch.setattr(cls, "_prepare_prediction", original)
    assert ad.apply_patch() is True
    assert ad.apply_patch() is False
    run(cls._prepare_prediction(object(), "m"))
    assert calls == ["m"]                       # disabled: PR-Agent's own path, unchanged
    assert ab._review_alternate is ad.claude_available


# ── real SDK error shapes (review follow-up) ──────────────────────

anthropic = pytest.importorskip("anthropic")
httpx = pytest.importorskip("httpx")
_REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def sdk_error(cls, status, error_type, message):
    body = {"type": "error", "error": {"type": error_type, "message": message}}
    return cls(message, response=httpx.Response(status, request=_REQUEST), body=body)


def test_a_rate_limit_that_mentions_billing_does_not_stop_the_line(ledger):
    exc = sdk_error(anthropic.RateLimitError, 429, "rate_limit_error",
                    "Rate limit reached; see your billing plan for higher limits.")
    with pytest.raises(ad.LineFailed) as e:
        call(ledger, exc)
    assert e.value.status == "error" and not ledger.is_exhausted()


@pytest.mark.parametrize("exc", [
    sdk_error(anthropic.BadRequestError, 400, "invalid_request_error",
              "You have reached your specified API usage limits."),
    sdk_error(anthropic.APIStatusError, 402, "billing_error", "Payment required."),
])
def test_real_spend_limit_errors_stop_the_line_for_the_period(ledger, exc):
    with pytest.raises(ad.LineFailed) as e:
        call(ledger, exc)
    assert e.value.status == "quota" and ledger.is_exhausted()


def test_overloaded_is_transient(ledger):
    exc = sdk_error(anthropic.InternalServerError, 529, "overloaded_error", "Overloaded")
    with pytest.raises(ad.LineFailed) as e:
        call(ledger, exc)
    assert e.value.status == "error" and not ledger.is_exhausted()


@pytest.mark.parametrize("exc", [anthropic.APITimeoutError(request=_REQUEST),
                                 httpx.ReadTimeout("read timed out", request=_REQUEST)])
def test_sdk_and_stream_read_timeouts_are_charged_and_close_the_client(ledger, exc):
    client = FakeClient(exc)
    with pytest.raises(ad.LineFailed) as e:
        run(ad.claude_review("sys", "user", DUAL, ledger, 300, api_key="k",
                             client_factory=lambda key, t: client))
    assert e.value.status == "timeout" and client.closed
    price = CLAUDE_CFG.prices[0]
    expected = (len("sys") + len("user")) * price.input + ab.timeout_output_tokens(300) * price.output
    assert ledger.snapshot()[0] == pytest.approx(expected / 1_000_000)


def test_failed_call_still_closes_the_client(ledger):
    client = FakeClient(RuntimeError("boom"))
    with pytest.raises(ad.LineFailed):
        run(ad.claude_review("s", "u", DUAL, ledger, 300, api_key="k",
                             client_factory=lambda key, t: client))
    assert client.closed


def test_malformed_claude_price_table_never_falls_back_to_gpt_prices():
    cfg = ad.parse_claude_budget({"prices": [{"model": "x"}]})
    assert [p.model for p in cfg.prices] == ["claude-opus-5-5"]


def test_claude_is_unavailable_with_a_damaged_ledger(tmp_path, clock, monkeypatch):
    path = tmp_path / "budget-anthropic.json"
    path.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(ad, "_config", DUAL)
    monkeypatch.setattr(ad, "_api_key", lambda: "k")
    monkeypatch.setattr(ad, "_ledger", ab.BudgetLedger(path=str(path), config=CLAUDE_CFG, clock=clock))
    assert ad.claude_available() is False


# ── Argus review follow-up (PR #74) ───────────────────────────────

def test_a_loose_early_match_does_not_take_the_exact_one():
    gpt = [finding("a.py", 20, 20, content="value may be None here (null_check)")]
    claude = [finding("a.py", 18, 18, header="Security", content="missing permission check (perm_guard)"),
              finding("a.py", 20, 20, content="value may be None here (null_check)")]
    merged = ad.merge_findings(gpt, claude)
    by_line = {f["start_line"]: f["argus_source"] for f in merged}
    assert by_line == {20: "both", 18: "claude"}
    assert len(merged) == 2


def test_a_stream_that_drops_mid_answer_is_charged_an_estimate(ledger):
    with pytest.raises(ad.LineFailed) as e:
        call(ledger, httpx.RemoteProtocolError("peer closed connection"))
    assert e.value.status == "error" and ledger.snapshot()[0] > 0


def test_a_refused_request_costs_nothing(ledger):
    with pytest.raises(ad.LineFailed):
        call(ledger, sdk_error(anthropic.BadRequestError, 400, "invalid_request_error", "bad field"))
    assert ledger.snapshot()[0] == 0


def test_the_event_is_written_when_both_lines_fail(pr_agent_stubs, monkeypatch):
    emitted = []
    monkeypatch.setattr(ad, "_emit", lambda reviewer, model, config, stats: emitted.append(dict(stats)))
    with pytest.raises(RuntimeError):
        prepare(Reviewer(RuntimeError("gpt down")), ad.LineFailed("timeout"))
    assert emitted == [{"gpt_status": "error", "claude_status": "timeout"}]


def test_pairing_finds_as_many_agreed_pairs_as_possible():
    gpt = [finding("a.py", 10, 10), finding("a.py", 14, 14)]
    claude = [finding("a.py", 13, 13), finding("a.py", 17, 17)]
    merged = ad.merge_findings(gpt, claude)
    assert [f["argus_source"] for f in merged] == ["both", "both"]


def test_an_error_event_inside_a_started_stream_is_charged(ledger):
    with pytest.raises(ad.LineFailed):
        call(ledger, sdk_error(anthropic.APIStatusError, 200, "overloaded_error", "Overloaded"))
    assert ledger.snapshot()[0] > 0 and not ledger.is_exhausted()


def test_the_event_is_written_when_gpt_fails_and_claude_is_unparsable(pr_agent_stubs, monkeypatch):
    emitted = []
    monkeypatch.setattr(ad, "_emit", lambda reviewer, model, config, stats: emitted.append(dict(stats)))
    with pytest.raises(RuntimeError):
        prepare(Reviewer(RuntimeError("gpt down")), "not yaml: [")
    assert emitted and emitted[0]["gpt_status"] == "error" and emitted[0]["claude_status"] == "unparsed"


# ── second Argus review (8a18f39) ─────────────────────────────────

def test_an_exact_pair_is_not_traded_for_a_crossed_one():
    a, b = "缓存键没有包含用户编号，会串号。", "重试循环没有上限，会一直重试。"
    gpt = [finding("a.py", 20, 20, content=a), finding("a.py", 22, 22, content=b)]
    claude = [finding("a.py", 20, 20, content=a), finding("a.py", 19, 19, content=b)]
    assert ad.best_pairing(gpt, claude) == {0: 0, 1: 1}


def test_cancelled_call_is_charged_and_the_cancellation_goes_through(ledger):
    async def scenario():
        started = asyncio.Event()

        async def hang():
            started.set()
            await asyncio.sleep(60)

        client = FakeClient(hang)
        task = asyncio.create_task(ad.claude_review("sys", "user", DUAL, ledger, 300, api_key="k",
                                                    client_factory=lambda key, t: client))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return client

    client = run(scenario())
    assert client.closed and ledger.snapshot()[0] > 0


def test_a_connection_that_never_opened_costs_nothing(ledger):
    refused = anthropic.APIConnectionError(request=_REQUEST)
    refused.__cause__ = httpx.ConnectError("name resolution failed")
    with pytest.raises(ad.LineFailed):
        call(ledger, refused)
    timeout = anthropic.APITimeoutError(request=_REQUEST)
    timeout.__cause__ = httpx.ConnectTimeout("connect timed out")
    with pytest.raises(ad.LineFailed) as e:
        call(ledger, timeout)
    assert e.value.status == "timeout"
    assert ledger.snapshot()[0] == 0


# Pairs from the review of the similarity measure: different defects two lines apart
# must never merge, whatever language or shared sentence pattern they use.
DIFFERENT = [
    ("Missing null check on config before use.", "Missing permission check before delete."),
    ("The function does not check the return value of open.", "The function does not validate the input path."),
    ("The handler dereferences request.user without checking for None, so anonymous requests crash.",
     "The delete endpoint does not verify that the caller has the admin role."),
    ("这里没有处理空列表，会除以零。", "这里没有处理超时，会一直阻塞。"),
    ("函数返回值没有被检查，错误会被吞掉。", "函数参数没有被校验，非法输入会被接受。"),
    ("这里的锁没有释放，并发时会死锁。", "这里的文件句柄没有关闭，会泄漏。"),
    ("parse_config 在 reset_day 为 0 时会抛出 ValueError。", "parse_config 读取 timezone 时没有校验，非法值会被接受。"),
    ("user 可能为 None，访问 user.name 会崩溃。", "delete_user 没有检查 is_admin，普通用户也能删除他人账号。"),
]


@pytest.mark.parametrize("a, b", DIFFERENT)
def test_different_defects_on_nearby_lines_never_merge(a, b):
    assert not ad.same_finding(finding("a.py", 20, 20, content=a), finding("a.py", 22, 22, content=b))


def test_an_answer_without_the_security_field_keeps_the_review_incomplete():
    cut_off = yaml.safe_dump({"review": {"key_issues_to_review": []}})
    text, _ = ad.merge_predictions(cut_off, answer([], "No"), load_yaml)
    assert "security_concerns" not in yaml.safe_load(text)["review"]


def test_the_section_says_agreement_is_a_lower_bound():
    stats = {"gpt_status": "ok", "claude_status": "ok", "gpt_findings": 1, "claude_findings": 1,
             "both": 0, "gpt_only": 1, "claude_only": 1}
    assert "偏保守" in ad.comparison_section(stats, "g", "c")


def test_a_concern_survives_when_both_finding_lists_are_unusable():
    gpt = yaml.safe_dump({"review": {"key_issues_to_review": "garbled", "security_concerns": "No"}})
    claude = yaml.safe_dump({"review": {"key_issues_to_review": "garbled", "security_concerns": "令牌写进了日志"}},
                            allow_unicode=True)
    text, stats = ad.merge_predictions(gpt, claude, load_yaml)
    assert (stats["gpt_status"], stats["claude_status"]) == ("unparsed", "unparsed")
    review = yaml.safe_load(text)["review"]
    assert review["security_concerns"] == "令牌写进了日志"
    assert review["key_issues_to_review"] == "garbled"  # still incomplete


def test_both_unusable_without_a_concern_passes_gpt_text_unchanged():
    gpt = yaml.safe_dump({"review": {"key_issues_to_review": "garbled", "security_concerns": "No"}})
    text, _ = ad.merge_predictions(gpt, "not yaml at all: [", load_yaml)
    assert text == gpt


def test_usage_is_recorded_even_if_cancelled_while_closing(ledger):
    class CancelOnClose(FakeClient):
        async def close(self):
            raise asyncio.CancelledError()

    client = CancelOnClose(message())
    with pytest.raises(asyncio.CancelledError):
        run(ad.claude_review("sys", "user", DUAL, ledger, 300, api_key="k",
                             client_factory=lambda key, t: client))
    assert ledger.snapshot()[0] == pytest.approx((10_000 * 4.0 + 1000 * 20.0) / 1_000_000)


def test_a_wrong_pairing_hides_no_finding():
    # Two defects in one function: they share identifiers and may be paired.
    timeout = finding("src/auth.py", 10, 30,
                      content="load_config reads config.timeout without validation; negative values hang.")
    traversal = finding("src/auth.py", 12, 12, header="Security",
                        content="load_config reads config.path without validation; path traversal possible.")
    merged = ad.merge_findings([timeout], [traversal])
    texts = " ".join(f["issue_content"] for f in merged)
    assert "negative values hang" in texts and "path traversal" in texts
    if len(merged) == 1:
        assert merged[0]["issue_header"] == "Security"
        assert "GPT 的描述" in merged[0]["issue_content"]


def test_an_agreed_finding_with_one_description_is_not_repeated():
    a = finding("a.py", 1, 1, content="列表为空时除以零。")
    merged = ad.merge_findings([a], [dict(a)])
    assert merged[0]["issue_content"] == "列表为空时除以零。"


@pytest.mark.parametrize("failure", [
    lambda: anthropic.APIConnectionError(request=_REQUEST),   # dropped mid-answer
    lambda: TimeoutError(),
])
def test_a_failed_call_is_charged_even_if_cancelled_while_closing(ledger, failure):
    class CancelOnClose(FakeClient):
        async def close(self):
            raise asyncio.CancelledError()

    client = CancelOnClose(failure())
    with pytest.raises(asyncio.CancelledError):
        run(ad.claude_review("sys", "user", DUAL, ledger, 300, api_key="k",
                             client_factory=lambda key, t: client))
    assert ledger.snapshot()[0] > 0


def test_arguments_the_sdk_rejects_cost_nothing(ledger):
    class OldSdk(FakeClient):
        def __init__(self):
            super().__init__(message())

            class Messages:
                def stream(self, **kwargs):
                    raise TypeError("unexpected keyword argument 'output_config'")
            self.messages = Messages()

    with pytest.raises(ad.LineFailed) as e:
        run(ad.claude_review("sys", "user", DUAL, ledger, 300, api_key="k",
                             client_factory=lambda key, t: OldSdk()))
    assert e.value.status == "error"
    assert ledger.snapshot()[0] == 0


def test_the_continue_notice_says_review_stops_with_the_claude_budget():
    import argus_budget as ab
    from datetime import datetime
    config = ab.parse_config({"monthly_usd": 1})
    text = ab.exhausted_comment(1.0, config, datetime(2026, 12, 1), review_continues=True)
    assert "Claude 线路本期额度也用完" in text


def test_a_more_severe_version_wins_over_a_minor_security_header():
    critical = finding("src/auth.py", 10, 10, header="Critical Bug", content="token check skipped for admin users")
    minor = finding("src/auth.py", 11, 11, header="Security nit", content="token check skipped for admin users")
    merged = ad.merge_findings([critical], [minor])
    assert len(merged) == 1 and merged[0]["issue_header"] == "Critical Bug"
    assert psf._finding_severity(merged[0]) == "Critical"


def test_a_type_error_after_the_request_was_built_is_charged(ledger):
    with pytest.raises(ad.LineFailed):
        call(ledger, TypeError("bad event payload"))
    assert ledger.snapshot()[0] > 0


def _thread(issue):
    return {"path": issue["relevant_file"], "resolved": False, "resolved_by_human": False,
            "replied": False, "judged": False, "first_bot_body": psf.format_review_finding_body(issue)}


def test_a_repeated_half_of_a_pair_does_not_hide_the_new_half():
    # Paired on nearby lines and shared words, but two defects; only the first was posted before.
    old = finding("app/store.py", 10, 10,
                  content="parse_header trusts content_length from request_headers; oversized body exhausts memory.")
    new = finding("app/store.py", 11, 11, header="Security",
                  content="parse_header trusts content_type from request_headers; wrong charset garbles text.")
    merged = ad.merge_findings([old], [new])
    assert len(merged) == 1 and ad.VERSIONS_KEY in merged[0]
    threads = psf._relevant_threads([_thread(old)])
    out, dropped = ad.split_repeated_pairs(merged, lambda f: psf.repeat_reason(f, threads))
    assert [f["argus_source"] for f in out] == ["claude"]
    assert "wrong charset" in out[0]["issue_content"]
    assert dropped[0][1] == "repeats an open thread"
    kept, _ = psf.filter_findings(ad.strip_versions(out), threads=threads)
    assert len(kept) == 1 and ad.VERSIONS_KEY not in kept[0]


def test_a_pair_with_no_repeat_or_two_repeats_stays_whole():
    a = finding("a.py", 1, 1, content="列表为空时除以零。")
    merged = ad.merge_findings([a], [dict(a)])
    out, dropped = ad.split_repeated_pairs(merged, lambda f: None)
    assert out == merged and dropped == []
    out, dropped = ad.split_repeated_pairs(merged, lambda f: "repeats an open thread")
    assert out == merged and dropped == []


def test_stripping_the_versions_rewrites_the_prediction():
    merged = ad.merge_findings([finding("a.py", 1, 1)], [finding("a.py", 1, 1)])
    text = yaml.safe_dump({"review": {"key_issues_to_review": merged, "security_concerns": "No"}},
                          allow_unicode=True)
    out, dropped = psf._filter_prediction(text, load_yaml, lambda fs: (ad.strip_versions(fs), []))
    assert dropped == [] and ad.VERSIONS_KEY not in out
