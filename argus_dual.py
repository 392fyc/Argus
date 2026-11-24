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
import functools
import os
import re
from dataclasses import dataclass, replace

from argus_budget import (BudgetExhausted, BudgetLedger, ModelPrice, _count, _field,
                          estimate_prompt_tokens,
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


def _never_sent(exc) -> bool:
    """True when the request provably never reached the API: the connection itself
    could not be made (DNS failure, refused connection, connect timeout). The SDK
    raises APIConnectionError / APITimeoutError from the underlying httpx error."""
    try:
        import httpx
        before_send = (httpx.ConnectError, httpx.ConnectTimeout, httpx.UnsupportedProtocol)
    except Exception:
        return False
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, before_send):
            return True
        seen.add(id(exc))
        exc = exc.__cause__  # only the explicit "raise ... from err" chain
    return False


def _rejected_before_work(exc) -> bool:
    """True for an HTTP error status the API returned instead of a response (the
    request was refused, so nothing was generated or billed). An error event inside
    a stream that had started arrives with status 200 and is not such a rejection."""
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and status >= 400


def _make_client(api_key, timeout):
    import anthropic
    # No SDK retries: a second try would delay the whole review, and the GPT line
    # still answers while this one is down.
    return anthropic.AsyncAnthropic(api_key=api_key, max_retries=0, timeout=timeout)


def _charge_estimate(ledger, model, system, user, timeout, max_tokens=None, reason="timed out"):
    """Charge a call whose usage never came back. The prompt is counted in tokens the
    way the GPT line counts its own (LiteLLM's counter; one per character only if it
    cannot count), so a failed Claude call is not charged per character. The output
    is charged at the request's own limit (``max_tokens``): the most it can have
    produced, so the estimate never falls short of the bill."""
    tokens = estimate_prompt_tokens({"model": model, "messages": [
        {"role": "system", "content": system}, {"role": "user", "content": user}]})
    return ledger.record_timeout(model, tokens, timeout, reason=reason, output_tokens=max_tokens)


def _failure_outcome(exc, ledger, model, prompt, timeout, built=True, max_tokens=None):
    """(ledger write or None, LineFailed or None to re-raise) for a failed call.
    ``prompt`` is (system, user); the write runs in a worker thread."""
    estimate = functools.partial(_charge_estimate, ledger, model, *prompt, timeout, max_tokens)
    if isinstance(exc, asyncio.CancelledError):
        # Cancelled while the request was out: the provider may still bill it.
        return functools.partial(estimate, reason="was cancelled while in flight"), None
    if not isinstance(exc, Exception):
        return None, None
    name = type(exc).__name__
    if is_timeout_error(exc) or name == "APITimeoutError":
        return (None if _never_sent(exc) else estimate), LineFailed("timeout")
    if _is_claude_quota_error(exc):
        return ledger.mark_exhausted, LineFailed("quota", name)
    if not built or _rejected_before_work(exc) or _never_sent(exc):
        # Refused, never connected, or the SDK rejected the arguments before anything
        # was sent (e.g. an SDK too old for a parameter): nothing to bill. A failure
        # after the request was built is charged, whatever its type.
        return None, LineFailed("error", name)
    # The answer may have been under way (a dropped stream, an error event
    # mid-stream): the provider can bill it and no usage comes back. Charge the same
    # estimate as a timeout; stopping early beats under-counting.
    return (functools.partial(estimate, reason=f"failed ({name}) after it may have started"),
            LineFailed("error", name))


async def _close(client):
    close = getattr(client, "close", None)
    if close is not None:
        try:
            await close()
        except Exception:
            pass


