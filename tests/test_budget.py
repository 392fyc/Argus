"""Tests for argus_budget: periods and the reset boundary, the ledger (atomic
writes, concurrency, damaged files), pricing, quota errors, the one-time notice."""

import asyncio
import json
import math
import pathlib
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import argus_budget as ab  # noqa: E402

JST = timezone(timedelta(hours=9))
CFG = ab.BudgetConfig()  # 10 USD, reset day 15, Asia/Tokyo, gpt-6.1-sol prices


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def at(year, month, day, hour=0, minute=0, second=0, tz=JST):
    return datetime(year, month, day, hour, minute, second, tzinfo=tz)


@pytest.fixture
def clock():
    return Clock(at(2026, 11, 20, 12))


@pytest.fixture
def ledger(tmp_path, clock):
    return ab.BudgetLedger(path=str(tmp_path / "budget.json"), config=CFG, clock=clock)


@pytest.fixture(autouse=True)
def event_sink(tmp_path, monkeypatch):
    sink = tmp_path / "events.jsonl"
    monkeypatch.setattr(ab.emitter, "_path", str(sink))
    monkeypatch.setattr(ab.emitter, "_ready", None)
    return sink


def read_ledger(ledger):
    return json.loads(pathlib.Path(ledger.path).read_text(encoding="utf-8"))


# ── periods ───────────────────────────────────────────────────────

def test_period_starts_on_reset_day_at_midnight_tokyo_time():
    start, nxt = ab.period_bounds(at(2026, 11, 20, 12), CFG)
    assert start == at(2026, 11, 15) and nxt == at(2026, 12, 15)


def test_boundary_second_before_and_at_the_reset_moment():
    before = ab.period_bounds(at(2026, 11, 14, 23, 59, 59), CFG)[0]
    exactly = ab.period_bounds(at(2026, 11, 15, 0, 0, 0), CFG)[0]
    assert before == at(2026, 10, 15)
    assert exactly == at(2026, 11, 15)


def test_boundary_is_judged_in_tokyo_time_not_utc():
    # 14:59:59 UTC on the 14th is 23:59:59 JST; one second later it is the 15th, 00:00 JST.
    utc = timezone.utc
    assert ab.period_bounds(at(2026, 11, 14, 14, 59, 59, utc), CFG)[0] == at(2026, 10, 15)
    assert ab.period_bounds(at(2026, 11, 14, 15, 0, 0, utc), CFG)[0] == at(2026, 11, 15)
    # 00:00 UTC on the 15th is already 09:00 JST on the 15th.
    assert ab.period_bounds(at(2026, 11, 15, 0, 0, 0, utc), CFG)[0] == at(2026, 11, 15)


def test_period_crosses_the_year_in_both_directions():
    start, nxt = ab.period_bounds(at(2027, 1, 5), CFG)
    assert start == at(2026, 12, 15) and nxt == at(2027, 1, 15)
    start, nxt = ab.period_bounds(at(2026, 12, 20), CFG)
    assert start == at(2026, 12, 15) and nxt == at(2027, 1, 15)


def test_other_reset_days_and_timezones():
    cfg = ab.BudgetConfig(reset_day=1, timezone="UTC")
    assert ab.period_bounds(at(2026, 3, 1, 0, 0, 0, timezone.utc), cfg)[0] == at(2026, 3, 1, tz=timezone.utc)
    assert ab.period_bounds(at(2026, 2, 28, 23, 59, 59, timezone.utc), cfg)[1] == at(2026, 3, 1, tz=timezone.utc)


def test_missing_tz_database_falls_back_to_fixed_tokyo_offset(monkeypatch):
    import zoneinfo

    def no_tzdata(name):
        raise zoneinfo.ZoneInfoNotFoundError(name)

    monkeypatch.setattr(zoneinfo, "ZoneInfo", no_tzdata)
    start, _ = ab.period_bounds(at(2026, 11, 14, 15, 0, 0, timezone.utc), CFG)
    assert start == at(2026, 11, 15)  # still 00:00 +09:00


def test_unknown_timezone_uses_utc_not_a_crash():
    cfg = ab.BudgetConfig(timezone="Mars/Olympus")
    start, _ = ab.period_bounds(at(2026, 11, 20, tz=timezone.utc), cfg)
    assert start.utcoffset() == timedelta(0)


# ── config ────────────────────────────────────────────────────────

def test_config_defaults_match_the_agreed_values():
    cfg = ab.parse_config(None)
    assert (cfg.monthly_usd, cfg.reset_day, cfg.timezone) == (10.0, 15, "Asia/Tokyo")
    price = cfg.prices[0]
    assert (price.model, price.input, price.cached_input, price.output) == ("gpt-6.1-sol", 2.0, 0.10, 10.0)


