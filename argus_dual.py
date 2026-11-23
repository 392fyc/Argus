"""
Argus dual review: GPT and Claude review the same diff (Mercury #821).

Every /review sends the prompt PR-Agent built to both lines at the same time:

* the GPT line is PR-Agent's own call (configuration.toml ``model``);
* the Claude line calls the Anthropic Messages API with the official SDK. Not through
  LiteLLM: PR-Agent's LiteLLM path sends a thinking ``budget_tokens`` that current Claude
  models reject, and it has no adaptive thinking or effort setting.

The two YAML answers are merged into one prediction before Argus's filters run:
findings both lines reported become one finding marked as agreed; the rest keep the
line that reported them. The review body gets a comparison section and the events log
a ``dual_review`` record, so the two lines can be compared over time.

Either line may be missing: no key, its budget spent, a timeout, a refusal or output
that cannot be parsed. The review then goes ahead on the other line alone and says so.
Only when both fail does the review fail, with the GPT line's error as before.

The Claude line has its own spend ledger and cap (``ARGUS_BUDGET_ANTHROPIC__MONTHLY_USD``
on the deployment host); without a cap it makes no call, like the GPT line.
"""

from __future__ import annotations

import asyncio
import copy
import os
from dataclasses import dataclass, replace

from argus_budget import (BudgetExhausted, BudgetLedger, ModelPrice, _count, _field,
                          _note_outcome, find_price, is_quota_error, is_timeout_error,
                          parse_config)

ANTHROPIC_LEDGER_ENV = "ARGUS_BUDGET_ANTHROPIC_PATH"
DEFAULT_ANTHROPIC_LEDGER_PATH = "/var/log/argus/budget-anthropic.json"
# claude-opus-5-5 list price per million tokens: input 4.00, cache hit 0.20, output 20.00
# (Anthropic pricing page). A 5-minute cache write costs 1.25 x input.
_DEFAULT_CLAUDE_PRICES = (ModelPrice("claude-opus-5-5", 4.00, 0.20, 20.00),)
CACHE_WRITE_FACTOR = 1.25
_PATCH_MARKER = "_argus_dual_patch"

SOURCE_BOTH, SOURCE_GPT, SOURCE_CLAUDE = "both", "gpt", "claude"
SOURCE_LABELS = {SOURCE_BOTH: "GPT 与 Claude 一致", SOURCE_GPT: "仅 GPT", SOURCE_CLAUDE: "仅 Claude"}
# Why a line produced no usable review. Fixed texts: they reach public PR comments,
# so they never carry amounts or provider messages.
STATUS_TEXT = {
    "ok": "完成",
    "disabled": "未启用",
    "no_key": "未配置 API 密钥",
    "budget": "额度已用完或未设上限",
    "quota": "服务商报告额度用完",
    "timeout": "超时",
    "refusal": "模型拒绝回答",
    "max_tokens": "输出被截断",
    "unparsed": "输出无法解析",
    "empty": "没有输出",
    "error": "调用失败",
}


class LineFailed(Exception):
    """The Claude line produced no usable review; ``status`` is a STATUS_TEXT key."""

    def __init__(self, status, detail=""):
        super().__init__(f"{status}: {detail}" if detail else status)
        self.status = status
        self.detail = detail


# ── Configuration ─────────────────────────────────────────────────

@dataclass(frozen=True)
class DualConfig:
    enabled: bool = False
    model: str = "claude-opus-5-5"
    effort: str = "high"
    max_tokens: int = 32000


