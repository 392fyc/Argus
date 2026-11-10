"""
Argus monthly spend cap (Argus #49).

Every LLM call Argus makes goes through LiteLLM's ``acompletion`` (PR-Agent's
handler calls it, and so does the reply judge). This module wraps that one
function so that:

* before a call, a spent-up budget stops the call (``BudgetExhausted``);
* after a call, the returned token usage is priced and added to a ledger;
* a provider "quota used up" error marks the period as used up and is not retried;
* a call that runs past ``ai_timeout`` is not retried either: it is charged an
  estimate (the provider may still bill it) and the command posts one short
  failure note instead of leaving "Preparing review..." behind (Argus #52).

It also wraps ``PRAgent._handle_request`` so that, once the budget is used up,
a command is skipped and the PR gets exactly one comment per period saying so.

The ledger is a small JSON file on the mounted log volume. Reads and writes are
serialised with a thread lock plus an OS file lock, and every write goes to a
temporary file that is then renamed over the ledger, so a crash never leaves a
half-written file. A ledger that cannot be read counts as "used up": stopping
too early is cheaper than spending without a limit.

"Check before the call" cannot know what the call will cost, so the cap can be
exceeded by the cost of the calls that were already in flight when the last
check passed (one call when requests run one after another).
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps

from argus_events import emitter, EventType

LEDGER_ENV = "ARGUS_BUDGET_PATH"
DEFAULT_LEDGER_PATH = "/var/log/argus/budget.json"
LEDGER_VERSION = 1
_PATCH_MARKER = "_argus_budget_patch"
MAX_NOTICE_ATTEMPTS = 3      # tries to post the "used up" comment per period
NOTICE_RETRY_SECONDS = 120   # wait before another try after one that did not finish
# Output tokens charged for a call that timed out (its usage never comes back):
# about the largest review output seen, reasoning included (Argus #52).
TIMEOUT_OUTPUT_TOKENS = 16000

# Commands that never call the model; they stay available when the budget is spent.
_FREE_COMMANDS = frozenset({"help", "config", "settings"})
# Provider error texts that mean "the account has no quota/credit left", as
# opposed to an ordinary per-minute rate limit (which must not stop Argus).
_QUOTA_MARKERS = (
    "insufficient_quota",
    "credit_balance_exhausted",
    "billing_hard_limit_reached",
    "exceeded your current quota",
    "credit balance is too low",
)
# Fixed offsets used only if the container has no tz database (slim images).
_FIXED_OFFSETS = {"Asia/Tokyo": 9, "UTC": 0, "Etc/UTC": 0}


class BudgetExhausted(Exception):
    """Raised instead of calling the model when the period's budget is used up.

    Deliberately not an ``openai`` exception, so no retry logic treats it as a
    transient provider error.
    """

    def __init__(self, reason, spent_usd=None, next_reset=None):
        super().__init__(reason)
        self.reason = reason
        self.spent_usd = spent_usd
        self.next_reset = next_reset


class ModelTimeout(Exception):
    """Raised in place of a provider timeout.

    PR-Agent retries ``openai.APIError`` and wraps other exceptions in it, so the
    type alone does not stop a retry; the guard also refuses every later call in
    the same command once one call timed out (see ``make_guarded_acompletion``).
    """


# ── Configuration ─────────────────────────────────────────────────

@dataclass(frozen=True)
class ModelPrice:
    """USD per one million tokens."""
    model: str
    input: float
    cached_input: float
    output: float


# gpt-6.1-sol list price: input 2.00, cached input 0.10, output 10.00 (OpenAI
# pricing page, as given in Argus #49). Used when configuration.toml has no table.
_DEFAULT_PRICES = (ModelPrice("gpt-6.1-sol", 2.00, 0.10, 10.00),)


@dataclass(frozen=True)
class BudgetConfig:
    monthly_usd: float = 10.0
    reset_day: int = 15          # 1..28: the day of the month a new period starts
    timezone: str = "Asia/Tokyo"
    prices: tuple = _DEFAULT_PRICES
    # Charged when a successful call returns no usage, so it is never free.
    missing_usage_usd: float = 0.10


def _positive_number(value, default):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if not math.isfinite(value) or value <= 0:
        return default
    return float(value)


def _non_negative_number(value, default):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if not math.isfinite(value) or value < 0:
        return default
    return float(value)


def _lower_keys(mapping):
    return {str(k).lower(): v for k, v in dict(mapping).items()}


def parse_config(raw) -> BudgetConfig:
    """Build a BudgetConfig from the ``[argus_budget]`` table; bad values fall
    back to the defaults (the cap stays in force rather than turning off)."""
    defaults = BudgetConfig()
    if not raw:
        return defaults
    raw = _lower_keys(raw)  # dynaconf may hand back upper-case keys
    reset_day = raw.get("reset_day", defaults.reset_day)
    if isinstance(reset_day, bool) or not isinstance(reset_day, int) or not 1 <= reset_day <= 28:
        print(f"[Argus Budget] invalid reset_day {reset_day!r}; using {defaults.reset_day}")
        reset_day = defaults.reset_day
    tz = raw.get("timezone", defaults.timezone)
    if not isinstance(tz, str) or not tz.strip():
        tz = defaults.timezone
    prices = []
    for entry in raw.get("prices") or []:
        try:
            entry = _lower_keys(entry)
            prices.append(ModelPrice(
                model=str(entry["model"]).strip(),
                input=float(entry["input"]),
                cached_input=float(entry.get("cached_input", entry["input"])),
                output=float(entry["output"]),
            ))
        except (KeyError, TypeError, ValueError):
            print(f"[Argus Budget] ignoring malformed price entry: {entry!r}")
    return BudgetConfig(
        monthly_usd=_positive_number(raw.get("monthly_usd"), defaults.monthly_usd),
        reset_day=reset_day,
        timezone=tz.strip(),
        prices=tuple(prices) or defaults.prices,
        missing_usage_usd=_non_negative_number(
            raw.get("missing_usage_usd"), defaults.missing_usage_usd),
    )


def load_config() -> BudgetConfig:
    """Read ``[argus_budget]`` from PR-Agent's settings (configuration.toml)."""
    try:
        from pr_agent.config_loader import get_settings
        return parse_config(get_settings().get("argus_budget"))
    except Exception as e:
        print(f"[Argus Budget] settings unavailable ({e}); using defaults")
        return BudgetConfig()


