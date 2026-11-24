"""Runtime settings a trigger comment may change for one command (Argus #63).

PR-Agent 0.38.0 turns every ``--section.key=value`` argument of a command into
a setting for that request (``PRAgent._handle_request`` calls
``update_settings_from_args``); its own deny list only covers keys, URLs and a
few switches. So ``@argus-review review --config.model=...`` or
``--argus_budget.monthly_usd=...`` would change the model or the spend cap for
that run.

This module wraps ``PRAgent._handle_request`` and lets only two such arguments
through, with limits read from ``[argus_review]`` in configuration.toml:

* ``--config.ai_timeout=<seconds>``: a whole number from MIN_AI_TIMEOUT to
  ``max_ai_timeout`` (default 900);
* ``--config.reasoning_effort=<value>``: one of ``reasoning_efforts`` (default
  low, medium, high).

Any other ``--x=y`` argument stops the command before PR-Agent applies a
setting or calls the model, and the PR gets one short note saying why.
Arguments without ``=`` (such as ``-i``) pass unchanged, as do the arguments of
the automatic commands, which PR-Agent applies itself before this point, and
the location keys PR-Agent adds to a question asked on a code line (``/ask_line``).

The defaults for every review stay in configuration.toml, or in the deployment
host's ``.env`` as ``CONFIG__AI_TIMEOUT=600`` and the like (PR-Agent reads
environment variables over the TOML files; see README "Runtime settings").
"""

from __future__ import annotations

import asyncio
import shlex
from functools import wraps

from argus_events import EventType, emitter

_PATCH_MARKER = "_argus_runtime_patch"
MIN_AI_TIMEOUT = 60
DEFAULT_MAX_AI_TIMEOUT = 900
DEFAULT_REASONING_EFFORTS = ("low", "medium", "high")
_TIMEOUT_KEY = "config.ai_timeout"
_EFFORT_KEY = "config.reasoning_effort"
# A question on a code line reaches PRAgent as a request PR-Agent builds itself
# (pr_agent/servers/github_app.py handle_line_comments, 0.38.0):
#   /ask_line --line_start=.. --line_end=.. --side=.. --file_name=.. --comment_id=.. <question>
# These top-level keys only tell PR_LineQuestions where the question was asked.
_LINE_QUESTION_KEYS = frozenset({"line_start", "line_end", "side", "file_name", "comment_id"})


def _get_settings():
    from pr_agent.config_loader import get_settings
    return get_settings()


def _review_table(settings):
    try:
        table = settings.get("argus_review")
    except Exception:
        return {}
    return table if hasattr(table, "get") else {}


def limits(settings=None):
    """(max_ai_timeout, allowed reasoning efforts) from ``[argus_review]``.

    A missing or invalid value falls back to the default, so a typo in the
    configuration never opens the gate wider than the defaults.
    """
    if settings is None:
        try:
            settings = _get_settings()
        except Exception:
            settings = None
    table = _review_table(settings) if settings is not None else {}
    cap = table.get("max_ai_timeout", DEFAULT_MAX_AI_TIMEOUT)
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < MIN_AI_TIMEOUT:
        cap = DEFAULT_MAX_AI_TIMEOUT
    efforts = table.get("reasoning_efforts", DEFAULT_REASONING_EFFORTS)
    if (isinstance(efforts, str) or not hasattr(efforts, "__iter__")
            or not all(isinstance(e, str) and e for e in efforts) or not list(efforts)):
        efforts = DEFAULT_REASONING_EFFORTS
    return cap, tuple(efforts)


def request_args(request):
    """The arguments of a command, split exactly as PRAgent._handle_request does."""
    if isinstance(request, str):
        lexer = shlex.shlex(request.replace("'", "\\'"), posix=True)
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError:
            return []   # PR-Agent fails on the same text before applying any setting
        return tokens[1:]
    try:
        return list(request)[1:]
    except TypeError:
        return []


def _parse_value(value):
    """The value as PR-Agent's _fix_key_value reads it (YAML scalar)."""
    try:
        import yaml
        return yaml.safe_load(value)
    except Exception:
        return value


def check_args(args, cap=DEFAULT_MAX_AI_TIMEOUT, efforts=DEFAULT_REASONING_EFFORTS, command=""):
    """None when every setting argument is allowed, else the reason for the first refused one."""
    line_keys = _LINE_QUESTION_KEYS if command == "ask_line" else frozenset()
    for raw in args:
        arg = str(raw).strip()
        if not arg.startswith("--"):
            continue
        body = arg.strip("-").strip()
        if "=" not in body:
            continue
        key, value = body.split("=", 1)
        key = key.strip().lower().replace("__", ".")
        shown = key[:60].replace("`", "")
        if key in line_keys:
            continue
        value = _parse_value(value.strip())
        if key == _TIMEOUT_KEY:
            if isinstance(value, bool) or not isinstance(value, int) or not MIN_AI_TIMEOUT <= value <= cap:
                return f"`--{_TIMEOUT_KEY}` 需要 {MIN_AI_TIMEOUT} 到 {cap} 之间的整数秒数"
        elif key == _EFFORT_KEY:
            if value not in efforts:
                return f"`--{_EFFORT_KEY}` 只能是 {'、'.join(efforts)} 之一"
        else:
            return f"参数 `--{shown}` 不能在评论里修改"
    return None


