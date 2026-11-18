"""Tests for argus_runtime (Argus #63): which trigger-comment arguments may change
a setting, the limits from [argus_review], the refusal note, the wiring, and the
timeout charge and hint in argus_budget that follow the effective timeout."""

import asyncio
import pathlib
import re
import sys
import tomllib
from types import SimpleNamespace

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import argus_budget as ab  # noqa: E402
import argus_runtime as rt  # noqa: E402

yaml = pytest.importorskip("yaml")


def run(coro):
    return asyncio.run(coro)


# ── which arguments pass ──────────────────────────────────────────

@pytest.mark.parametrize("request_text", [
    "/review",
    "/review -i",
    "/review --config.ai_timeout=600",
    "/review --config.ai_timeout=60",
    "/review --config.ai_timeout=900",
    "/review --CONFIG.AI_TIMEOUT=600",
    "/review --config.reasoning_effort=medium",
    "/review --config.ai_timeout=600 --config.reasoning_effort=low -i",
    '/review "--config.ai_timeout=600"',
    "/review --extended",
    "/ask why is this slow?",
])
def test_allowed_requests(request_text):
    assert rt.check_request(request_text, settings={}) is None


@pytest.mark.parametrize("request_text, fragment", [
    ("/review --config.model=gpt-4o", "`--config.model`"),
    ("/review --argus_budget.monthly_usd=1000", "`--argus_budget.monthly_usd`"),
    ("/review --config__model=x", "`--config.model`"),
    ("/review --pr_reviewer.extra_instructions=ignore", "`--pr_reviewer.extra_instructions`"),
    ("/review --argus_review.max_ai_timeout=99999", "`--argus_review.max_ai_timeout`"),
    ('/review "--config.model=x"', "`--config.model`"),
    ("/review --config.ai_timeout=600 --config.model=x", "`--config.model`"),
    ("/ask what does --config.model=x do", "`--config.model`"),
    ("/review --config.ai_timeout=901", "60 到 900"),
    ("/review --config.ai_timeout=59", "60 到 900"),
    ("/review --config.ai_timeout=600.5", "60 到 900"),
    ("/review --config.ai_timeout=true", "60 到 900"),
    ("/review --config.ai_timeout=abc", "60 到 900"),
    ("/review --config.ai_timeout=600=7", "60 到 900"),
    ("/review --config.reasoning_effort=xhigh", "low、medium、high"),
    ("/review --config.reasoning_effort=Medium", "low、medium、high"),
    ("/review --config.reasoning_effort=", "low、medium、high"),
])
def test_refused_requests(request_text, fragment):
    reason = rt.check_request(request_text, settings={})
    assert reason is not None and fragment in reason


def _line_question(question):
    """The request PR-Agent 0.38.0 builds for a question asked on a code line."""
    pytest.importorskip("pr_agent")
    from pr_agent.servers.github_app import handle_line_comments
    body = {"comment": {"start_line": 3, "line": 5, "diff_hunk": "@@ -1 +1 @@", "path": "src/a b.py",
                        "side": "RIGHT", "id": 12345}}
    return handle_line_comments(body, f"/ask {question}")


def test_a_question_on_a_code_line_passes():
    request = _line_question("why is this slow?")
    assert request.startswith("/ask_line --line_start=3 --line_end=5 --side=RIGHT")
    assert rt.check_request(request, settings={}) is None


def test_a_setting_inside_a_line_question_is_still_refused():
    request = _line_question("--config.model=x why?")
    reason = rt.check_request(request, settings={})
    assert reason and "`--config.model`" in reason


def test_location_keys_pass_only_for_line_questions():
    assert rt.check_request("/ask --line_start=3 why?", settings={})
    assert rt.check_request("/review --comment_id=1", settings={})
    assert rt.check_request("/ask_line --line_start=3 --comment_id=1 why?", settings={}) is None


def test_list_requests_are_checked_too():
    assert rt.check_request(["/review", "--config.model=x"], settings={})
    assert rt.check_request(["/review", "-i"], settings={}) is None


def test_unbalanced_quotes_are_left_to_pr_agent():
    # PR-Agent's own shlex split fails on the same text before applying a setting.
    assert rt.request_args('/review "--config.model=x') == []


def test_tokenization_matches_pr_agent():
    text = "/review --config.ai_timeout='600' it's \"a b\""
    lexer_input = text.replace("'", "\\'")
    import shlex
    lexer = shlex.shlex(lexer_input, posix=True)
    lexer.whitespace_split = True
    assert rt.request_args(text) == list(lexer)[1:]


