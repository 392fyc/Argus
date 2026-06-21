"""
Argus Guard — User whitelist + @mention support for PR-Agent GitHub App webhook.

Features:
- User whitelist: only allowed users' events pass through
- @mention rewrite: converts @argus-review[bot] mentions to PR-Agent slash commands

    ARGUS_ALLOWED_USERS=392fyc,trusted-bot   (comma-separated, case-insensitive)

If empty or unset, ALL users are allowed.
"""

import json
import os

from argus_events import emitter, EventType
from mention_rewrite import should_rewrite_mention, rewrite_mention

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
            emitter.emit(EventType.REQUEST_BLOCKED, actor=actor)
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


# @mention → slash-command rewriting lives in mention_rewrite.py
# (pure + unit-tested in tests/test_mention_rewrite.py). The intent classifier
# keys off the verb following a mention, with the LAST explicit-command mention
# winning so a trailing "@argus-review review" re-triggers review even when a
# fix-summary body precedes it (Argus #23).


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

        # Defense-in-depth: skip replies that contain judgment tags
        JUDGMENT_TAGS = ("✅ Acknowledged", "❓ Follow-up", "⚠️ Escalated")
        if any(tag in reply_body for tag in JUDGMENT_TAGS):
            return

        owner, name = repo_full.split("/", 1)

        from patch_suggestion_format import (
            _get_github_token, _judge_reply_with_llm,
            _reply_to_thread, _resolve_thread, _is_bot_author,
            MAX_REPLY_ROUNDS,
        )
        import requests as _req

        # Get App installation token so replies come from argus-review[bot]
        from patch_suggestion_format import _get_app_installation_token
        token = _get_app_installation_token()
        if not token:
            print("[Argus] No app installation token — cannot reply as bot")
            emitter.emit(EventType.ERROR, pr_number=pr_number, repo=repo_full,
                         message="no app installation token")
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
                emitter.emit(EventType.REPLY_CLASSIFIED, pr_number=pr_number, repo=repo_full,
                             verdict="max_rounds_reached", thread_path=t.get("path"))
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

            # Never post replies for LLM errors — log and return silently
            if verdict == "ESCALATE" and "LLM error" in reason:
                print(f"[Argus] Reply judgment LLM failed for {thread_path}: {reason}")
                emitter.emit(EventType.ERROR, pr_number=pr_number, repo=repo_full,
                             message=f"LLM failed: {reason}", thread_path=thread_path)
                return

            if verdict == "ACCEPT":
                _reply_to_thread(auth_h, repo_full, pr_number, first_db_id,
                                 f"✅ Acknowledged — {reason}")
                resolved = _resolve_thread(auth_h, t["id"])
                print(f"[Argus] Reply accepted: {thread_path} → "
                      f"{'resolved' if resolved else 'resolve failed'}")
                emitter.emit(EventType.REPLY_CLASSIFIED, pr_number=pr_number, repo=repo_full,
                             verdict="ACCEPT", thread_path=thread_path, reason=reason)
                if resolved:
                    emitter.emit(EventType.THREAD_RESOLVED, pr_number=pr_number, repo=repo_full,
                                 thread_path=thread_path)
                else:
                    emitter.emit(EventType.ERROR, pr_number=pr_number, repo=repo_full,
                                 message=f"_resolve_thread failed for {thread_path}",
                                 thread_path=thread_path)
            elif verdict == "REJECT":
                _reply_to_thread(auth_h, repo_full, pr_number, first_db_id,
                                 f"❓ Follow-up — {reason}")
                print(f"[Argus] Reply rejected: {thread_path} → follow-up")
                emitter.emit(EventType.REPLY_CLASSIFIED, pr_number=pr_number, repo=repo_full,
                             verdict="REJECT", thread_path=thread_path, reason=reason)
            else:  # ESCALATE (genuine, not LLM error)
                _reply_to_thread(auth_h, repo_full, pr_number, first_db_id,
                                 f"⚠️ Escalated — {reason}\n\n"
                                 f"*This thread requires human reviewer input.*")
                print(f"[Argus] Reply escalated: {thread_path}")
                emitter.emit(EventType.REPLY_CLASSIFIED, pr_number=pr_number, repo=repo_full,
                             verdict="ESCALATE", thread_path=thread_path, reason=reason)
            return  # Only handle one thread per comment

    except Exception as e:
        print(f"[Argus] Reply handler error: {e}")
        emitter.emit(EventType.ERROR, message=f"reply handler: {e}")


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
                if should_rewrite_mention(comment_body):
                    rewritten, method = rewrite_mention(comment_body)
                    if rewritten:
                        print(f"[Argus] @mention rewritten ({method}): "
                              f"'{comment_body[:60]}' → '{rewritten[:60]}'")
                        body["comment"]["body"] = rewritten
                        _pr_num = body.get("pull_request", {}).get("number")
                        _repo = body.get("repository", {}).get("full_name")
                        emitter.emit(EventType.MENTION_REWRITTEN, pr_number=_pr_num,
                                     repo=_repo, original=comment_body[:60],
                                     rewritten=rewritten[:60], method=method)

            return await original_handle(body, event, sender, sender_id,
                                         action, log_context, agent)

        ga_mod.handle_comments_on_pr = patched_handle
        print("[Argus] @mention rewrite patched")
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