def test_config_reads_table_with_any_key_case():
    cfg = ab.parse_config({"MONTHLY_USD": 25, "RESET_DAY": 1, "TIMEZONE": "UTC",
                           "PRICES": [{"MODEL": "m", "INPUT": 1, "OUTPUT": 4}]})
    assert (cfg.monthly_usd, cfg.reset_day, cfg.timezone) == (25.0, 1, "UTC")
    assert cfg.prices == (ab.ModelPrice("m", 1.0, 1.0, 4.0),)  # cached defaults to input


@pytest.mark.parametrize("raw", [
    {"monthly_usd": 0}, {"monthly_usd": -5}, {"monthly_usd": "ten"}, {"monthly_usd": True},
    {"monthly_usd": float("nan")}, {"monthly_usd": float("inf")}])
def test_bad_cap_falls_back_to_the_default_cap(raw):
    assert ab.parse_config(raw).monthly_usd == 10.0


@pytest.mark.parametrize("day", [0, 29, 31, "15", None, True, 1.5])
def test_bad_reset_day_falls_back(day):
    assert ab.parse_config({"reset_day": day}).reset_day == 15


def test_malformed_price_entries_are_ignored_and_empty_table_keeps_default():
    cfg = ab.parse_config({"prices": [{"model": "x"}, "junk"]})
    assert cfg.prices == ab.BudgetConfig().prices


def test_settings_loader_uses_pr_agent_table(monkeypatch):
    import types
    fake = types.ModuleType("pr_agent.config_loader")
    fake.get_settings = lambda: SimpleNamespace(get=lambda k: {"monthly_usd": 3} if k == "argus_budget" else None)
    monkeypatch.setitem(sys.modules, "pr_agent", types.ModuleType("pr_agent"))
    monkeypatch.setitem(sys.modules, "pr_agent.config_loader", fake)
    assert ab.load_config().monthly_usd == 3.0


# ── pricing ───────────────────────────────────────────────────────

PRICE = ab.ModelPrice("gpt-6.1-sol", 2.00, 0.10, 10.00)


def usage(prompt, completion, cached=0, reasoning=0):
    return SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning))


def test_cost_bills_cached_input_at_the_cached_rate():
    # 6000 fresh input, 4000 cached, 2000 output (1500 of them reasoning).
    cost = ab.compute_cost(usage(10000, 2000, cached=4000, reasoning=1500), PRICE)
    assert cost == pytest.approx((6000 * 2.00 + 4000 * 0.10 + 2000 * 10.00) / 1e6)
    assert cost == pytest.approx(0.0324)


def test_reasoning_tokens_are_not_billed_twice():
    assert ab.compute_cost(usage(0, 1000, reasoning=1000), PRICE) == pytest.approx(0.01)


def test_reasoning_reported_beyond_completion_is_added():
    assert ab.compute_cost(usage(0, 100, reasoning=300), PRICE) == pytest.approx(400 * 10 / 1e6)


def test_usage_may_be_a_plain_dict_and_cached_is_clamped():
    u = {"prompt_tokens": 100, "completion_tokens": 0,
         "prompt_tokens_details": {"cached_tokens": 500}}
    assert ab.compute_cost(u, PRICE) == pytest.approx(100 * 0.10 / 1e6)


def test_no_usage_is_not_priced():
    assert ab.compute_cost(None, PRICE) is None
    assert ab.compute_cost(usage(0, 0), PRICE) is None


def test_model_names_are_normalised_and_unknown_models_cost_the_most():
    assert ab.normalize_model("openai/responses/GPT-6.1-Sol") == "gpt-6.1-sol"
    cfg = ab.BudgetConfig(prices=(ab.ModelPrice("cheap", 1, 1, 2), ab.ModelPrice("dear", 5, 5, 40)))
    assert ab.find_price("openai/responses/dear", cfg).output == 40
    assert ab.find_price("something-else", cfg).model == "dear"
    snapshot = ab.find_price("gpt-6.1-sol-2026-10-01", CFG)
    assert snapshot.model == "gpt-6.1-sol"


def test_responses_bridge_usage_shape_is_priced():
    # LiteLLM maps Responses API usage to prompt/completion tokens plus *_details.
    response = SimpleNamespace(usage=usage(1_000_000, 100_000, cached=200_000, reasoning=60_000))
    assert ab.compute_cost(response.usage, PRICE) == pytest.approx(0.8 * 2 + 0.2 * 0.1 + 0.1 * 10)


# ── ledger: spending, the cap, rollover ───────────────────────────

def test_spending_accumulates_and_persists(ledger):
    ledger.record_cost(1.5)
    ledger.record_cost(2.25)
    assert read_ledger(ledger)["spent_usd"] == pytest.approx(3.75)
    ledger.check()  # under the cap: no exception