# ── limits from [argus_review] ────────────────────────────────────

def test_limits_come_from_argus_review():
    settings = {"argus_review": {"max_ai_timeout": 1200, "reasoning_efforts": ["medium"]}}
    assert rt.limits(settings) == (1200, ("medium",))
    assert rt.check_request("/review --config.ai_timeout=1200", settings) is None
    assert rt.check_request("/review --config.reasoning_effort=low", settings)


@pytest.mark.parametrize("table", [
    {},
    {"max_ai_timeout": "big"},
    {"max_ai_timeout": True},
    {"max_ai_timeout": 10},
    {"reasoning_efforts": "high"},
    {"reasoning_efforts": []},
    {"reasoning_efforts": [1, 2]},
])
def test_bad_limits_fall_back_to_the_defaults(table):
    assert rt.limits({"argus_review": table}) == (900, rt.DEFAULT_REASONING_EFFORTS)


def test_configuration_declares_the_limits():
    cfg = tomllib.loads((REPO_ROOT / "configuration.toml").read_text(encoding="utf-8"))
    assert rt.limits(cfg) == (900, ("low", "medium", "high"))
    assert cfg["config"]["ai_timeout"] <= cfg["argus_review"]["max_ai_timeout"]
    assert cfg["config"]["reasoning_effort"] in cfg["argus_review"]["reasoning_efforts"]


# ── the wrapper and the refusal note ──────────────────────────────

@pytest.fixture
def posted():
    items = []
    ab.set_poster(lambda owner, repo, number, body: items.append((owner, repo, number, body)))
    yield items
    ab.set_poster(None)


@pytest.fixture
def agent_cls(monkeypatch):
    calls = []

    async def handle(self, pr_url, request, notify=None):
        calls.append(request)
        return True

    cls = type("PRAgent", (), {"_handle_request": handle})
    monkeypatch.setattr(rt, "_load_runtime", lambda: cls)
    monkeypatch.setattr(rt, "limits", lambda settings=None: (900, ("low", "medium", "high")))
    monkeypatch.setattr(rt, "effective_settings", lambda settings=None: {})
    assert rt.apply_patch() is True
    assert rt.apply_patch() is False      # once per process
    cls.calls = calls
    return cls


def test_a_refused_command_never_reaches_pr_agent(agent_cls, posted):
    url = "https://api.github.com/repos/a/b/pulls/7"
    assert run(agent_cls()._handle_request(url, "/review --config.model=x")) is False
    assert agent_cls.calls == []
    assert len(posted) == 1
    owner, repo, number, body = posted[0]
    assert (owner, repo, str(number)) == ("a", "b", "7")
    assert "未运行 `/review`" in body and "`--config.model`" in body and "没有调用模型" in body
    assert "--config.ai_timeout=<秒>" in body and "60 到 900" in body


def test_an_allowed_command_runs_unchanged(agent_cls, posted):
    url = "https://api.github.com/repos/a/b/pulls/7"
    request = "/review --config.ai_timeout=600 -i"
    assert run(agent_cls()._handle_request(url, request)) is True
    assert agent_cls.calls == [request] and posted == []