def check_request(request, settings=None):
    cap, efforts = limits(settings)
    return check_args(request_args(request), cap, efforts, _command_name(request))


def rejection_comment(command, reason, settings=None):
    cap, efforts = limits(settings)
    command = command or "review"
    return (
        f"⚠️ **Argus 未运行 `/{command}`**\n\n"
        f"原因：{reason}。没有调用模型，也没有计费。\n\n"
        f"评论里只能调整这一次运行的 `--{_TIMEOUT_KEY}=<秒>`（{MIN_AI_TIMEOUT} 到 {cap}）"
        f"和 `--{_EFFORT_KEY}=<{'|'.join(efforts)}>`；其他运行参数在 Argus 的部署配置里修改。"
    )


def _command_name(request):
    try:
        first = request.split()[0] if isinstance(request, str) else request[0]
        return str(first).lstrip("/").lower()
    except (IndexError, TypeError, AttributeError):
        return ""


def notify_rejected(pr_url, request, reason):
    """Post the rejection note on the PR; never raises."""
    import argus_budget
    command = _command_name(request)
    print(f"[Argus] /{command} on {pr_url} refused: {reason}")
    parsed = argus_budget.parse_pr_url(pr_url)
    if not parsed:
        return False
    try:
        argus_budget._poster(parsed[0], parsed[1], parsed[2], rejection_comment(command, reason))
    except Exception as e:
        print(f"[Argus] could not post the refusal note: {e}")
        return False
    return True


def _notify_review_start_failure(pr_url, sender_id, is_eligible):
    import argus_budget

    parsed = argus_budget.parse_pr_url(pr_url)
    if not parsed:
        return False
    owner, repo, number = parsed
    emitter.emit(EventType.ERROR, pr_number=number, repo=f"{owner}/{repo}",
                 message="/review could not connect to GitHub while opening the PR")
    try:
        if not is_eligible(sender_id, pr_url):
            return False
        argus_budget._poster(owner, repo, number,
                            "⚠️ **Argus 未发布本轮审查**\n\n"
                            "读取 GitHub PR 数据时连接中断，本轮没有发布审查。"
                            "已有线程的回复仅对应各自的发现。")
    except Exception as error:
        print(f"[Argus] could not post review start failure: {type(error).__name__}")
        return False
    return True


def wrap_review_comment_handler(original, is_eligible):
    """Report the GitHub startup failure that can end a manual review before dispatch."""
    from requests.exceptions import ConnectionError

    @wraps(original)
    async def checked_comment(body, event, sender, sender_id, action, log_context, agent):
        try:
            return await original(body, event, sender, sender_id, action, log_context, agent)
        except ValueError as error:
            comment = body.get("comment", {})
            request = comment.get("body", "") if isinstance(comment, dict) else ""
            pr_url = log_context.get("api_url")
            if (action == "created" and isinstance(request, str) and request.lstrip().startswith("/")
                    and _command_name(request) in ("review", "review_pr", "auto_review") and pr_url
                    and isinstance(error.__cause__, ConnectionError)
                    and str(error) == f"Failed to get git provider for {pr_url}"):
                if await asyncio.to_thread(_notify_review_start_failure, pr_url, sender_id, is_eligible):
                    return {}
            raise

    return checked_comment


def effective_settings(settings=None):
    """The model settings this process will use, for the startup log."""
    if settings is None:
        settings = _get_settings()
    config = settings.config
    cap, efforts = limits(settings)
    return {
        "model": config.get("model"),
        "ai_timeout": config.get("ai_timeout"),
        "reasoning_effort": config.get("reasoning_effort"),
        "max_model_tokens": config.get("max_model_tokens"),
        "comment max_ai_timeout": cap,
        "comment reasoning_efforts": list(efforts),
    }


def _load_runtime():
    from pr_agent.agent.pr_agent import PRAgent
    return PRAgent


def apply_patch():
    """Wrap PRAgent._handle_request with the argument check, once per process.

    Apply it after the budget patch, so it runs first and a refused command
    neither checks the budget nor posts a budget notice.
    """
    agent_cls = _load_runtime()
    original = agent_cls._handle_request
    if getattr(original, _PATCH_MARKER, False) is True:
        return False

    @wraps(original)
    async def checked_handle(self, pr_url, request, notify=None):
        reason = check_request(request)
        if reason:
            await asyncio.to_thread(notify_rejected, pr_url, request, reason)
            return False
        return await original(self, pr_url, request, notify)

    setattr(checked_handle, _PATCH_MARKER, True)
    agent_cls._handle_request = checked_handle
    try:
        print(f"[Argus] runtime settings: {effective_settings()}")
    except Exception as e:
        print(f"[Argus] could not read the runtime settings: {e}")
    print("[Argus] comment-argument check applied")
    return True
