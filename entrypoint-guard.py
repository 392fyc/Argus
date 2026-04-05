#!/usr/bin/env python3
"""
Argus Guard — User whitelist ASGI middleware for PR-Agent GitHub App.

Sits in front of the PR-Agent webhook server. Inspects GitHub webhook
payloads and blocks events from users not on the whitelist.

Configuration:
    ARGUS_ALLOWED_USERS=392fyc,trusted-bot   (comma-separated, case-insensitive)

If ARGUS_ALLOWED_USERS is empty or unset, ALL users are allowed.
"""

import json
import os

_raw = os.environ.get("ARGUS_ALLOWED_USERS", "").strip()
ALLOWED_USERS: set[str] = set()
if _raw:
    ALLOWED_USERS = {u.strip().lower() for u in _raw.split(",") if u.strip()}


def get_actor(payload: dict) -> str | None:
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

        # Buffer the request body to inspect it
        body_parts = []
        original_body_sent = False

        async def receive_wrapper():
            nonlocal original_body_sent
            message = await receive()
            if message["type"] == "http.request":
                body_parts.append(message.get("body", b""))
                if not message.get("more_body", False):
                    original_body_sent = True
            return message

        # We need to read the body first, then decide
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break

        # Parse and check
        try:
            payload = json.loads(body)
            actor = get_actor(payload)
        except (json.JSONDecodeError, KeyError):
            actor = None

        if actor and actor.lower() not in ALLOWED_USERS:
            print(f"[Argus Guard] BLOCKED: '{actor}' not in whitelist")
            response_body = json.dumps({
                "status": "skipped",
                "reason": f"user '{actor}' not in whitelist"
            }).encode()
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"content-length", str(len(response_body)).encode()],
                ],
            })
            await send({
                "type": "http.response.body",
                "body": response_body,
            })
            return

        if actor:
            print(f"[Argus Guard] ALLOWED: '{actor}'")

        # Replay the buffered body to the original app
        body_sent = False

        async def replay_receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            # After body is sent, just pass through
            return await receive()

        return await self.app(scope, replay_receive, send)


def apply_guard():
    """Import the PR-Agent app and wrap it with the guard middleware."""
    from pr_agent.servers.github_app import app
    app.add_middleware(ArgusGuardMiddleware)
    return app