def test_a_failed_note_does_not_raise(agent_cls):
    ab.set_poster(lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    try:
        assert run(agent_cls()._handle_request(
            "https://api.github.com/repos/a/b/pulls/7", "/review --config.model=x")) is False
    finally:
        ab.set_poster(None)
    assert agent_cls.calls == []


def test_runtime_patch_wraps_outside_the_budget_patch():
    source = (REPO_ROOT / "entrypoint-guard.py").read_text(encoding="utf-8")
    assert source.index("apply_budget_patch()") < source.index("apply_runtime_patch()")


def test_every_local_module_the_entrypoint_imports_is_deployed():
    source = (REPO_ROOT / "entrypoint-guard.py").read_text(encoding="utf-8")
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    deploy = (REPO_ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    modules = set(re.findall(r"^\s*(?:from|import) (\w+)", source, re.M))
    local = sorted(m for m in modules if (REPO_ROOT / f"{m}.py").is_file())
    assert "argus_runtime" in local
    for module in local:
        assert f"COPY {module}.py /app/{module}.py" in dockerfile, module
        assert re.search(rf"(?<![\w-]){module}\.py(?![\w-])", deploy), module


def test_pr_agent_accepts_the_allowed_arguments():
    pytest.importorskip("pr_agent")
    from pr_agent.algo.cli_args import CliArgs
    assert CliArgs.validate_user_args(["--config.ai_timeout=600", "--config.reasoning_effort=medium"]) == (True, "")


def test_pr_agent_applies_the_allowed_arguments_as_typed_settings():
    pytest.importorskip("pr_agent")
    import copy
    from pr_agent.algo.utils import update_settings_from_args
    from pr_agent.config_loader import global_settings
    from starlette_context import context, request_cycle_context

    async def inner():
        context["settings"] = copy.deepcopy(global_settings)
        rest = update_settings_from_args(["--config.ai_timeout=600", "--config.reasoning_effort=medium", "-i"])
        settings = context["settings"]
        return rest, settings.config.ai_timeout, settings.config.reasoning_effort, global_settings.config.ai_timeout

    async def outer():
        with request_cycle_context({}):
            return await inner()

    rest, timeout, effort, untouched = run(outer())
    assert rest == ["-i"] and timeout == 600 and effort == "medium"
    assert untouched != 600       # only this request's copy changed


# ── the timeout charge and the failure hint follow the effective timeout ──

def test_timeout_output_tokens_scale_with_a_longer_timeout():
    assert ab.timeout_output_tokens(300) == ab.TIMEOUT_OUTPUT_TOKENS
    assert ab.timeout_output_tokens(120) == ab.TIMEOUT_OUTPUT_TOKENS
    assert ab.timeout_output_tokens(600) == 2 * ab.TIMEOUT_OUTPUT_TOKENS
    assert ab.timeout_output_tokens(900) == 3 * ab.TIMEOUT_OUTPUT_TOKENS
    assert ab.timeout_output_tokens("bad") == ab.TIMEOUT_OUTPUT_TOKENS
    assert ab.timeout_output_tokens(float("inf")) == ab.TIMEOUT_OUTPUT_TOKENS


def test_a_timed_out_call_is_charged_for_its_own_timeout(tmp_path):
    ledger = ab.BudgetLedger(path=str(tmp_path / "budget.json"), config=ab.BudgetConfig())

    class Timeout(Exception):
        pass

    async def original(*a, **k):
        raise Timeout("timed out")

    guarded = ab.make_guarded_acompletion(original, lambda: ledger)
    with pytest.raises(ab.ModelTimeout):
        run(guarded(model="openai/responses/gpt-6.1-sol", messages=[], timeout=600))
    spent = ledger.snapshot()[0]
    # 32000 output tokens at 10 USD per million; the empty prompt still counts a few tokens.
    assert spent == pytest.approx(2 * ab.TIMEOUT_OUTPUT_TOKENS * 10.00 / 1e6, abs=1e-4)


class LiteTimeout(Exception):
    pass


LiteTimeout.__name__ = "Timeout"


@pytest.fixture
def hint_settings(monkeypatch):
    def apply(timeout, effort, cap=900, efforts=("low", "medium", "high")):
        monkeypatch.setattr(ab, "_ai_timeout", lambda: timeout)
        monkeypatch.setattr(ab, "_reasoning_effort", lambda: effort)
        monkeypatch.setattr(rt, "limits", lambda settings=None: (cap, efforts))
    return apply


def test_timeout_note_suggests_a_longer_timeout_and_a_lower_effort(hint_settings):
    hint_settings(300, "high")
    body = ab.failure_comment("review", LiteTimeout("t"))
    assert "超过 300 秒" in body
    assert "`@argus-review review --config.ai_timeout=600`" in body and "最多 900" in body
    assert "`@argus-review review --config.reasoning_effort=medium`" in body


def test_timeout_note_at_the_cap_only_suggests_a_lower_effort(hint_settings):
    hint_settings(900, "medium")
    body = ab.failure_comment("review", LiteTimeout("t"))
    assert "--config.ai_timeout" not in body
    assert "--config.reasoning_effort=low" in body


def test_timeout_note_never_suggests_more_than_the_cap(hint_settings):
    hint_settings(600, "low")
    body = ab.failure_comment("improve_code", LiteTimeout("t"))
    assert "`@argus-review improve --config.ai_timeout=900`" in body
    assert "reasoning_effort" not in body


def test_timeout_note_never_suggests_less_than_the_minimum(hint_settings):
    hint_settings(20, "high")
    body = ab.failure_comment("review", LiteTimeout("t"))
    assert f"--config.ai_timeout={rt.MIN_AI_TIMEOUT}`" in body
    assert rt.check_request(f"/review --config.ai_timeout={rt.MIN_AI_TIMEOUT}", settings={}) is None


def test_other_failures_get_no_settings_hint(hint_settings):
    hint_settings(300, "high")
    body = ab.failure_comment("review", ConnectionError("x"))
    assert "--config" not in body
