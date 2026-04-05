"""
Argus Guard — User whitelist for PR-Agent GitHub App webhook.

Filters incoming webhook events: only whitelisted users pass through.

    ARGUS_ALLOWED_USERS=392fyc,trusted-bot   (comma-separated, case-insensitive)

If empty or unset, ALL users are allowed.
"""

import json
import os

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

        if actor and actor.lower() not in ALLOWED_USERS:
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


# ── App initialization (imported by gunicorn) ─────────────────────
# Import the original PR-Agent app and wrap it with the guard
from pr_agent.servers.github_app import app as _original_app
_original_app.add_middleware(ArgusGuardMiddleware)
app = _original_app