def parse_dual_config(raw) -> DualConfig:
    defaults = DualConfig()
    if not raw:
        return defaults
    raw = {str(k).lower(): v for k, v in dict(raw).items()}
    enabled = raw.get("enabled", defaults.enabled)
    model = raw.get("model", defaults.model)
    effort = raw.get("effort", defaults.effort)
    max_tokens = raw.get("max_tokens", defaults.max_tokens)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1024:
        print(f"[Argus Dual] invalid max_tokens {max_tokens!r}; using {defaults.max_tokens}")
        max_tokens = defaults.max_tokens
    if effort not in ("low", "medium", "high", "xhigh", "max"):
        print(f"[Argus Dual] invalid effort {effort!r}; using {defaults.effort}")
        effort = defaults.effort
    return DualConfig(enabled=enabled is True,
                      model=str(model).strip() or defaults.model,
                      effort=effort, max_tokens=max_tokens)


def parse_claude_budget(raw):
    """The Claude ledger's BudgetConfig: the GPT ledger's rules, Claude's prices."""
    config = parse_config(raw)
    # parse_config falls back to the GPT prices when the table is missing or every
    # entry is malformed; the Claude line must never be priced at those.
    gpt_defaults = parse_config(None).prices
    if config.prices == gpt_defaults:
        return replace(config, prices=_DEFAULT_CLAUDE_PRICES)
    return config


def _global(name):
    """A table of PR-Agent's global settings. Not get_settings(): inside a request that
    copy holds the reviewed repository's .pr_agent.toml, which must not switch the
    second line on, change its model or set its cap (same rule as Argus #65)."""
    try:
        from pr_agent.config_loader import global_settings
        return global_settings.get(name)
    except Exception as e:
        print(f"[Argus Dual] settings unavailable ({e})")
        return None


_config = None
_ledger = None


def get_config() -> DualConfig:
    global _config
    if _config is None:
        _config = parse_dual_config(_global("argus_dual_review"))
    return _config


def get_claude_ledger() -> BudgetLedger:
    global _ledger
    if _ledger is None:
        path = os.environ.get(ANTHROPIC_LEDGER_ENV) or DEFAULT_ANTHROPIC_LEDGER_PATH
        _ledger = BudgetLedger(path=path, config=parse_claude_budget(_global("argus_budget_anthropic")))
    return _ledger


def _api_key():
    key = _global("anthropic.key")
    return key.strip() if isinstance(key, str) and key.strip() else None


def claude_available() -> bool:
    """Whether a review could run on the Claude line now (enabled, keyed, budget left)."""
    try:
        return get_config().enabled and _api_key() is not None and not get_claude_ledger().is_exhausted()
    except Exception:
        return False


# ── Claude call ───────────────────────────────────────────────────

def claude_cost(usage, price: ModelPrice):
    """USD cost of one Messages API call. ``input_tokens`` excludes the cached
    parts, which are billed separately (cache reads cheaper, writes dearer).
    Output tokens include thinking. None when there is no usage to price."""
    if usage is None:
        return None
    fresh = _count(_field(usage, "input_tokens"))
    read = _count(_field(usage, "cache_read_input_tokens"))
    written = _count(_field(usage, "cache_creation_input_tokens"))
    output = _count(_field(usage, "output_tokens"))
    if fresh + read + written + output == 0:
        return None
    return (fresh * price.input + read * price.cached_input
            + written * price.input * CACHE_WRITE_FACTOR + output * price.output) / 1_000_000


_SPEND_LIMIT_MARKERS = ("usage limit", "spend limit", "spending limit", "billing")
# Anthropic error types that are transient whatever the message says.
_TRANSIENT_TYPES = frozenset({"rate_limit_error", "overloaded_error", "api_error"})


def _error_type(exc):
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("type") or "").lower()
        return str(body.get("type") or "").lower()
    return ""


def _is_claude_quota_error(exc) -> bool:
    """True when the error says the account or workspace has no credit or hit its
    spend limit. A structured transient type (rate limit, overload) decides first,
    as in argus_budget.is_quota_error, so a rate-limit message that happens to
    mention billing never stops the line for the whole period."""
    status = getattr(exc, "status_code", None)
    if status == 402 or _error_type(exc) == "billing_error":
        return True
    if _error_type(exc) in _TRANSIENT_TYPES:
        return False
    if is_quota_error(exc):
        return True
    text = f"{exc} {getattr(exc, 'body', '')}".lower()
    return status in (400, 429) and any(m in text for m in _SPEND_LIMIT_MARKERS)