async def claude_review(system, user, config: DualConfig, ledger: BudgetLedger,
                        timeout, api_key=None, client_factory=_make_client):
    """Ask Claude for the review. Returns the answer text or raises LineFailed."""
    api_key = api_key if api_key is not None else _api_key()
    if not api_key:
        raise LineFailed("no_key")
    # Ledger calls take a file lock and write to disk: off the event loop (Argus #56).
    try:
        await asyncio.to_thread(ledger.check)
    except BudgetExhausted as e:
        raise LineFailed("budget", e.reason)
    timeout = float(timeout or 300)
    price = find_price(config.model, ledger.config)
    client = client_factory(api_key, timeout)
    try:
        built = False

        async def call():
            nonlocal built
            manager = client.messages.stream(
                model=config.model, max_tokens=config.max_tokens, system=system,
                messages=[{"role": "user", "content": user}],
                thinking={"type": "adaptive"},
                output_config={"effort": config.effort})
            built = True  # the SDK accepted the arguments; sending starts on entering
            async with manager as stream:
                return await stream.get_final_message()
        # The SDK timeout bounds each read; this bounds the whole answer.
        message = await asyncio.wait_for(call(), timeout + 15)
    except BaseException as exc:
        # Record first, then close: the write is shielded and runs in a thread, so a
        # cancellation that arrives while closing (or now) cannot drop it.
        charge, failure = _failure_outcome(exc, ledger, config.model,
                                           (system, user), timeout, built, config.max_tokens)
        try:
            if charge is not None:
                await asyncio.shield(asyncio.to_thread(charge))
        finally:
            await _close(client)
        if failure is None:
            raise
        raise failure from exc
    # The answer is in: record its usage before anything else can be interrupted.
    # Shielded, so a cancellation from here on still lets the write finish.
    cost = claude_cost(_field(message, "usage"), price)
    try:
        await asyncio.shield(asyncio.to_thread(
            ledger.record_cost, ledger.config.missing_usage_usd if cost is None else cost))
    finally:
        await _close(client)
    stop = _field(message, "stop_reason")
    if stop in ("refusal", "max_tokens"):
        raise LineFailed(stop)
    text = "".join(_field(b, "text", "") or "" for b in (_field(message, "content") or [])
                   if _field(b, "type") == "text")
    if not text.strip():
        raise LineFailed("empty")
    return text


# ── Merge ─────────────────────────────────────────────────────────

def _load(text, load_yaml):
    """The parsed answer when it has a ``review`` table, whatever else is wrong with it."""
    from patch_suggestion_format import _REVIEW_YAML_FIX_KEYS
    if not text or not text.strip():
        return None
    try:
        data = load_yaml(text.strip(), keys_fix_yaml=_REVIEW_YAML_FIX_KEYS,
                         first_key="review", last_key="security_concerns")
    except Exception:
        return None
    review = data.get("review") if isinstance(data, dict) else None
    return data if isinstance(review, dict) else None


def _parse(text, load_yaml):
    """(data, findings) when the answer holds a valid key_issues_to_review, else None."""
    from patch_suggestion_format import _MISSING, _normalize_findings
    data = _load(text, load_yaml)
    if data is None:
        return None
    findings, valid = _normalize_findings(data["review"].get("key_issues_to_review", _MISSING))
    return (data, strip_internal(findings)) if valid else None


INTERNAL_KEYS = ("argus_source", "argus_versions")


def strip_internal(findings):
    """The findings without Argus's internal fields: a model that writes them itself
    (e.g. ``argus_source: both``) must not set the source note or the pairing."""
    return [{k: v for k, v in f.items() if k not in INTERNAL_KEYS} if isinstance(f, dict) else f
            for f in findings]


def _line_range(issue):
    try:
        start = int(issue.get("start_line") or issue.get("end_line"))
        end = int(issue.get("end_line") or start)
    except (TypeError, ValueError, OverflowError):
        # Not a number, NaN, or infinite (YAML reads ".inf" as a float): no location.
        return None
    return (min(start, end), max(start, end))


# Share of features (Jaccard) two descriptions of one defect must have in common.
# Measured on hand-written pairs (Argus PR #74 review): different defects scored at
# most 0.14, the same defect between 0.00 and 0.24. Text alone cannot separate them
# well. A wrong pairing gives the wrong count and attribution, so keep the
# similarity threshold conservative.
SIMILAR_TEXT = 0.2
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*[A-Za-z0-9_]")
_CODE_REFERENCE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]{2,})`")
_SENTENCE_END = re.compile(r"[。！？；;!?]+|\.(?=\s|$)|\r?\n[ \t]*\r?\n")
_EXIT_STATUS = re.compile(
    r"(?:\bexit(?:ed|s)?(?:\s+with)?(?:\s+(?:status|code)){0,2}|退出(?:状态码?|码)?)"
    r"\s*[:=为]?\s*`?(-?[0-9]+)\b",
    re.IGNORECASE,
)
# English words and Chinese character pairs any finding may contain; they say
# nothing about which defect it is.
_STOP_WORDS = frozenset("""
the and for are was has have had not but you all any from its this that with will can may should
would could use via per out see now new old yet off let get than then their they them what when
which while your here there where because also only just more most such some other each every
being been were into without missing check checks checked does doesn did before after cause
causes causing allow allows allowing value values function functions method code line lines call
calls called return returns error errors issue issues possible bug problem potential
""".split())
_STOP_PAIRS = frozenset("""
这里 没有 导致 可能 处理 问题 如果 需要 应该 因为 建议 存在 以及 进行 使用 调用 当前 这个 一个
时会 会导 时候 的时 没处 未处 后会 并且 但是 直接 会被 返回 函数 参数 检查 校验 错误 异常
""".split())