def test_cap_boundary_is_inclusive(ledger):
    ledger.record_cost(9.999999)
    ledger.check()
    ledger.record_cost(0.000001)
    with pytest.raises(ab.BudgetExhausted) as info:
        ledger.check()
    assert info.value.spent_usd == pytest.approx(10.0)
    assert info.value.next_reset == at(2026, 12, 15)


def test_new_period_clears_spending_the_flag_and_the_notice(ledger, clock):
    ledger.record_cost(12)
    ledger.mark_exhausted()
    assert ledger.claim_notification() is True
    clock.now = at(2026, 12, 14, 23, 59, 59)
    with pytest.raises(ab.BudgetExhausted):
        ledger.check()                       # still the old period one second before
    clock.now = at(2026, 12, 15, 0, 0, 0)
    ledger.check()                           # the reset day arrived
    state = read_ledger(ledger)
    assert state["spent_usd"] == 0 and state["exhausted"] is False and state["notified"] is False
    assert datetime.fromisoformat(state["period_start"]) == at(2026, 12, 15)
    ledger.record_cost(11)
    assert ledger.claim_notification() is True  # a new period may announce again


def test_a_clock_that_steps_back_does_not_reopen_a_spent_budget(ledger, clock):
    ledger.record_cost(11)
    clock.now = at(2026, 11, 1)              # earlier than the stored period start
    with pytest.raises(ab.BudgetExhausted):
        ledger.check()


def test_missing_ledger_starts_a_fresh_period(ledger):
    ledger.check()
    assert read_ledger(ledger)["spent_usd"] == 0


def test_exhausted_flag_blocks_even_with_money_left(ledger):
    ledger.record_cost(1)
    ledger.mark_exhausted()
    with pytest.raises(ab.BudgetExhausted) as info:
        ledger.check()
    assert "quota" in info.value.reason


# ── ledger: damaged files stop spending ───────────────────────────

GOOD = {"version": 1, "period_start": "2026-11-15T00:00:00+09:00",
        "spent_usd": 1.0, "exhausted": False, "notified": False}


@pytest.mark.parametrize("content", [
    "", "{not json", "[]", "null", '"text"',
    json.dumps({**GOOD, "version": 2}),
    json.dumps({k: v for k, v in GOOD.items() if k != "spent_usd"}),
    json.dumps({**GOOD, "spent_usd": -1}),
    json.dumps({**GOOD, "spent_usd": "1.0"}),
    json.dumps({**GOOD, "spent_usd": True}),
    '{"version":1,"period_start":"2026-11-15T00:00:00+09:00","spent_usd":NaN,"exhausted":false,"notified":false}',
    json.dumps({**GOOD, "period_start": "2026-11-15T00:00:00"}),   # no timezone
    json.dumps({**GOOD, "period_start": "last tuesday"}),
    json.dumps({**GOOD, "exhausted": "no"}),
    json.dumps({**GOOD, "notified": 0}),
])
def test_unreadable_or_invalid_ledger_counts_as_used_up(ledger, content):
    pathlib.Path(ledger.path).write_text(content, encoding="utf-8")
    with pytest.raises(ab.BudgetExhausted) as info:
        ledger.check()
    assert "unusable" in info.value.reason
    assert ledger.is_exhausted() is True
    assert ledger.record_cost(1) is None                      # never overwrites a damaged ledger
    assert pathlib.Path(ledger.path).read_text(encoding="utf-8") == content


def test_binary_garbage_in_the_ledger_counts_as_used_up(ledger):
    pathlib.Path(ledger.path).write_bytes(b"\xff\xfe\x00garbage")
    assert ledger.is_exhausted() is True


def test_damaged_ledger_notice_is_sent_once_per_process(ledger):
    pathlib.Path(ledger.path).write_text("{", encoding="utf-8")
    assert ledger.claim_notification() is True
    assert ledger.claim_notification() is False


def test_unwritable_location_stops_calls(tmp_path, clock):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    led = ab.BudgetLedger(path=str(blocker / "budget.json"), config=CFG, clock=clock)
    with pytest.raises(ab.BudgetExhausted):
        led.check()


# ── ledger: atomic writes ─────────────────────────────────────────

def test_failed_write_keeps_the_previous_ledger_and_leaves_no_temp_file(ledger, monkeypatch):
    ledger.record_cost(1)
    before = pathlib.Path(ledger.path).read_text(encoding="utf-8")

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(ab.os, "replace", broken_replace)
    assert ledger.record_cost(5) is None
    monkeypatch.undo()
    assert pathlib.Path(ledger.path).read_text(encoding="utf-8") == before
    leftovers = [p.name for p in pathlib.Path(ledger.path).parent.iterdir()
                 if p.name.endswith(".tmp")]
    assert leftovers == []