def _make_client(api_key, timeout):
    import anthropic
    # No SDK retries: a second try would delay the whole review, and the GPT line
    # still answers while this one is down.
    return anthropic.AsyncAnthropic(api_key=api_key, max_retries=0, timeout=timeout)


async def claude_review(system, user, config: DualConfig, ledger: BudgetLedger,
                        timeout, api_key=None, client_factory=_make_client):
    """Ask Claude for the review. Returns the answer text or raises LineFailed."""
    api_key = api_key if api_key is not None else _api_key()
    if not api_key:
        raise LineFailed("no_key")
    try:
        ledger.check()
    except BudgetExhausted as e:
        raise LineFailed("budget", e.reason)
    timeout = float(timeout or 300)
    price = find_price(config.model, ledger.config)
    client = client_factory(api_key, timeout)
    try:
        async def call():
            async with client.messages.stream(
                    model=config.model, max_tokens=config.max_tokens, system=system,
                    messages=[{"role": "user", "content": user}],
                    thinking={"type": "adaptive"},
                    output_config={"effort": config.effort}) as stream:
                return await stream.get_final_message()
        # The SDK timeout bounds each read; this bounds the whole answer.
        message = await asyncio.wait_for(call(), timeout + 15)
    except Exception as exc:
        if is_timeout_error(exc) or type(exc).__name__ == "APITimeoutError":
            ledger.record_timeout(config.model, len(system) + len(user), timeout)
            raise LineFailed("timeout") from exc
        if _is_claude_quota_error(exc):
            ledger.mark_exhausted()
            raise LineFailed("quota", type(exc).__name__) from exc
        raise LineFailed("error", type(exc).__name__) from exc
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass
    cost = claude_cost(_field(message, "usage"), price)
    ledger.record_cost(ledger.config.missing_usage_usd if cost is None else cost)
    stop = _field(message, "stop_reason")
    if stop in ("refusal", "max_tokens"):
        raise LineFailed(stop)
    text = "".join(_field(b, "text", "") or "" for b in (_field(message, "content") or [])
                   if _field(b, "type") == "text")
    if not text.strip():
        raise LineFailed("empty")
    return text


# ── Merge ─────────────────────────────────────────────────────────

def _parse(text, load_yaml):
    """(data, findings) when the answer holds a valid key_issues_to_review, else None."""
    from patch_suggestion_format import _MISSING, _REVIEW_YAML_FIX_KEYS, _normalize_findings
    if not text or not text.strip():
        return None
    try:
        data = load_yaml(text.strip(), keys_fix_yaml=_REVIEW_YAML_FIX_KEYS,
                         first_key="review", last_key="security_concerns")
    except Exception:
        return None
    review = data.get("review") if isinstance(data, dict) else None
    if not isinstance(review, dict):
        return None
    findings, valid = _normalize_findings(review.get("key_issues_to_review", _MISSING))
    return (data, findings) if valid else None


def _line_range(issue):
    try:
        start = int(issue.get("start_line") or issue.get("end_line"))
        end = int(issue.get("end_line") or start)
    except (TypeError, ValueError):
        return None
    return (min(start, end), max(start, end))


def same_finding(a, b, slack=3):
    """True when two findings from different lines are the same complaint: same file
    and overlapping lines (within ``slack``), or same file and the shared-word test
    Argus uses against its own threads. Lines decide most cases: the findings are
    written in Chinese, which the word test does not split into words."""
    from patch_suggestion_format import _finding_tokens, _norm_path
    path = _norm_path(a.get("relevant_file"))
    if not path or path != _norm_path(b.get("relevant_file")):
        return False
    ra, rb = _line_range(a), _line_range(b)
    if ra and rb and ra[0] <= rb[1] + slack and rb[0] <= ra[1] + slack:
        return True
    ta, tb = _finding_tokens(a), _finding_tokens(b)
    if not ta or not tb:
        return False
    overlap = len(ta & tb)
    return overlap >= 3 and overlap / min(len(ta), len(tb)) >= 0.4