def _features(text):
    """Identifiers and words (3+ letters) plus pairs of adjacent Chinese characters,
    without the generic ones. Letters are never split into pairs: common letter pairs
    would make any two English sentences look alike."""
    text = str(text or "")
    feats = set()
    for word in _WORD.findall(text):
        word = word.lower().strip("._")
        if len(word) >= 3 and word not in _STOP_WORDS:
            feats.add("w:" + word)
    for run in _CJK_RUN.findall(text):
        for a, b in zip(run, run[1:]):
            if a + b not in _STOP_PAIRS:
                feats.add("z:" + a + b)
    return feats


def text_similarity(a, b):
    """Share of the two descriptions' features they have in common (Jaccard, 0 to 1)."""
    fa, fb = _features(a.get("issue_content")), _features(b.get("issue_content"))
    if not fa or not fb:
        return 0.0
    return len(fa & fb) / len(fa | fb)


def _anchored_sentences(issue):
    sentences = _SENTENCE_END.split(str(issue.get("issue_content") or ""))
    anchored = []
    for sentence in sentences:
        references = set(_CODE_REFERENCE.findall(sentence))
        if references:
            words = _features(_CODE_REFERENCE.sub(" ", sentence))
            anchored.append((references, words, set(_EXIT_STATUS.findall(sentence))))
    return anchored


def _agree_on_code_references(a, b):
    """Require a shared exit status and separate evidence about the mechanism.

    In two distinct sentence pairs, each pair must share wording and a code
    reference, with no shared reference used across both pairs. One pair must
    describe the same exit status. This lets a short report corroborate a
    longer report that describes the same command behavior.
    """
    left, right = _anchored_sentences(a), _anchored_sentences(b)
    matched = []
    for i, (refs_a, words_a, statuses_a) in enumerate(left):
        for j, (refs_b, words_b, statuses_b) in enumerate(right):
            shared = refs_a & refs_b
            if not shared or len(words_a & words_b) < 4:
                continue
            same_status = len(statuses_a) == 1 and statuses_a == statuses_b
            if any(i != x and j != y and shared.isdisjoint(refs) and (same_status or status)
                   for x, y, refs, status in matched):
                return True
            matched.append((i, j, shared, same_status))
    return False


def same_finding(a, b, slack=3, *, use_code_references=True):
    """True when two findings from different lines are the same complaint, on the same
    file. Nearby lines (within ``slack``) only make two findings candidates: their
    descriptions must share enough wording. For overlapping locations, reporting
    the same exit status and mechanism can corroborate a match.
    Two findings that both name lines further apart than that are different places,
    and stay separate even when they share identifiers (one function can have two
    defects in two branches). Only when a finding names no lines does the shared-word
    test Argus uses against its own threads decide. A heuristic: two models may
    describe one defect in very different words, which then shows up as two one-sided
    findings rather than one agreed finding."""
    from patch_suggestion_format import _finding_tokens, _norm_path
    path = _norm_path(a.get("relevant_file"))
    if not path or path != _norm_path(b.get("relevant_file")):
        return False
    ra, rb = _line_range(a), _line_range(b)
    if ra and rb:
        if ra[0] <= rb[1] + slack and rb[0] <= ra[1] + slack:
            if text_similarity(a, b) >= SIMILAR_TEXT:
                return True
            overlap = max(ra[0], rb[0]) <= min(ra[1], rb[1])
            return use_code_references and overlap and _agree_on_code_references(a, b)
        return False
    ta, tb = _finding_tokens(a), _finding_tokens(b)
    if not ta or not tb:
        return False
    overlap = len(ta & tb)
    return overlap >= 3 and overlap / min(len(ta), len(tb)) >= 0.4