def test_calls_are_refused_until_the_ledger_can_be_written_again(ledger, monkeypatch):
    ledger.record_cost(1)
    real_replace = ab.os.replace
    monkeypatch.setattr(ab.os, "replace", lambda s, d: (_ for _ in ()).throw(OSError("ro")))
    ledger.record_cost(1)                      # could not be saved
    with pytest.raises(ab.BudgetExhausted):
        ledger.check()
    monkeypatch.setattr(ab.os, "replace", real_replace)
    ledger.check()                             # writable again: allowed
    assert ledger.is_exhausted() is False


def test_ledger_is_written_through_a_rename(ledger, monkeypatch):
    calls = []
    real = ab.os.replace
    monkeypatch.setattr(ab.os, "replace", lambda s, d: (calls.append((s, d)), real(s, d))[1])
    ledger.record_cost(1)
    assert calls and calls[-1][1] == ledger.path and calls[-1][0] != ledger.path


# ── ledger: concurrency ───────────────────────────────────────────

def test_concurrent_threads_lose_no_updates(ledger):
    def work():
        for _ in range(40):
            ledger.record_cost(0.01)

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert read_ledger(ledger)["spent_usd"] == pytest.approx(3.20)


def test_concurrent_processes_lose_no_updates(tmp_path):
    path = tmp_path / "budget.json"
    script = (
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "from datetime import datetime, timezone\n"
        "import argus_budget as ab\n"
        "led = ab.BudgetLedger(path=sys.argv[2], clock=lambda: datetime(2026,11,20,tzinfo=timezone.utc))\n"
        "for _ in range(25): led.record_cost(0.01)\n")
    procs = [subprocess.Popen([sys.executable, "-c", script, str(REPO_ROOT), str(path)])
             for _ in range(4)]
    assert [p.wait(timeout=60) for p in procs] == [0, 0, 0, 0]
    assert json.loads(path.read_text(encoding="utf-8"))["spent_usd"] == pytest.approx(1.00)