def _rank(issue):
    from patch_suggestion_format import _SEVERITY_RANK, _finding_severity
    return _SEVERITY_RANK[_finding_severity(issue)]


def merge_findings(gpt, claude):
    """One list from both lines, each finding tagged with ``argus_source``.

    A finding both lines reported keeps the more severe of the two versions. The list
    is ordered by severity, agreed findings first within a severity, so a cap keeps
    the most severe findings and prefers the agreed ones among equals."""
    merged, used = [], set()
    for g in gpt:
        match = next((i for i, c in enumerate(claude) if i not in used and same_finding(g, c)), None)
        if match is None:
            merged.append(dict(g, argus_source=SOURCE_GPT))
            continue
        used.add(match)
        c = claude[match]
        keep = c if _rank(c) > _rank(g) else g
        merged.append(dict(keep, argus_source=SOURCE_BOTH))
    merged += [dict(c, argus_source=SOURCE_CLAUDE) for i, c in enumerate(claude) if i not in used]
    order = {SOURCE_BOTH: 0, SOURCE_GPT: 1, SOURCE_CLAUDE: 1}
    return sorted(merged, key=lambda f: (-_rank(f), order[f["argus_source"]]))


def _security_text(review):
    from patch_suggestion_format import _MISSING, _security_review_clear
    raw = review.get("security_concerns", _MISSING)
    if _security_review_clear(raw):
        return "clear"
    if raw is _MISSING or raw is None or not str(raw).strip():
        return None
    return str(raw).strip()


def merge_security(gpt_review, claude_review_):
    """A concern from either line is kept (labelled when both report one); "No" when
    at least one line says clear and neither reports a concern; else unchanged."""
    texts = {SOURCE_GPT: _security_text(gpt_review), SOURCE_CLAUDE: _security_text(claude_review_)}
    concerns = {k: v for k, v in texts.items() if v not in (None, "clear")}
    if len(concerns) == 2:
        return f"GPT：{concerns[SOURCE_GPT]}\n\nClaude：{concerns[SOURCE_CLAUDE]}"
    if concerns:
        return next(iter(concerns.values()))
    if "clear" in texts.values():
        return "No"
    return gpt_review.get("security_concerns")


def merge_predictions(gpt_text, claude_text, load_yaml, gpt_status="ok", claude_status="ok"):
    """Merge the two answers. Returns (prediction_text, stats).

    A line whose answer has no valid finding list counts as failed ("unparsed").
    With one usable line its findings are tagged and used alone; with none the GPT
    text is returned unchanged so Argus reports it as it always did."""
    import yaml
    g = _parse(gpt_text, load_yaml) if gpt_status == "ok" else None
    c = _parse(claude_text, load_yaml) if claude_status == "ok" else None
    if gpt_status == "ok" and g is None:
        gpt_status = "unparsed"
    if claude_status == "ok" and c is None:
        claude_status = "unparsed"
    stats = {"gpt_status": gpt_status, "claude_status": claude_status,
             "gpt_findings": len(g[1]) if g else 0, "claude_findings": len(c[1]) if c else 0,
             "both": 0, "gpt_only": 0, "claude_only": 0}
    if g is None and c is None:
        return gpt_text, stats
    base = copy.deepcopy((g or c)[0])
    review = base["review"]
    if g and c:
        findings = merge_findings(g[1], c[1])
        for f in findings:
            stats[{SOURCE_BOTH: "both", SOURCE_GPT: "gpt_only", SOURCE_CLAUDE: "claude_only"}[f["argus_source"]]] += 1
        review["security_concerns"] = merge_security(g[0]["review"], c[0]["review"])
    else:
        # One line only: nothing to compare, so findings carry no source tag (an
        # inline "仅 GPT" would suggest Claude looked and disagreed).
        # Ranked by severity like a merged list, so the inline cap keeps the most severe.
        findings = sorted((g or c)[1], key=lambda f: -_rank(f))
        stats["gpt_only" if g else "claude_only"] = len(findings)
    review["key_issues_to_review"] = findings
    return yaml.safe_dump(base, sort_keys=False, allow_unicode=True, width=10 ** 6), stats