def match_score(a, b):
    """How closely two matching findings agree: the share of character pairs their
    descriptions have in common (0 to 1) plus how much their line ranges overlap (1 for the same
    lines, below 0 the further apart they are). Higher is closer. Used only to choose
    among findings that same_finding already accepts."""
    shared = text_similarity(a, b)
    ra, rb = _line_range(a), _line_range(b)
    closeness = 0.0
    if ra and rb:
        overlap = min(ra[1], rb[1]) - max(ra[0], rb[0]) + 1
        span = max(ra[1], rb[1]) - min(ra[0], rb[0]) + 1
        closeness = overlap / span if overlap > 0 else (overlap - 1) / span
    return shared + closeness


def _rank(issue):
    from patch_suggestion_format import _SEVERITY_RANK, _finding_severity
    return _SEVERITY_RANK[_finding_severity(issue)]


MAX_EXACT_PAIRING = 12  # Claude findings; 2**12 states per GPT finding


def _best_pairing(edges, claude_count):
    """Maximize pair count, then total match score. Use greedy ranking above the state limit."""
    if claude_count > MAX_EXACT_PAIRING:
        pairs, taken = {}, set()
        for score, gi, ci in sorted(((s, gi, ci) for gi, es in edges.items() for ci, s in es),
                                    key=lambda e: (-e[0], e[1], e[2])):
            if gi not in pairs and ci not in taken:
                pairs[gi] = ci
                taken.add(ci)
        return pairs
    # best[mask] = (pair count, total score, pairing) using the GPT findings seen so far
    best = {0: (0, 0.0, ())}
    for gi, candidates in edges.items():
        step = dict(best)
        for mask, (count, total, chosen) in best.items():
            for ci, score in candidates:
                if mask >> ci & 1:
                    continue
                key = mask | 1 << ci
                value = (count + 1, total + score, chosen + ((gi, ci),))
                if key not in step or value[:2] > step[key][:2]:
                    step[key] = value
        best = step
    winner = max(best.values(), key=lambda v: (v[0], v[1]))
    return dict(winner[2])


def best_pairing(gpt, claude):
    """Pair ordinary matches first. Use reference-based matches only for the remaining findings."""
    def pair(gpt_indices, claude_indices, *, use_code_references):
        edges = {
            gi: [(ci, match_score(gpt[gi], claude[ci])) for ci in claude_indices
                 if same_finding(gpt[gi], claude[ci], use_code_references=use_code_references)]
            for gi in gpt_indices
        }
        return _best_pairing(edges, len(claude_indices))

    pairs = pair(range(len(gpt)), range(len(claude)), use_code_references=False)
    if len(pairs) < min(len(gpt), len(claude)):
        taken = set(pairs.values())
        pairs.update(pair([gi for gi in range(len(gpt)) if gi not in pairs],
                          [ci for ci in range(len(claude)) if ci not in taken],
                          use_code_references=True))
    return pairs


def _is_security(finding):
    header = str(finding.get("issue_header") or "").lower()
    return "security" in header or "安全" in header


def _combine(g, c):
    """One agreed finding from the two versions. The header (and location) of the
    more severe one (a security header wins a tie: the merged severity never drops
    below either version's, so the severity filter cannot drop it); the other version's
    description is kept below, and so is its location when it differs (two call sites
    a few lines apart described in the same words), so a pair matched wrongly hides
    nothing."""
    g_first = (_rank(g), _is_security(g)) >= (_rank(c), _is_security(c))
    keep, other, other_name = (g, c, "Claude") if g_first else (c, g, "GPT")
    item = dict(keep, argus_source=SOURCE_BOTH)
    mine = str(keep.get("issue_content") or "").strip()
    theirs = str(other.get("issue_content") or "").strip()
    if theirs and theirs != mine:
        header = str(other.get("issue_header") or "").strip()
        label = f"{other_name} 的描述" + (f"（{header}）" if header else "")
        mine = f"{mine}\n\n{label}：{theirs}" if mine else theirs
    here, there = _line_range(keep), _line_range(other)
    if there and there != here:
        lines = f"第 {there[0]} 行" if there[0] == there[1] else f"第 {there[0]}-{there[1]} 行"
        mine = f"{mine}\n\n{other_name} 标注的位置：{lines}" if mine else f"{other_name} 标注的位置：{lines}"
    item["issue_content"] = mine
    # The two versions as reported, so the repeat filter can judge each on its own
    # (split_repeated_pairs); removed before the finding is posted.
    item[VERSIONS_KEY] = [dict(g, argus_source=SOURCE_GPT), dict(c, argus_source=SOURCE_CLAUDE)]
    return item


