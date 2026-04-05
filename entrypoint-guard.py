"""
Argus Guard — User whitelist + @mention support for PR-Agent GitHub App webhook.

Features:
- User whitelist: only allowed users' events pass through
- @mention rewrite: converts @argus-review[bot] mentions to /ask commands

    ARGUS_ALLOWED_USERS=392fyc,trusted-bot   (comma-separated, case-insensitive)

If empty or unset, ALL users are allowed.
"""

import json
import os
import re

_raw = os.environ.get("ARGUS_ALLOWED_USERS", "").strip()
ALLOWED_USERS: set = set()
if _raw:
    ALLOWED_USERS = {u.strip().lower() for u in _raw.split(",") if u.strip()}


def get_actor(payload: dict):
    """Extract the acting user from a GitHub webhook payload."""
    if "comment" in payload and isinstance(payload["comment"], dict):
        user = payload["comment"].get("user", {})
        if isinstance(user, dict) and "login" in user:
            return user["login"]
    if "sender" in payload and isinstance(payload["sender"], dict):
        return payload["sender"].get("login")
    return None


class ArgusGuardMiddleware:
    """ASGI middleware that filters webhook requests by user whitelist."""

    def __init__(self, app):
        self.app = app
        if ALLOWED_USERS:
            print(f"[Argus Guard] Whitelist ACTIVE: {sorted(ALLOWED_USERS)}")
        else:
            print("[Argus Guard] Whitelist DISABLED — all users allowed")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not ALLOWED_USERS:
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if "github_webhooks" not in path:
            return await self.app(scope, receive, send)

        # Buffer the full request body
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break

        # Parse and check user
        try:
            payload = json.loads(body)
            actor = get_actor(payload)
        except (json.JSONDecodeError, KeyError):
            actor = None

        # Always allow our own bot (self-triggered events)
        is_self_bot = actor and actor.lower() == "argus-review[bot]"
        if not is_self_bot and actor and actor.lower() not in ALLOWED_USERS:
            print(f"[Argus Guard] BLOCKED: '{actor}' not in whitelist")
            resp = json.dumps({"status": "skipped", "reason": f"user '{actor}' not in whitelist"}).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [[b"content-type", b"application/json"],
                                    [b"content-length", str(len(resp)).encode()]]})
            await send({"type": "http.response.body", "body": resp})
            return

        if actor:
            print(f"[Argus Guard] ALLOWED: '{actor}'")

        # Replay buffered body to the original app
        body_sent = False
        async def replay_receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return await self.app(scope, replay_receive, send)


# ── @mention → /ask rewrite ──────────────────────────────────────
# Rewrites @argus-review[bot] mentions as /ask commands so PR-Agent
# can process them. Must be applied AFTER HMAC verification (at the
# handler level, not the ASGI middleware level).
#
# Pattern: matches @argus-review[bot] or @argus-review (with or without [bot])
# Ref: https://docs.github.com/en/webhooks/webhook-events-and-payloads#issue_comment

BOT_MENTION_RE = re.compile(
    r'@argus-review(?:\[bot\])?\s*', re.IGNORECASE)

# Patterns that indicate the mention is in a quote (not a direct request)
QUOTE_PREFIX_RE = re.compile(r'^\s*>')


def _should_rewrite_mention(body: str) -> bool:
    """Check if comment body contains a direct @mention (not in a quote)."""
    if not BOT_MENTION_RE.search(body):
        return False
    # Don't rewrite if the mention is only in quoted lines
    for line in body.split('\n'):
        if BOT_MENTION_RE.search(line) and not QUOTE_PREFIX_RE.match(line):
            return True
    return False


def _rewrite_mention(body: str) -> str:
    """Rewrite @argus-review mention to /ask command."""
    # Remove the @mention prefix
    cleaned = BOT_MENTION_RE.sub("", body).strip()
    if not cleaned:
        return ""
    # If already a command, just strip the @mention
    if cleaned.startswith("/"):
        return cleaned
    # Rewrite as /ask
    return f"/ask {cleaned}"


def _patch_mention_handler():
    """Patch PR-Agent's comment handler to support @mentions.

    Intercepts handle_comments_on_pr to rewrite @mentions before
    the slash-command filter drops them. This runs AFTER get_body()
    has already verified the HMAC signature.

    handle_comments_on_pr signature (PR-Agent 0.34):
      async def handle_comments_on_pr(body, event, sender, sender_id,
                                       action, log_context, agent)
    """
    try:
        from pr_agent.servers import github_app as ga_mod

        original_handle = ga_mod.handle_comments_on_pr

        async def patched_handle(body, event, sender, sender_id,
                                 action, log_context, agent):
            # Rewrite @mentions to /ask before PR-Agent filters them
            if (action == "created" and "comment" in body
                    and isinstance(body["comment"], dict)):
                comment_body = body["comment"].get("body", "")
                # Skip self-mentions (prevent loops)
                if sender and "argus-review" in sender.lower():
                    return {}

                if _should_rewrite_mention(comment_body):
                    rewritten = _rewrite_mention(comment_body)
                    if rewritten:
                        print(f"[Argus] @mention rewritten: "
                              f"'{comment_body[:60]}' → '{rewritten[:60]}'")
                        body["comment"]["body"] = rewritten

            return await original_handle(body, event, sender, sender_id,
                                         action, log_context, agent)

        ga_mod.handle_comments_on_pr = patched_handle
        print("[Argus] @mention → /ask rewrite patched")
    except Exception as e:
        print(f"[Argus] Failed to patch @mention handler: {e}")


# ── App initialization (imported by gunicorn) ─────────────────────
# Apply CodeRabbit-style suggestion format patch (must be before app import)
from patch_suggestion_format import apply_patch
apply_patch()

# Apply @mention support (patches handle_comments_on_pr before app routes bind)
_patch_mention_handler()

# Import the original PR-Agent app and wrap it with the guard
from pr_agent.servers.github_app import app as _original_app
_original_app.add_middleware(ArgusGuardMiddleware)
app = _original_app