def test_exactly_one_thread_wins_the_notice(ledger):
    ledger.record_cost(11)
    wins = []

    def claim():
        wins.append(ledger.claim_notification())

    threads = [threading.Thread(target=claim) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert wins.count(True) == 1


def test_the_notice_is_remembered_across_restarts(ledger, tmp_path, clock):
    ledger.record_cost(11)
    assert ledger.claim_notification() is True
    again = ab.BudgetLedger(path=ledger.path, config=CFG, clock=clock)  # a new process
    assert again.claim_notification() is False


def test_no_notice_while_budget_remains(ledger):
    ledger.record_cost(1)
    assert ledger.claim_notification() is False


# ── quota errors ──────────────────────────────────────────────────

class ProviderError(Exception):
    def __init__(self, message, body=None, code=None):
        super().__init__(message)
        self.body, self.code = body, code


@pytest.mark.parametrize("exc,expected", [
    (ProviderError("429", body={"error": {"code": "insufficient_quota"}}), True),
    (ProviderError("You exceeded your current quota, please check your plan"), True),
    (ProviderError("x", code="credit_balance_exhausted"), True),
    (ProviderError("Your credit balance is too low to access the API"), True),
    (ProviderError("billing_hard_limit_reached"), True),
    (ProviderError("Rate limit reached for gpt in organization on tokens per min (TPM)",
                   body={"error": {"code": "rate_limit_exceeded"}}), False),
    # The structured code wins over the wording of the message.
    (ProviderError("You exceeded your current quota, please check your plan",
                   body={"error": {"code": "rate_limit_exceeded"}}), False),
    (ProviderError("Error code: 429", body={"error": {"type": "insufficient_quota",
                                                       "message": "try later"}}), True),
    (ProviderError('litellm.RateLimitError: {"error": {"message": "x", "type": "insufficient_quota", '
                   '"code": "insufficient_quota"}}'), True),
    (ProviderError("server error"), False),
    (TimeoutError("timed out"), False),
])
def test_quota_errors_are_told_apart_from_ordinary_rate_limits(exc, expected):
    assert ab.is_quota_error(exc) is expected


def run(coro):
    return asyncio.run(coro)


def make_guard(ledger, original):
    return ab.make_guarded_acompletion(original, lambda: ledger)


def test_call_prices_the_response_and_adds_it_to_the_ledger(ledger):
    async def original(*a, **k):
        return SimpleNamespace(usage=usage(10000, 2000, cached=4000, reasoning=1500))

    guarded = make_guard(ledger, original)
    run(guarded(model="openai/responses/gpt-6.1-sol", messages=[]))
    assert read_ledger(ledger)["spent_usd"] == pytest.approx(0.0324)


def test_spent_budget_stops_the_call_before_it_is_made(ledger):
    ledger.record_cost(10)
    called = []

    async def original(*a, **k):
        called.append(1)

    with pytest.raises(ab.BudgetExhausted):
        run(make_guard(ledger, original)(model="m"))
    assert called == []


def test_quota_error_marks_the_period_used_up_and_is_not_retried(ledger):
    calls = []
    error = ProviderError("Error code: 429", body={"error": {"code": "insufficient_quota"}})

    async def original(*a, **k):
        calls.append(1)
        raise error

    guarded = make_guard(ledger, original)
    with pytest.raises(ProviderError) as info:
        run(guarded(model="m"))
    assert info.value is error
    assert read_ledger(ledger)["exhausted"] is True
    with pytest.raises(ab.BudgetExhausted):
        run(guarded(model="m"))
    assert calls == [1]                         # the second call never reached the provider


def test_ordinary_rate_limit_does_not_use_up_the_budget(ledger):
    async def original(*a, **k):
        raise ProviderError("slow down", body={"error": {"code": "rate_limit_exceeded"}})

    with pytest.raises(ProviderError):
        run(make_guard(ledger, original)(model="m"))
    ledger.check()
    assert read_ledger(ledger)["exhausted"] is False


def test_response_without_usage_is_still_charged(ledger):
    async def original(*a, **k):
        return SimpleNamespace(usage=None)

    run(make_guard(ledger, original)(model="m"))
    assert read_ledger(ledger)["spent_usd"] == pytest.approx(CFG.missing_usage_usd)


def test_a_failed_call_without_quota_error_costs_nothing(ledger):
    async def original(*a, **k):
        raise ConnectionError("reset")

    with pytest.raises(ConnectionError):
        run(make_guard(ledger, original)(model="m"))
    assert read_ledger(ledger)["spent_usd"] == 0


def test_budget_exhausted_is_not_an_openai_error():
    try:
        import openai
    except ImportError:
        pytest.skip("openai not installed")
    assert not issubclass(ab.BudgetExhausted, openai.OpenAIError)


# ── the notice ────────────────────────────────────────────────────

@pytest.fixture
def posted():
    items = []
    ab.set_poster(lambda owner, repo, number, body: items.append((owner, repo, number, body)))
    yield items
    ab.set_poster(None)


def events(sink):
    if not sink.exists():
        return []
    return [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines()]


def test_notice_is_posted_once_with_the_reset_time_and_logged(ledger, posted, event_sink):
    ledger.record_cost(10.5)
    url = "https://github.com/acme/widgets/pull/7"
    assert ab.notify_once(ledger, url, "budget used up") is True
    assert ab.notify_once(ledger, url, "budget used up") is False
    assert ab.notify_once(ledger, "https://github.com/acme/widgets/pull/8", "x") is False
    assert len(posted) == 1
    owner, repo, number, body = posted[0]
    assert (owner, repo, number) == ("acme", "widgets", 7)
    assert "2026-12-15 00:00" in body and "Asia/Tokyo" in body and "$10.00" in body
    logged = [e for e in events(event_sink) if e["event_type"] == "budget_exhausted"]
    assert len(logged) == 1
    assert logged[0]["pr_number"] == 7 and logged[0]["repo"] == "acme/widgets"
    assert logged[0]["payload"]["monthly_usd"] == 10.0


def test_notice_parses_api_style_pr_urls():
    assert ab.parse_pr_url("https://api.github.com/repos/acme/widgets/pulls/42") == ("acme", "widgets", 42)
    assert ab.parse_pr_url("https://github.com/acme/widgets/pull/42") == ("acme", "widgets", 42)
    assert ab.parse_pr_url("nonsense") is None


def test_failed_post_is_not_marked_sent_and_is_retried_a_limited_number_of_times(
        ledger, clock, event_sink):
    ledger.record_cost(11)
    attempts = []

    def boom(*a):
        attempts.append(1)
        raise RuntimeError("github down")

    ab.set_poster(boom)
    url = "https://github.com/a/b/pull/1"
    try:
        assert ab.notify_once(ledger, url, "r") is False        # attempt 1 fails
        assert ab.notify_once(ledger, url, "r") is False        # too soon: nothing tried
        assert len(attempts) == 1
        assert read_ledger(ledger)["notified"] is False
        for _ in range(5):                                      # later requests keep trying...
            clock.now += timedelta(seconds=ab.NOTICE_RETRY_SECONDS + 1)
            ab.notify_once(ledger, url, "r")
        assert len(attempts) == ab.MAX_NOTICE_ATTEMPTS          # ...but only up to the limit
    finally:
        ab.set_poster(None)
    errors = [e for e in events(event_sink) if e["event_type"] == "error"]
    assert len(errors) == ab.MAX_NOTICE_ATTEMPTS
    # The "budget used up" event is written once, on the first attempt.
    assert len([e for e in events(event_sink) if e["event_type"] == "budget_exhausted"]) == 1


def test_a_retry_that_succeeds_marks_the_notice_sent_for_good(ledger, clock, posted):
    ledger.record_cost(11)
    url = "https://github.com/a/b/pull/1"
    ab.set_poster(lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    assert ab.notify_once(ledger, url, "r") is False
    clock.now += timedelta(seconds=ab.NOTICE_RETRY_SECONDS + 1)
    items = []
    ab.set_poster(lambda owner, repo, number, body: items.append(number))
    try:
        assert ab.notify_once(ledger, url, "r") is True
        clock.now += timedelta(seconds=ab.NOTICE_RETRY_SECONDS + 1)
        assert ab.notify_once(ledger, url, "r") is False
    finally:
        ab.set_poster(None)
    assert items == [1] and read_ledger(ledger)["notified"] is True


def test_ledger_written_before_the_notice_fields_still_loads(ledger):
    pathlib.Path(ledger.path).write_text(json.dumps(GOOD), encoding="utf-8")
    ledger.check()  # no exception: the new fields are optional


def test_bad_notice_fields_make_the_ledger_unusable(ledger):
    pathlib.Path(ledger.path).write_text(json.dumps({**GOOD, "notice_attempts": -1}), encoding="utf-8")
    assert ledger.is_exhausted() is True


def test_no_notice_when_budget_is_not_used_up(ledger, posted):
    assert ab.notify_once(ledger, "https://github.com/a/b/pull/1", "r") is False
    assert posted == []


# ── PR-Agent wiring ───────────────────────────────────────────────

class FakeAgent:
    def __init__(self):
        self.handled = []

    async def _handle_request(self, pr_url, request, notify=None):
        self.handled.append((pr_url, request))
        return True


def wire(ledger, monkeypatch):
    calls = []

    async def fake_acompletion(*a, **k):
        calls.append(1)
        return SimpleNamespace(usage=usage(1000, 100))

    handler = SimpleNamespace(acompletion=fake_acompletion)
    agent_cls = type("PRAgent", (FakeAgent,), {})
    monkeypatch.setattr(ab, "_load_runtime", lambda: (handler, agent_cls))
    assert ab.apply_patch(lambda: ledger) is True
    return handler, agent_cls, calls


def test_apply_patch_is_idempotent(ledger, monkeypatch):
    handler, agent_cls, _ = wire(ledger, monkeypatch)
    wrapped = handler.acompletion
    assert ab.apply_patch(lambda: ledger) is False
    assert handler.acompletion is wrapped


def test_requests_run_normally_while_budget_remains(ledger, monkeypatch, posted):
    _, agent_cls, _ = wire(ledger, monkeypatch)
    agent = agent_cls()
    assert run(agent._handle_request("https://github.com/a/b/pull/1", "/review")) is True
    assert agent.handled == [("https://github.com/a/b/pull/1", "/review")]
    assert posted == []


def test_used_up_budget_skips_commands_and_comments_only_once(ledger, monkeypatch, posted):
    _, agent_cls, _ = wire(ledger, monkeypatch)
    ledger.record_cost(10)
    agent = agent_cls()
    for pr, command in (("1", "/review"), ("1", "/describe"), ("2", "/improve"), ("1", ["review"])):
        assert run(agent._handle_request(f"https://github.com/a/b/pull/{pr}", command)) is False
    assert agent.handled == []
    assert len(posted) == 1 and posted[0][2] == 1


def test_free_commands_still_work_when_budget_is_used_up(ledger, monkeypatch, posted):
    _, agent_cls, _ = wire(ledger, monkeypatch)
    ledger.record_cost(10)
    agent = agent_cls()
    assert run(agent._handle_request("https://github.com/a/b/pull/1", "/help")) is True
    assert posted == []


def test_budget_used_up_during_a_run_is_announced_on_that_pr(ledger, monkeypatch, posted):
    async def spends_the_rest(self, pr_url, request, notify=None):
        ledger.record_cost(10)           # e.g. the last call crossed the cap
        return True

    # Re-wire with a request handler that spends the rest of the budget.
    ab_agent = type("PRAgent", (FakeAgent,), {"_handle_request": spends_the_rest})
    monkeypatch.setattr(ab, "_load_runtime",
                        lambda: (SimpleNamespace(acompletion=_noop), ab_agent))
    ab.apply_patch(lambda: ledger)
    agent_cls = ab_agent
    run(agent_cls()._handle_request("https://github.com/a/b/pull/9", "/review"))
    assert len(posted) == 1 and posted[0][2] == 9
    run(agent_cls()._handle_request("https://github.com/a/b/pull/9", "/review"))
    assert len(posted) == 1


async def _noop(*a, **k):
    return None


def test_wrapped_litellm_call_is_blocked_when_budget_is_used_up(ledger, monkeypatch):
    handler, _, calls = wire(ledger, monkeypatch)
    run(handler.acompletion(model="openai/responses/gpt-6.1-sol"))
    assert calls == [1]
    ledger.record_cost(10)
    with pytest.raises(ab.BudgetExhausted):
        run(handler.acompletion(model="openai/responses/gpt-6.1-sol"))
    assert calls == [1]


def test_command_name_parsing():
    assert ab._command_name("/review -i") == "review"
    assert ab._command_name(["/Describe", "--x"]) == "describe"
    assert ab._command_name("") == "" and ab._command_name(None) == ""


# ── timeouts and the failure note (Argus #52) ─────────────────────

class LiteTimeout(Exception):
    """Stands in for litellm.Timeout (a subclass named 'Timeout')."""


LiteTimeout.__name__ = "Timeout"


@pytest.fixture
def cleaned():
    items = []
    ab.set_cleaner(lambda owner, repo, number, command: items.append((owner, repo, number, command)))
    yield items
    ab.set_cleaner(None)


def test_timeout_errors_are_recognised():
    assert ab.is_timeout_error(LiteTimeout("x"))
    assert ab.is_timeout_error(TimeoutError())
    assert not ab.is_timeout_error(ConnectionError("reset"))
    assert not ab.is_timeout_error(ProviderError("slow down"))


def test_a_timeout_is_charged_an_estimate_and_raised_as_model_timeout(ledger):
    async def original(*a, **k):
        raise LiteTimeout("Connection timed out")

    messages = [{"role": "user", "content": "x" * 50000}]
    with pytest.raises(ab.ModelTimeout):
        run(make_guard(ledger, original)(model="openai/responses/gpt-6.1-sol", messages=messages))
    spent = read_ledger(ledger)["spent_usd"]
    allowance = ab.TIMEOUT_OUTPUT_TOKENS * 10.00 / 1e6     # output at the gpt-6.1-sol price
    assert allowance <= spent <= 50000 * 2.00 / 1e6 + allowance + 1e-9


def test_model_timeout_is_not_an_openai_error():
    try:
        import openai
    except ImportError:
        pytest.skip("openai not installed")
    assert not issubclass(ab.ModelTimeout, openai.OpenAIError)


def test_after_a_timeout_the_same_command_never_calls_the_model_again(
        ledger, monkeypatch, posted, cleaned):
    calls = []

    async def times_out(*a, **k):
        calls.append(1)
        raise LiteTimeout("Connection timed out")

    handler = SimpleNamespace(acompletion=times_out)

    async def review(self, pr_url, request, notify=None):
        # PR-Agent retries the call, as its @retry does after wrapping the error.
        for _ in range(2):
            try:
                await handler.acompletion(model="openai/responses/gpt-6.1-sol", messages=[])
            except Exception:
                pass
        return None

    agent_cls = type("PRAgent", (FakeAgent,), {"_handle_request": review})
    monkeypatch.setattr(ab, "_load_runtime", lambda: (handler, agent_cls))
    ab.apply_patch(lambda: ledger)
    run(agent_cls()._handle_request("https://github.com/a/b/pull/190", "/review"))
    assert calls == [1]
    assert cleaned == [("a", "b", 190, "review")]
    assert len(posted) == 1
    owner, repo, number, body = posted[0]
    assert (owner, repo, number) == ("a", "b", 190)
    assert "未完成 `/review`" in body and "超时" in body and "没有重试" in body
    assert "`@argus-review review`" in body
    # The next command starts fresh and may call the model again.
    run(agent_cls()._handle_request("https://github.com/a/b/pull/190", "/review"))
    assert calls == [1, 1]


def test_no_failure_note_when_a_call_succeeded_before_a_later_one_failed(
        ledger, monkeypatch, posted, cleaned):
    outcomes = [SimpleNamespace(usage=usage(1000, 100)), ConnectionError("reset")]

    async def acompletion(*a, **k):
        item = outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    handler = SimpleNamespace(acompletion=acompletion)

    async def review(self, pr_url, request, notify=None):
        await handler.acompletion(model="m", messages=[])     # the review itself
        try:
            await handler.acompletion(model="m", messages=[])  # a side call that fails
        except ConnectionError:
            pass
        return True

    agent_cls = type("PRAgent", (FakeAgent,), {"_handle_request": review})
    monkeypatch.setattr(ab, "_load_runtime", lambda: (handler, agent_cls))
    ab.apply_patch(lambda: ledger)
    run(agent_cls()._handle_request("https://github.com/a/b/pull/1", "/review"))
    assert posted == [] and cleaned == []


def test_other_model_failures_get_a_note_naming_the_error_type(
        ledger, monkeypatch, posted, cleaned):
    async def acompletion(*a, **k):
        raise ConnectionError("reset")

    handler = SimpleNamespace(acompletion=acompletion)

    async def review(self, pr_url, request, notify=None):
        try:
            await handler.acompletion(model="m", messages=[])
        except ConnectionError:
            pass
        return None

    agent_cls = type("PRAgent", (FakeAgent,), {"_handle_request": review})
    monkeypatch.setattr(ab, "_load_runtime", lambda: (handler, agent_cls))
    ab.apply_patch(lambda: ledger)
    run(agent_cls()._handle_request("https://github.com/a/b/pull/3", "/describe"))
    assert len(posted) == 1 and "`/describe`" in posted[0][3] and "ConnectionError" in posted[0][3]
    assert "`@argus-review describe`" in posted[0][3]
    assert cleaned == [("a", "b", 3, "describe")]
    assert read_ledger(ledger)["spent_usd"] == 0      # only timeouts are charged


def test_failure_note_problems_never_raise(ledger, monkeypatch):
    def boom(*a):
        raise RuntimeError("down")

    ab.set_cleaner(boom)
    ab.set_poster(boom)
    try:
        assert ab.notify_model_failure("https://github.com/a/b/pull/1", "/review",
                                       LiteTimeout("t")) is False
    finally:
        ab.set_cleaner(None)
        ab.set_poster(None)


def test_quota_error_gets_the_budget_notice_not_a_failure_note(ledger, monkeypatch, posted, cleaned):
    async def acompletion(*a, **k):
        raise ProviderError("Error code: 429", body={"error": {"code": "insufficient_quota"}})

    handler = SimpleNamespace(acompletion=acompletion)

    async def review(self, pr_url, request, notify=None):
        try:
            await handler.acompletion(model="m", messages=[])
        except ProviderError:
            pass
        return None

    agent_cls = type("PRAgent", (FakeAgent,), {"_handle_request": review})
    monkeypatch.setattr(ab, "_load_runtime", lambda: (handler, agent_cls))
    ab.apply_patch(lambda: ledger)
    run(agent_cls()._handle_request("https://github.com/a/b/pull/4", "/review"))
    assert len(posted) == 1 and "额度已用完" in posted[0][3]
    assert cleaned == []


def test_ai_timeout_is_five_minutes():
    import tomllib
    cfg = tomllib.loads((REPO_ROOT / "configuration.toml").read_text(encoding="utf-8"))
    assert cfg["config"]["ai_timeout"] == 300


class FakeResponse:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


def test_placeholder_removal_deletes_only_this_commands_own_placeholder(monkeypatch):
    import sys as _sys
    bot = {"type": "Bot", "login": "argus-review[bot]"}
    comments = [
        {"id": 1, "user": bot, "body": "Preparing review..."},                   # removed
        {"id": 2, "user": {"type": "User", "login": "argus-review[bot]"},
         "body": "Preparing review..."},                                          # not a bot
        {"id": 3, "user": {"type": "Bot", "login": "other[bot]"},
         "body": "Preparing review..."},                                          # other bot
        {"id": 4, "user": bot, "body": "Preparing suggestions..."},              # other command
        {"id": 5, "user": bot, "body": "Preparing review... and a real review"},  # not the placeholder
        {"id": 6, "user": bot, "body": "Preparing review..."},                   # delete fails
    ]
    deleted = []

    def fake_get(url, params=None, headers=None, timeout=None):
        assert url.endswith("/repos/a/b/issues/7/comments") and params["per_page"] == 100
        return FakeResponse(200, comments)

    def fake_delete(url, headers=None, timeout=None):
        deleted.append(url.rsplit("/", 1)[-1])
        return FakeResponse(204 if url.endswith("/1") else 404)

    monkeypatch.setitem(_sys.modules, "requests",
                        SimpleNamespace(get=fake_get, delete=fake_delete))
    monkeypatch.setitem(_sys.modules, "patch_suggestion_format",
                        SimpleNamespace(_get_app_installation_token=lambda: "t"))
    assert ab._remove_temporary_comments("a", "b", 7, "review") == 1
    assert deleted == ["1", "6"]
    assert ab._remove_temporary_comments("a", "b", 7, "ask") == 0     # no known placeholder


def test_placeholder_removal_reports_a_failed_listing(monkeypatch):
    import sys as _sys
    monkeypatch.setitem(_sys.modules, "requests", SimpleNamespace(
        get=lambda *a, **k: FakeResponse(500), delete=None))
    monkeypatch.setitem(_sys.modules, "patch_suggestion_format",
                        SimpleNamespace(_get_app_installation_token=lambda: "t"))
    with pytest.raises(RuntimeError):
        ab._remove_temporary_comments("a", "b", 7, "review")