def cap_findings(findings, limit):
    """(kept, cut): the first ``limit`` findings of an already ranked list."""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        return list(findings), []
    return list(findings[:limit]), list(findings[limit:])


# ── Review body ───────────────────────────────────────────────────

def source_note(issue):
    """The source line under an inline finding, or "" for a single-line review."""
    label = SOURCE_LABELS.get(issue.get("argus_source"))
    return f"<sub>来源：{label}</sub>" if label else ""


def comparison_section(stats, gpt_model, claude_model, cut=()):
    """The "双线对照" block of the review body."""
    def status(key):
        s = stats.get(key, "error")
        return STATUS_TEXT.get(s, STATUS_TEXT["error"])
    lines = ["<details>", "<summary>🔀 双线对照（GPT / Claude）</summary>", "",
             "| 线路 | 模型 | 状态 | 发现数（过滤前） |", "| --- | --- | --- | --- |",
             f"| GPT | `{gpt_model}` | {status('gpt_status')} | {stats.get('gpt_findings', 0)} |",
             f"| Claude | `{claude_model}` | {status('claude_status')} | {stats.get('claude_findings', 0)} |",
             "",
             f"两线一致 {stats.get('both', 0)} 条，仅 GPT {stats.get('gpt_only', 0)} 条，"
             f"仅 Claude {stats.get('claude_only', 0)} 条。"]
    usable = [stats.get("gpt_status") == "ok", stats.get("claude_status") == "ok"]
    if sum(usable) == 1:
        lines.append("")
        lines.append("本次只有一条线路给出了可用结果，审查按单线进行，行内评论不标来源。")
    elif sum(usable) == 0:
        lines.append("")
        lines.append("两条线路都没有给出可用结果。")
    if cut:
        lines += ["", f"因行内评论条数上限，以下 {len(cut)} 条未发为行内评论："]
        def one_line(value):
            return " ".join(str(value or "").split())
        for f in cut:
            loc = f"{one_line(f.get('relevant_file')) or '?'}:{one_line(f.get('start_line')) or '?'}"
            label = SOURCE_LABELS.get(f.get("argus_source"))
            lines.append(f"- `{loc}` {one_line(f.get('issue_header'))}"
                         + (f"（{label}）" if label else "")
                         + f"：{one_line(f.get('issue_content'))}")
    lines += ["", "</details>"]
    return "\n".join(lines)


# ── Patching ──────────────────────────────────────────────────────

def _render_prompts(reviewer):
    """The system and user prompts PRReviewer._get_prediction sends (PR-Agent 0.38.0)."""
    from jinja2 import Environment, StrictUndefined
    from pr_agent.config_loader import get_settings
    variables = copy.deepcopy(reviewer.vars)
    variables["diff"] = reviewer.patches_diff
    environment = Environment(undefined=StrictUndefined)
    system = environment.from_string(get_settings().pr_review_prompt.system).render(variables)
    user = environment.from_string(get_settings().pr_review_prompt.user).render(variables)
    return system, user


def _ai_timeout():
    try:
        from pr_agent.config_loader import get_settings
        return get_settings().config.ai_timeout
    except Exception:
        return None