def _resolve_tz(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        pass
    if name in _FIXED_OFFSETS:
        return timezone(timedelta(hours=_FIXED_OFFSETS[name]), name)
    print(f"[Argus Budget] unknown timezone {name!r}; using UTC")
    return timezone.utc


def period_bounds(now: datetime, config: BudgetConfig):
    """Return (start, next_reset) of the budget period containing ``now``.

    A period starts at 00:00 on ``reset_day`` in the configured timezone and
    lasts until the same moment of the next month. At 00:00 on the reset day
    itself a new period has already begun.
    """
    tz = _resolve_tz(config.timezone)
    local = now.astimezone(tz)
    year, month = local.year, local.month
    if local.day < config.reset_day:
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    start = datetime(year, month, config.reset_day, tzinfo=tz)
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return start, datetime(ny, nm, config.reset_day, tzinfo=tz)


# ── Pricing ───────────────────────────────────────────────────────

def _field(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _count(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(int(value), 0)


def normalize_model(model) -> str:
    """'openai/responses/gpt-6.1-sol' -> 'gpt-6.1-sol'."""
    return str(model or "").rsplit("/", 1)[-1].strip().lower()


def find_price(model, config: BudgetConfig) -> ModelPrice:
    """Price for ``model``; an unlisted model is billed at the dearest listed
    price (stopping early beats under-counting)."""
    name = normalize_model(model)
    best = None
    for price in config.prices:
        key = price.model.lower()
        if name == key:
            return price
        if name.startswith(key) and (best is None or len(key) > len(best.model)):
            best = price  # e.g. a dated snapshot 'gpt-6.1-sol-2026-...'
    if best is not None:
        return best
    print(f"[Argus Budget] no price for model {model!r}; using the highest listed price")
    return max(config.prices, key=lambda p: (p.output, p.input))


def compute_cost(usage, price: ModelPrice):
    """USD cost of one call from a LiteLLM/OpenAI ``usage`` (object or dict).

    ``prompt_tokens`` includes the cached part, which is billed at the cached
    rate instead. ``completion_tokens`` already counts reasoning tokens (LiteLLM
    maps the Responses API ``output_tokens`` to it); if a provider reported
    reasoning tokens beyond it, they are added so output is never under-counted.
    Returns None when there is no usage to price.
    """
    if usage is None:
        return None
    prompt = _count(_field(usage, "prompt_tokens"))
    completion = _count(_field(usage, "completion_tokens"))
    cached = _count(_field(_field(usage, "prompt_tokens_details"), "cached_tokens"))
    reasoning = _count(_field(_field(usage, "completion_tokens_details"), "reasoning_tokens"))
    if prompt == 0 and completion == 0:
        return None
    cached = min(cached, prompt)
    output = completion if completion >= reasoning else completion + reasoning
    return ((prompt - cached) * price.input
            + cached * price.cached_input
            + output * price.output) / 1_000_000


_QUOTA_CODES = frozenset({"insufficient_quota", "credit_balance_exhausted",
                          "billing_hard_limit_reached"})
_RATE_LIMIT_CODES = frozenset({"rate_limit_exceeded", "rate_limit_error"})
_CODE_RE = re.compile(r"""["']?(?:code|type)["']?\s*[:=]\s*["']([a-z_]+)["']""")


def is_quota_error(exc) -> bool:
    """True for a provider error saying the account's quota/credit is gone.

    A structured error code decides first: a quota code means used up, an
    ordinary rate-limit code (``rate_limit_exceeded``) means not, whatever the
    message says. Without such a code, the known quota phrases decide.
    """
    parts = [str(exc), repr(getattr(exc, "body", None)), str(getattr(exc, "code", ""))]
    response = getattr(exc, "response", None)
    with contextlib.suppress(Exception):
        parts.append(response.text)
    text = " ".join(parts).lower()
    codes = set(_CODE_RE.findall(text)) | {str(getattr(exc, "code", "")).lower()}
    if codes & _QUOTA_CODES:
        return True
    if codes & _RATE_LIMIT_CODES:
        return False
    return any(marker in text for marker in _QUOTA_MARKERS)


# ── Ledger ────────────────────────────────────────────────────────

@dataclass
class LedgerState:
    period_start: datetime
    spent_usd: float = 0.0
    exhausted: bool = False
    notified: bool = False
    notice_attempts: int = 0                 # tries to post the notice this period
    notice_claimed_at: datetime | None = None  # when the latest try started


@contextlib.contextmanager
def _file_lock(lock_path, timeout=10.0):
    """Cross-process exclusive lock on ``lock_path`` (msvcrt on Windows, flock elsewhere)."""
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    handle = open(lock_path, "a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"could not lock {lock_path}")
                time.sleep(0.01)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


class _LedgerCorrupt(Exception):
    pass


class BudgetLedger:
    """The persistent record of what this period has cost."""

    def __init__(self, path=None, config: BudgetConfig = None, clock=None):
        self.path = path or os.environ.get(LEDGER_ENV) or DEFAULT_LEDGER_PATH
        self.config = config or BudgetConfig()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._thread_lock = threading.RLock()
        self._degraded = None      # reason string while the ledger cannot be written
        self._notified_mem = set()  # periods already announced by this process

    # -- low level ---------------------------------------------------

    @contextlib.contextmanager
    def _locked(self):
        with self._thread_lock:
            with _file_lock(self.path + ".lock"):
                yield

    def _fresh_state(self, start):
        return LedgerState(period_start=start)

    def _read(self):
        """Return the stored state, or None when no ledger exists yet.

        Raises _LedgerCorrupt for a file that exists but is not a valid ledger.
        """
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            raise _LedgerCorrupt(f"unreadable ledger: {e}")
        try:
            if not isinstance(raw, dict) or raw.get("version") != LEDGER_VERSION:
                raise ValueError("unexpected structure or version")
            start = datetime.fromisoformat(raw["period_start"])
            if start.tzinfo is None:
                raise ValueError("period_start has no timezone")
            spent = raw["spent_usd"]
            if isinstance(spent, bool) or not isinstance(spent, (int, float)) \
                    or not math.isfinite(spent) or spent < 0:
                raise ValueError("invalid spent_usd")
            exhausted, notified = raw["exhausted"], raw["notified"]
            if not isinstance(exhausted, bool) or not isinstance(notified, bool):
                raise ValueError("invalid flags")
        except (KeyError, TypeError, ValueError) as e:
            raise _LedgerCorrupt(f"invalid ledger: {e}")
        # Optional fields: a ledger written before they existed still loads.
        attempts = raw.get("notice_attempts", 0)
        claimed = raw.get("notice_claimed_at")
        try:
            if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
                raise ValueError("invalid notice_attempts")
            claimed_at = datetime.fromisoformat(claimed) if claimed else None
            if claimed_at is not None and claimed_at.tzinfo is None:
                raise ValueError("notice_claimed_at has no timezone")
        except (TypeError, ValueError) as e:
            raise _LedgerCorrupt(f"invalid ledger: {e}")
        return LedgerState(start, float(spent), exhausted, notified, attempts, claimed_at)

    def _write(self, state: LedgerState):
        """Write the ledger atomically: temp file, flush to disk, rename."""
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        tmp = os.path.join(directory, f".{os.path.basename(self.path)}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        payload = {
            "version": LEDGER_VERSION,
            "period_start": state.period_start.isoformat(),
            "spent_usd": round(state.spent_usd, 6),
            "exhausted": state.exhausted,
            "notified": state.notified,
            "notice_attempts": state.notice_attempts,
            "notice_claimed_at": state.notice_claimed_at.isoformat() if state.notice_claimed_at else None,
        }
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            with contextlib.suppress(OSError):
                os.remove(tmp)

    def _current(self):
        """Load the state for the current period (rolling over if needed).

        Returns (state, changed): changed means the in-memory state differs
        from what is on disk and should be saved. Raises _LedgerCorrupt.
        """
        now = self._clock()
        start, _ = period_bounds(now, self.config)
        state = self._read()
        if state is None:
            return self._fresh_state(start), True
        if state.period_start.astimezone(timezone.utc) < start.astimezone(timezone.utc):
            # A new period began: spending, the used-up flag and the notice all reset.
            return self._fresh_state(start), True
        # A stored period that is not older than the current one is kept as it
        # is (a clock that stepped backwards must not reopen a spent budget).
        return state, False

    def _is_spent(self, state):
        return state.exhausted or state.spent_usd >= self.config.monthly_usd

    def next_reset(self):
        return period_bounds(self._clock(), self.config)[1]

    # -- public operations -------------------------------------------

    def check(self):
        """Raise BudgetExhausted when no call may be made now."""
        try:
            with self._locked():
                try:
                    state, changed = self._current()
                except _LedgerCorrupt as e:
                    raise BudgetExhausted(f"budget ledger {self.path} is unusable: {e}",
                                          next_reset=self.next_reset())
                if changed or self._degraded:
                    self._write(state)  # also probes that the ledger is writable
                    self._degraded = None
                if self._is_spent(state):
                    raise BudgetExhausted(
                        f"budget used up: ${state.spent_usd:.4f} of ${self.config.monthly_usd:.2f}"
                        if not state.exhausted else "provider reported the quota used up",
                        spent_usd=state.spent_usd, next_reset=self.next_reset())
        except BudgetExhausted:
            raise
        except (OSError, TimeoutError) as e:
            self._degraded = f"ledger not writable: {e}"
            raise BudgetExhausted(f"budget ledger {self.path} is not usable: {e}",
                                  next_reset=self.next_reset())

    def is_exhausted(self) -> bool:
        try:
            self.check()
            return False
        except BudgetExhausted:
            return True

    def record_cost(self, usd):
        """Add ``usd`` to this period's spending."""
        try:
            with self._locked():
                state, _ = self._current()
                state.spent_usd += max(float(usd), 0.0)
                self._write(state)
                self._degraded = None
                return state.spent_usd
        except _LedgerCorrupt as e:
            print(f"[Argus Budget] cost not recorded, ledger unusable: {e}")
        except (OSError, TimeoutError) as e:
            # Spending could not be recorded: refuse further calls until a
            # write succeeds again (check() re-probes).
            self._degraded = f"ledger not writable: {e}"
            print(f"[Argus Budget] cost not recorded: {e}")
        return None

    def mark_exhausted(self):
        """Record that the provider says the quota is gone for this period."""
        try:
            with self._locked():
                state, _ = self._current()
                state.exhausted = True
                self._write(state)
        except (_LedgerCorrupt, OSError, TimeoutError) as e:
            self._degraded = f"could not record exhaustion: {e}"
            print(f"[Argus Budget] could not record exhaustion: {e}")

    def claim_notification(self) -> bool:
        """True when the caller should try to post the notice now (see claim_notice)."""
        return self.claim_notice() > 0

    def confirm_notification(self):
        """Record that the notice was posted, so it is never tried again this period."""
        try:
            with self._locked():
                state, _ = self._current()
                state.notified = True
                self._write(state)
        except (_LedgerCorrupt, OSError, TimeoutError) as e:
            print(f"[Argus Budget] could not record the notice as sent: {e}")

    def claim_notice(self) -> int:
        """Claim the right to post the 'used up' notice; returns the attempt number, or 0.

        Once the budget is used up exactly one caller at a time gets a claim. A
        claim is not final: the notice counts as sent only after
        confirm_notification(). If posting failed, another caller may claim again
        after NOTICE_RETRY_SECONDS, at most MAX_NOTICE_ATTEMPTS times per period,
        so a failing GitHub never causes an endless retry or a flood.
        """
        try:
            with self._locked():
                try:
                    state, _ = self._current()
                except _LedgerCorrupt:
                    # No readable ledger to record the notice in: once per process.
                    return self._claim_in_memory("corrupt")
                if not self._is_spent(state):
                    return False
                key = state.period_start.isoformat()
                if state.notified or key in self._notified_mem:
                    return 0
                if state.notice_attempts >= MAX_NOTICE_ATTEMPTS:
                    return 0
                now = self._clock()
                if state.notice_claimed_at and \
                        now - state.notice_claimed_at < timedelta(seconds=NOTICE_RETRY_SECONDS):
                    return 0  # another request is posting it, or just failed
                state.notice_attempts += 1
                state.notice_claimed_at = now
                self._write(state)
                return state.notice_attempts
        except (OSError, TimeoutError):
            return self._claim_in_memory("unwritable")

    def _claim_in_memory(self, key):
        """Fallback when the ledger cannot hold the notice state: once per process."""
        if key in self._notified_mem:
            return 0
        self._notified_mem.add(key)
        return 1

    def snapshot(self):
        """(spent_usd or None, period_start or None) for messages; never raises."""
        try:
            with self._locked():
                state, _ = self._current()
                return state.spent_usd, state.period_start
        except Exception:
            return None, None

    def record_response(self, model, response):
        """Price a finished call from its ``usage`` and add it to the ledger."""
        cost = compute_cost(_field(response, "usage"), find_price(model, self.config))
        if cost is None:
            cost = self.config.missing_usage_usd
            print(f"[Argus Budget] no usage returned for {model!r}; charging ${cost:.2f}")
        return self.record_cost(cost)

    def record_timeout(self, model, prompt_tokens):
        """Charge a call that timed out on the client side.

        The provider may still have run and billed it, and its usage is never
        returned. Charge the prompt at the input price plus TIMEOUT_OUTPUT_TOKENS
        at the output price. A rough estimate: a long high-effort call can
        produce more output than that, so it may still be low.
        """
        price = find_price(model, self.config)
        cost = (_count(prompt_tokens) * price.input
                + TIMEOUT_OUTPUT_TOKENS * price.output) / 1_000_000
        print(f"[Argus Budget] call to {model!r} timed out; charging an estimated ${cost:.2f}")
        return self.record_cost(cost)


# ── LiteLLM wrapper ───────────────────────────────────────────────

# Model outcomes of the command being handled: {"error": last exception or None,
# "succeeded": number of calls that returned, "timed_out": a call timed out}.
# A dict, so a call made in a child task (which copies the context) still
# reports to the command that started it.
# A command counts as failed only when no call succeeded, so a later side call
# (for example the reply judge) failing after the review was posted is not
# reported as a failed review.
_run_state = contextvars.ContextVar("argus_model_run_state", default=None)


def _note_outcome(error):
    state = _run_state.get()
    if state is None:
        return
    if error is None:
        state["succeeded"] += 1
    else:
        state["error"] = error


def is_timeout_error(exc) -> bool:
    """True for LiteLLM/OpenAI/httpx timeouts and the built-in TimeoutError."""
    if isinstance(exc, (TimeoutError, ModelTimeout)):
        return True
    names = {"Timeout", "APITimeoutError", "ReadTimeout", "TimeoutException"}
    return any(cls.__name__ in names for cls in type(exc).__mro__)


def estimate_prompt_tokens(kwargs) -> int:
    """Tokens in the request messages; one per character if LiteLLM cannot count
    (about right for Chinese text, an overestimate for English)."""
    messages = kwargs.get("messages") or []
    try:
        import litellm
        return int(litellm.token_counter(model=kwargs.get("model"), messages=messages))
    except Exception:
        total = 0
        for m in messages:
            content = m.get("content", "") if isinstance(m, dict) else m
            total += len(str(content))
        return total


def make_guarded_acompletion(original, ledger_getter):
    """Wrap an ``acompletion`` coroutine function with the budget rules."""

    @wraps(original)
    async def guarded(*args, **kwargs):
        ledger = ledger_getter()
        ledger.check()
        state = _run_state.get()
        if state is not None and state["timed_out"]:
            raise ModelTimeout("an earlier call in this command timed out; not calling again")
        model = kwargs.get("model", args[0] if args else None)
        try:
            response = await original(*args, **kwargs)
        except BudgetExhausted:
            raise
        except Exception as exc:
            _note_outcome(exc)
            if is_quota_error(exc):
                ledger.mark_exhausted()
                print(f"[Argus Budget] provider reported the quota used up: {str(exc)[:200]}")
            elif is_timeout_error(exc):
                if state is not None:
                    state["timed_out"] = True
                ledger.record_timeout(model, estimate_prompt_tokens(kwargs))
                raise ModelTimeout(f"model call timed out: {str(exc)[:200]}") from exc
            raise
        _note_outcome(None)
        ledger.record_response(model, response)
        return response

    setattr(guarded, _PATCH_MARKER, True)
    setattr(guarded, "_argus_original_acompletion", original)
    return guarded


# ── Notification ──────────────────────────────────────────────────

_PR_URL_RE = re.compile(r"(?:repos/)?([^/]+)/([^/]+)/pulls?/(\d+)")


def parse_pr_url(pr_url):
    """(owner, repo, number) from a PR html or API URL, or None."""
    m = _PR_URL_RE.search(str(pr_url or ""))
    return (m.group(1), m.group(2), int(m.group(3))) if m else None


def exhausted_comment(spent_usd, config: BudgetConfig, next_reset, reason=""):
    spent = "未知" if spent_usd is None else f"${spent_usd:.2f}"
    return (
        "⚠️ **Argus 本期审查额度已用完**\n\n"
        f"本期已花费 {spent}，上限 ${config.monthly_usd:.2f}。"
        "本期内 Argus 不再审查此 PR 或其他 PR，也不会再发这条提示。\n\n"
        f"下次重置：{next_reset.strftime('%Y-%m-%d %H:%M')}（{config.timezone}）。"
        + (f"\n\n原因：{reason}" if reason else "")
    )


def _post_issue_comment(owner, repo, number, body):
    import requests
    try:
        from patch_suggestion_format import _get_app_installation_token
        token = _get_app_installation_token()
    except Exception:
        token = None
    if not token:
        raise RuntimeError("no GitHub token to post the budget notice")
    r = requests.post(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments",
        json={"body": body},
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json"},
        timeout=15)
    if r.status_code != 201:
        raise RuntimeError(f"GitHub returned {r.status_code}")


_poster = _post_issue_comment


def set_poster(fn):
    """Replace the comment poster (tests)."""
    global _poster
    _poster = fn if fn is not None else _post_issue_comment


def notify_once(ledger: BudgetLedger, pr_url, reason=""):
    """Post the 'used up' comment on this PR and log an event.

    The notice counts as sent only after the comment was posted; a failed post
    may be tried again by a later request, a limited number of times per period
    (MAX_NOTICE_ATTEMPTS, spaced NOTICE_RETRY_SECONDS apart). The event is
    written on the first attempt only. Returns True when this call posted it.
    """
    attempt = ledger.claim_notice()
    if not attempt:
        return False
    spent, start = ledger.snapshot()
    next_reset = ledger.next_reset()
    parsed = parse_pr_url(pr_url)
    owner_repo = f"{parsed[0]}/{parsed[1]}" if parsed else None
    number = parsed[2] if parsed else None
    if attempt == 1:
        emitter.emit(EventType.BUDGET_EXHAUSTED, pr_number=number, repo=owner_repo,
                     spent_usd=spent, monthly_usd=ledger.config.monthly_usd,
                     period_start=start.isoformat() if start else None,
                     next_reset=next_reset.isoformat(), reason=reason)
        print(f"[Argus Budget] budget used up ({reason or 'cap reached'}); "
              f"next reset {next_reset.isoformat()}")
    if not parsed:
        ledger.confirm_notification()  # no PR to comment on: nothing more to try
        return True
    try:
        _poster(parsed[0], parsed[1], parsed[2],
                exhausted_comment(spent, ledger.config, next_reset, reason))
    except Exception as e:
        print(f"[Argus Budget] could not post the budget notice (attempt {attempt}): {e}")
        emitter.emit(EventType.ERROR, pr_number=number, repo=owner_repo,
                     message=f"budget notice not posted (attempt {attempt}): {e}")
        return False
    ledger.confirm_notification()
    return True


# ── Model failure note (Argus #52) ────────────────────────────────

_BOT_LOGIN = "argus-review[bot]"


def _ai_timeout():
    try:
        from pr_agent.config_loader import get_settings
        return get_settings().config.ai_timeout
    except Exception:
        return None


def failure_comment(command, error):
    if is_timeout_error(error):
        limit = _ai_timeout()
        reason = f"模型调用超时（超过 {limit} 秒）" if limit else "模型调用超时"
    else:
        reason = f"模型调用失败（{type(error).__name__}）"
    command = command or "review"
    canonical = _ALIASES.get(command, command)
    if canonical in _PLACEHOLDERS:      # the verbs an @mention can start
        hint = f"需要时评论 `@argus-review {canonical}` 重新运行。"
    else:
        hint = f"需要时重新发送 `/{command}`。"
    return (
        f"⚠️ **Argus 未完成 `/{command}`**\n\n"
        f"原因：{reason}。Argus 没有重试。\n\n"
        + hint
    )


# The placeholder PR-Agent 0.38.0 posts for each command (pr_agent/tools/*.py).
_PLACEHOLDERS = {
    "review": "Preparing review...",
    "describe": "Preparing PR description...",
    "improve": "Preparing suggestions...",
}

# PR-Agent 0.38.0 command aliases that run the same tool (pr_agent/agent/pr_agent.py).
_ALIASES = {
    "review_pr": "review",
    "auto_review": "review",
    "describe_pr": "describe",
    "improve_code": "improve",
}


def _remove_temporary_comments(owner, repo, number, command="review"):
    """Delete Argus's own placeholder for ``command`` left by a failed run.

    Only the exact placeholder of that command is removed, so a placeholder of
    another command still running on the same PR stays.
    """
    placeholder = _PLACEHOLDERS.get(_ALIASES.get(command, command))
    if not placeholder:
        return 0
    import requests
    from patch_suggestion_format import _get_app_installation_token
    token = _get_app_installation_token()
    if not token:
        raise RuntimeError("no GitHub token to remove the placeholder")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    since = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    base = f"https://api.github.com/repos/{owner}/{repo}/issues"
    r = requests.get(f"{base}/{number}/comments", params={"since": since, "per_page": 100},
                     headers=headers, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"GitHub returned {r.status_code}")
    removed = 0
    for c in r.json():
        user = c.get("user") or {}
        body = (c.get("body") or "").strip()
        if (user.get("type") == "Bot" and user.get("login") == _BOT_LOGIN
                and body == placeholder):
            d = requests.delete(f"{base}/comments/{c['id']}", headers=headers, timeout=15)
            removed += d.status_code == 204
    return removed


_cleaner = _remove_temporary_comments


def set_cleaner(fn):
    """Replace the placeholder remover (tests)."""
    global _cleaner
    _cleaner = fn if fn is not None else _remove_temporary_comments


def notify_model_failure(pr_url, request, error):
    """Remove the placeholder and post one failure note; never retried, never raises."""
    parsed = parse_pr_url(pr_url)
    command = _command_name(request)
    owner_repo = f"{parsed[0]}/{parsed[1]}" if parsed else None
    number = parsed[2] if parsed else None
    print(f"[Argus] /{command} on {pr_url} gave up: {type(error).__name__}: {str(error)[:200]}")
    emitter.emit(EventType.ERROR, pr_number=number, repo=owner_repo,
                 message=f"/{command} gave up after a model error: {type(error).__name__}")
    if not parsed:
        return False
    try:
        _cleaner(parsed[0], parsed[1], parsed[2], command)
    except Exception as e:
        print(f"[Argus] could not remove the placeholder comment: {e}")
    try:
        _poster(parsed[0], parsed[1], parsed[2], failure_comment(command, error))
    except Exception as e:
        print(f"[Argus] could not post the failure note: {e}")
        return False
    return True


# ── Patching ──────────────────────────────────────────────────────

_ledger = None
_ledger_lock = threading.Lock()


def get_ledger() -> BudgetLedger:
    global _ledger
    with _ledger_lock:
        if _ledger is None:
            _ledger = BudgetLedger(config=load_config())
        return _ledger


def _command_name(request):
    try:
        first = request.split()[0] if isinstance(request, str) else request[0]
        return str(first).lstrip("/").lower()
    except (IndexError, TypeError, AttributeError):
        return ""


def _load_runtime():
    """Load PR-Agent lazily so this module is testable without it."""
    from pr_agent.algo.ai_handlers import litellm_ai_handler
    from pr_agent.agent.pr_agent import PRAgent
    return litellm_ai_handler, PRAgent


def apply_patch(ledger_getter=None):
    """Wrap LiteLLM completion and PR-Agent's request handler, once per process."""
    ledger_getter = ledger_getter or get_ledger
    handler, agent_cls = _load_runtime()
    if getattr(handler.acompletion, _PATCH_MARKER, False) is True:
        return False

    handler.acompletion = make_guarded_acompletion(handler.acompletion, ledger_getter)

    original_handle = agent_cls._handle_request

    @wraps(original_handle)
    async def guarded_handle(self, pr_url, request, notify=None):
        ledger = ledger_getter()
        if _command_name(request) in _FREE_COMMANDS:
            return await original_handle(self, pr_url, request, notify)
        try:
            ledger.check()
        except BudgetExhausted as e:
            notify_once(ledger, pr_url, e.reason)
            print(f"[Argus Budget] skipped {request!r} on {pr_url}: {e.reason}")
            return False
        state = {"error": None, "succeeded": 0, "timed_out": False}
        token = _run_state.set(state)
        try:
            result = await original_handle(self, pr_url, request, notify)
        finally:
            _run_state.reset(token)
        # PR-Agent swallows errors inside a command, so a quota error or a model
        # failure during this run is only visible afterwards.
        if ledger.is_exhausted():
            notify_once(ledger, pr_url, "budget used up during this run")
        elif state["error"] is not None and state["succeeded"] == 0:
            notify_model_failure(pr_url, request, state["error"])
        return result

    setattr(guarded_handle, _PATCH_MARKER, True)
    agent_cls._handle_request = guarded_handle
    print("[Argus] budget guard applied")
    return True