VERSIONS_KEY = "argus_versions"


def split_repeated_pairs(findings, repeats):
    """Judge each version of an agreed finding against the existing threads.

    ``repeats(finding)`` returns a drop reason when the finding repeats an existing
    thread, else None. Only the two versions decide, never the merged text (whose
    words are the union of both and can look like a repeat when neither is):
    both repeat, the pair is dropped; one repeats, the new version is kept on its
    own (tagged with its line) and the other dropped; neither repeats, the pair is
    kept and its index returned in ``judged`` so the repeat test is not run on the
    merged text again. Returns (findings, dropped, judged)."""
    out, dropped, judged = [], [], set()
    for f in findings:
        versions = f.get(VERSIONS_KEY)
        if not (isinstance(versions, list) and len(versions) == 2):
            out.append(f)
            continue
        reasons = [repeats(v) for v in versions]
        if reasons[0] and reasons[1]:
            dropped.append((f, reasons[0]))
        elif reasons[0] or reasons[1]:
            new = 1 if reasons[0] else 0
            out.append(versions[new])
            dropped.append((versions[1 - new], reasons[1 - new]))
        else:
            judged.add(len(out))
            out.append(f)
    return out, dropped, judged


def strip_versions(findings):
    """The findings without the internal VERSIONS_KEY, ready to be posted."""
    return [{k: v for k, v in f.items() if k != VERSIONS_KEY} for f in findings]


def merge_findings(gpt, claude):
    """One list from both lines, each finding tagged with ``argus_source``.

    A finding both lines reported keeps the more severe version's header and both
    descriptions (_combine), so a wrong pairing never hides a finding. The list
    is ordered by severity, agreed findings first within a severity, so a cap keeps
    the most severe findings and prefers the agreed ones among equals."""
    pair_of = best_pairing(gpt, claude)
    used = set(pair_of.values())
    merged = []
    for gi, g in enumerate(gpt):
        if gi not in pair_of:
            merged.append(dict(g, argus_source=SOURCE_GPT))
            continue
        c = claude[pair_of[gi]]
        merged.append(_combine(g, c))
    merged += [dict(c, argus_source=SOURCE_CLAUDE) for i, c in enumerate(claude) if i not in used]
    return rank_findings(merged)


def rank_findings(findings):
    """Most severe first, agreed findings first among equals (stable otherwise)."""
    return sorted(findings, key=lambda f: (-_rank(f), 0 if f.get("argus_source") == SOURCE_BOTH else 1))


def _security_text(review):
    from patch_suggestion_format import _MISSING, _security_review_clear
    raw = review.get("security_concerns", _MISSING)
    if _security_review_clear(raw):
        return "clear"
    if raw is _MISSING or raw is None or not str(raw).strip():
        return None
    return str(raw).strip()


_NO_VALUE = object()


def merge_security(gpt_review, claude_review_, usable=(SOURCE_GPT, SOURCE_CLAUDE)):
    """The merged security_concerns, or _NO_VALUE to leave the field out.

    A concern either answer states is kept, even from an answer whose finding list is
    unusable (labelled when both report one). "No" only when every usable line says so
    explicitly: a usable answer without the field (output cut off before it) leaves the
    field out, so Argus still treats the review as incomplete."""
    reviews = {SOURCE_GPT: gpt_review, SOURCE_CLAUDE: claude_review_}
    texts = {k: _security_text(r) if isinstance(r, dict) else None for k, r in reviews.items()}
    concerns = {k: v for k, v in texts.items() if v not in (None, "clear")}
    if len(concerns) == 2:
        return f"GPT：{concerns[SOURCE_GPT]}\n\nClaude：{concerns[SOURCE_CLAUDE]}"
    if concerns:
        return next(iter(concerns.values()))
    if usable and all(texts[k] == "clear" for k in usable):
        return "No"
    return _NO_VALUE