def _line_status(exc):
    from argus_budget import argus_stop
    stop = argus_stop(exc)
    if isinstance(stop, BudgetExhausted):
        return "budget"
    if stop is not None or is_timeout_error(exc):
        return "timeout"
    return "error"


async def dual_prepare_prediction(reviewer, model, original, claude_call=None):
    """Replacement body of PRReviewer._prepare_prediction when dual review is on."""
    from pr_agent.algo.pr_processing import get_pr_diff
    from pr_agent.algo.utils import load_yaml
    reviewer.patches_diff = get_pr_diff(reviewer.git_provider, reviewer.token_handler, model,
                                        add_line_numbers_to_hunks=True, disable_extra_lines=False)
    if not reviewer.patches_diff:
        reviewer.prediction = None
        return
    config = get_config()
    system, user = _render_prompts(reviewer)
    claude_call = claude_call or (lambda: claude_review(
        system, user, config, get_claude_ledger(), _ai_timeout()))
    gpt_result, claude_result = await asyncio.gather(
        reviewer._get_prediction(model), claude_call(), return_exceptions=True)
    gpt_ok = not isinstance(gpt_result, BaseException)
    claude_ok = not isinstance(claude_result, BaseException)
    claude_status = "ok" if claude_ok else getattr(claude_result, "status", "error")
    if not claude_ok and not isinstance(claude_result, LineFailed):
        print(f"[Argus Dual] Claude line raised {type(claude_result).__name__}: {claude_result}")
    if not gpt_ok and not claude_ok:
        raise gpt_result
    text, stats = merge_predictions(gpt_result if gpt_ok else None,
                                    claude_result if claude_ok else None, load_yaml,
                                    gpt_status="ok" if gpt_ok else _line_status(gpt_result),
                                    claude_status=claude_status)
    if not gpt_ok:
        if stats["claude_status"] != "ok":
            raise gpt_result
        # The review goes ahead on Claude alone: the command did not fail.
        _note_outcome(None)
        print(f"[Argus Dual] GPT line failed ({stats['gpt_status']}); reviewing with Claude alone")
    elif stats["claude_status"] != "ok":
        print(f"[Argus Dual] Claude line unavailable ({stats['claude_status']}); reviewing with GPT alone")
    reviewer.prediction = text
    reviewer._argus_dual = dict(stats, gpt_model=str(model), claude_model=config.model, cut=[])
    try:
        from argus_events import emitter, EventType
        provider = reviewer.git_provider
        repo = getattr(getattr(provider, "repo", None), "full_name", None)
        emitter.emit(EventType.DUAL_REVIEW, pr_number=getattr(provider, "pr_num", None), repo=repo,
                     gpt_model=str(model), claude_model=config.model,
                     **{k: v for k, v in stats.items()})
    except Exception as e:
        print(f"[Argus Dual] event not written: {e}")


def apply_patch():
    """Run both lines inside PRReviewer._prepare_prediction, once per process."""
    from pr_agent.tools import pr_reviewer
    import argus_budget
    cls = pr_reviewer.PRReviewer
    if getattr(cls._prepare_prediction, _PATCH_MARKER, False):
        return False
    config = get_config()
    ledger = get_claude_ledger()
    try:
        import anthropic
        sdk = anthropic.__version__
    except Exception as e:
        sdk = f"unavailable ({e})"
    print(f"[Argus Dual] anthropic SDK {sdk}")
    print(f"[Argus Dual] second line: {'on, ' + config.model if config.enabled else 'off'}; "
          f"API key {'set' if _api_key() else 'missing'}; "
          f"monthly cap {'not set, no Claude call will be made' if ledger.config.monthly_usd is None else 'set'}")
    original = cls._prepare_prediction

    async def patched(self, model):
        if not get_config().enabled:
            return await original(self, model)
        return await dual_prepare_prediction(self, model, original)

    setattr(patched, _PATCH_MARKER, True)
    cls._prepare_prediction = patched
    argus_budget.set_review_alternate(claude_available)
    return True
