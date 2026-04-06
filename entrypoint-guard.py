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


PR_AGENT_COMMANDS = {
    "review", "describe", "improve", "ask", "help",
    "update_changelog", "similar_issue", "add_docs", "test",
}


def _rewrite_mention(body: str) -> str:
    """Rewrite @argus-review mentions to PR-Agent slash commands.

    Supports both styles:
      @argus-review review        → /review
      @argus-review review -i     → /review -i
      @argus-review /review       → /review
      @argus-review why is X bad? → /ask why is X bad?
    """
    cleaned = BOT_MENTION_RE.sub("", body).strip()
    if not cleaned:
        return ""
    # Already a slash command — pass through
    if cleaned.startswith("/"):
        return cleaned
    # Check if first word is a known PR-Agent command
    first_word = cleaned.split()[0].lower()
    if first_word in PR_AGENT_COMMANDS:
        rest = cleaned[len(first_word):].strip()
        return f"/{first_word} {rest}".rstrip()
    # Fallback: treat as /ask
    return f"/ask {cleaned}"


def _handle_reply_to_argus(body, sender):
    """Handle replies to Argus review threads — trigger LLM judgment.

    When a non-bot user replies to an Argus review comment, run the
    reply-aware judging logic (ACCEPT/REJECT/ESCALATE) immediately
    instead of waiting for the next push-triggered review.
    """
    try:
        comment = body.get("comment", {})
        # Only process replies (in_reply_to_id is set)
        in_reply_to = comment.get("in_reply_to_id")
        if not in_reply_to:
            return

        # Skip bot's own replies
        if sender and "argus-review" in sender.lower():
            return

        pr_number = body.get("pull_request", {}).get("number")
        repo_full = body.get("repository", {}).get("full_name", "")
        if not pr_number or not repo_full:
            return

        reply_body = comment.get("body", "")
        if not reply_body:
            return

        owner, name = repo_full.split("/", 1)

        from patch_suggestion_format import (
            _get_github_token, _judge_reply_with_llm,
            _reply_to_thread, _resolve_thread, _is_bot_author,
            MAX_REPLY_ROUNDS,
        )
        import requests as _req

        # Get token from settings (no provider object available here)
        from pr_agent.config_loader import get_settings
        token = get_settings().get("github.user_token", "")
        if not token:
            return

        auth_h = {"Authorization": f"Bearer {token}",
                  "Accept": "application/vnd.github+json"}
        bot_login = "argus-review[bot]"

        # Find the thread containing this reply via GraphQL
        query = """{
          repository(owner: "%s", name: "%s") {
            pullRequest(number: %d) {
              reviewThreads(first: 100) {
                nodes {
                  id
                  isResolved
                  path
                  comments(first: 20) {
                    totalCount
                    nodes {
                      databaseId
                      author { login }
                      body
                    }
                  }
                }
              }
            }
          }
        }""" % (owner, name, pr_number)

        g = _req.post("https://api.github.com/graphql",
                      json={"query": query}, headers=auth_h, timeout=15)
        if g.status_code != 200 or "data" not in g.json():
            return

        threads = g.json()["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]

        # Find thread where in_reply_to_id matches an Argus comment
        for t in threads:
            if t["isResolved"]:
                continue
            comments_data = t["comments"]
            if comments_data.get("totalCount", 0) > len(comments_data["nodes"]):
                continue

            # Check if any Argus comment in this thread has the replied-to databaseId
            argus_comment_ids = [
                c["databaseId"] for c in comments_data["nodes"]
                if c.get("author") and _is_bot_author(c["author"]["login"], bot_login)
            ]
            if in_reply_to not in argus_comment_ids:
                continue

            # Found the thread — check judgment round limit
            argus_judgment_count = sum(
                1 for c in comments_data["nodes"]
                if c.get("author") and _is_bot_author(c["author"]["login"], bot_login)
                and any(tag in c.get("body", "")
                        for tag in ("✅ Acknowledged", "❓ Follow-up", "⚠️ Escalated")))

            if argus_judgment_count >= MAX_REPLY_ROUNDS:
                print(f"[Argus] Reply judgment: max rounds reached for {t.get('path')}")
                return

            # Get original finding (first Argus comment)
            original_finding = ""
            first_db_id = None
            for c in comments_data["nodes"]:
                if c.get("author") and _is_bot_author(c["author"]["login"], bot_login):
                    original_finding = c.get("body", "")
                    first_db_id = c.get("databaseId")
                    break

            if not original_finding or not first_db_id:
                return

            verdict, reason = _judge_reply_with_llm(original_finding, reply_body)
            thread_path = t.get("path", "?")

            if verdict == "ACCEPT":
                _reply_to_thread(auth_h, repo_full, pr_number, first_db_id,
                                 f"✅ Acknowledged — {reason}")
                _resolve_thread(auth_h, t["id"])
                print(f"[Argus] Reply accepted: {thread_path} → resolved")
            elif verdict == "REJECT":
                _reply_to_thread(auth_h, repo_full, pr_number, first_db_id,
                                 f"❓ Follow-up — {reason}")
                print(f"[Argus] Reply rejected: {thread_path} → follow-up")
            else:
                _reply_to_thread(auth_h, repo_full, pr_number, first_db_id,
                                 f"⚠️ Escalated — {reason}\n\n"
                                 f"*This thread requires human reviewer input.*")
                print(f"[Argus] Reply escalated: {thread_path}")
            return  # Only handle one thread per comment

    except Exception as e:
        print(f"[Argus] Reply handler error: {e}")


def _patch_mention_handler():
    """Patch PR-Agent's comment handler to support @mentions + reply judging.

    Intercepts handle_comments_on_pr to:
    1. Judge replies to Argus review threads via LLM
    2. Rewrite @mentions before the slash-command filter drops them

    handle_comments_on_pr signature (PR-Agent 0.34):
      async def handle_comments_on_pr(body, event, sender, sender_id,
                                       action, log_context, agent)
    """
    try:
        from pr_agent.servers import github_app as ga_mod

        original_handle = ga_mod.handle_comments_on_pr

        async def patched_handle(body, event, sender, sender_id,
                                 action, log_context, agent):
            if action == "created" and "comment" in body and isinstance(body["comment"], dict):
                comment_body = body["comment"].get("body", "")
                # Skip self-comments (prevent loops)
                if sender and "argus-review" in sender.lower():
                    return {}

                # Handle replies to Argus threads (LLM judgment)
                if body["comment"].get("in_reply_to_id"):
                    _handle_reply_to_argus(body, sender)

                # Rewrite @mentions to PR-Agent commands
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