def merge_predictions(gpt_text, claude_text, load_yaml, gpt_status="ok", claude_status="ok"):
    """Merge the two answers. Returns (prediction_text, stats).

    A line whose answer has no valid finding list counts as failed ("unparsed").
    With one usable line its findings are used alone; with none the GPT text goes on
    so Argus reports it as incomplete as it always did, carrying any security concern
    either answer stated."""
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
    # Security is read from both answers, also one whose finding list is unusable.
    texts = {SOURCE_GPT: gpt_text, SOURCE_CLAUDE: claude_text}
    loaded = {k: _load(t, load_yaml) if t else None for k, t in texts.items()}
    reviews = {k: d["review"] if d else None for k, d in loaded.items()}
    usable = tuple(k for k, parsed in ((SOURCE_GPT, g), (SOURCE_CLAUDE, c)) if parsed)
    security = merge_security(reviews[SOURCE_GPT], reviews[SOURCE_CLAUDE], usable)
    if g is None and c is None:
        # No usable finding list: the review stays incomplete as before, but a
        # concern either answer states is not lost. It goes into the GPT answer
        # (or Claude's when GPT has no review table); the finding list stays invalid.
        if security is _NO_VALUE:
            return gpt_text, stats
        carrier = loaded[SOURCE_GPT] or loaded[SOURCE_CLAUDE]
        if carrier is None:
            return gpt_text, stats
        carrier = copy.deepcopy(carrier)
        carrier["review"]["security_concerns"] = security
        return yaml.safe_dump(carrier, sort_keys=False, allow_unicode=True, width=10 ** 6), stats
    base = copy.deepcopy((g or c)[0])
    review = base["review"]
    if security is _NO_VALUE:
        review.pop("security_concerns", None)
    else:
        review["security_concerns"] = security
    if g and c:
        findings = merge_findings(g[1], c[1])
        for f in findings:
            stats[{SOURCE_BOTH: "both", SOURCE_GPT: "gpt_only", SOURCE_CLAUDE: "claude_only"}[f["argus_source"]]] += 1
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
    elif stats.get("gpt_only", 0) and stats.get("claude_only", 0):
        lines.append("")
        lines.append("“一致”要求同一文件、相近行号和相似描述。"
                     "当一份描述比另一份详细时，共同的代码引用也可作为依据。"
                     "匹配偏保守，措辞差别大的同一问题可能分别计为单边发现。")
    elif sum(usable) == 0:
        lines.append("")
        lines.append("两条线路都没有给出可用结果。")
    if stats.get("split"):
        lines += ["", f"有 {stats['split']} 条“一致”发现的一半与已有讨论重复，"
                      "另一半作为单边发现单独发布（行内标为仅一方）。"]
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
        _emit(reviewer, model, config, {"gpt_status": _line_status(gpt_result),
                                        "claude_status": claude_status})
        raise gpt_result
    text, stats = merge_predictions(gpt_result if gpt_ok else None,
                                    claude_result if claude_ok else None, load_yaml,
                                    gpt_status="ok" if gpt_ok else _line_status(gpt_result),
                                    claude_status=claude_status)
    if not gpt_ok:
        if stats["claude_status"] != "ok":
            _emit(reviewer, model, config, stats)
            raise gpt_result
        # The review goes ahead on Claude alone: the command did not fail.
        _note_outcome(None)
        print(f"[Argus Dual] GPT line failed ({stats['gpt_status']}); reviewing with Claude alone")
    elif stats["gpt_status"] != "ok" and stats["claude_status"] != "ok":
        # Neither answer is usable, but the GPT call returned: its text goes on as it
        # did before dual review, and Argus reports it as incomplete output (no
        # approval wording) instead of a failed command.
        print(f"[Argus Dual] neither line usable (GPT {stats['gpt_status']}, Claude "
              f"{stats['claude_status']}); passing the GPT answer on unchanged, as before dual review")
    elif stats["claude_status"] != "ok":
        print(f"[Argus Dual] Claude line unavailable ({stats['claude_status']}); reviewing with GPT alone")
    elif stats["gpt_status"] != "ok":
        print(f"[Argus Dual] GPT answer unusable ({stats['gpt_status']}); reviewing with Claude alone")
    reviewer.prediction = text
    reviewer._argus_dual = dict(stats, gpt_model=str(model), claude_model=config.model, cut=[])
    _emit(reviewer, model, config, stats)


def _emit(reviewer, model, config, stats):
    """One dual_review event per review, failed ones included, so the log can
    compare how often each line was usable."""
    try:
        from argus_events import emitter, EventType
        provider = reviewer.git_provider
        repo = getattr(getattr(provider, "repo", None), "full_name", None)
        emitter.emit(EventType.DUAL_REVIEW, pr_number=getattr(provider, "pr_num", None), repo=repo,
                     gpt_model=str(model), claude_model=config.model, **dict(stats))
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
