"""
Argus — Patches PR-Agent output to CodeRabbit-style structured format.

Review body: summary + aggregated 🤖 Prompt for all comments
Inline threads: severity badge, description, suggestion, committable, agent prompt
Thread auto-resolve: resolve outdated threads after push
@mention support: rewrite @argus-review mentions as /ask commands

Reference: CodeRabbit PR review format (2026)
"""

from argus_events import emitter, EventType

# ── Severity mapping ──────────────────────────────────────────────

SEVERITY_MAP = {
    "critical bug": ("🔴", "Critical"),
    "security": ("🔴", "Critical"),
    "possible bug": ("🟠", "Major"),
    "possible issue": ("🟡", "Medium"),
    "performance": ("🟡", "Medium"),
    "general": ("🟡", "Medium"),
    "enhancement": ("🔵", "Minor"),
    "best practice": ("🔵", "Minor"),
    "maintainability": ("🔵", "Minor"),
    "typo": ("⚪", "Trivial"),
}

REVIEW_SEVERITY_MAP = {
    "bug": ("🔴", "Critical"),
    "critical": ("🔴", "Critical"),
    "security": ("🔴", "Critical"),
    "possible": ("🟠", "Major"),
    "error": ("🟠", "Major"),
    "performance": ("🟡", "Medium"),
    "issue": ("🟡", "Medium"),
}


def _detect_lang(filepath: str) -> str:
    ext_map = {
        ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
        ".py": "python", ".sh": "bash", ".yaml": "yaml", ".yml": "yaml",
        ".toml": "toml", ".json": "json", ".md": "markdown",
        ".css": "css", ".html": "html", ".rs": "rust", ".go": "go",
    }
    for ext, lang in ext_map.items():
        if filepath.endswith(ext):
            return lang
    return ""


def _get_review_severity(header: str):
    header_lower = header.lower()
    for key, val in REVIEW_SEVERITY_MAP.items():
        if key in header_lower:
            return val
    return ("🔵", "Minor")


# ── /improve inline thread format ─────────────────────────────────

def format_suggestion_body(d: dict, new_code_snippet: str) -> str:
    """Format a single /improve code suggestion as inline thread."""
    content = d.get("suggestion_content", "").rstrip()
    label = d.get("label", "general").strip().lower()
    score = d.get("score")
    existing_code = d.get("existing_code", "").rstrip()
    relevant_file = d.get("relevant_file", "").strip()
    start_line = d.get("relevant_lines_start", "")
    end_line = d.get("relevant_lines_end", "")

    icon, severity = SEVERITY_MAP.get(label, ("🟡", "Medium"))
    score_text = f" | importance: {score}/10" if score else ""
    lang = _detect_lang(relevant_file)

    parts = []

    # Header
    parts.append(f"_{icon} {severity}_ | _{label}_{score_text}")
    parts.append("")

    # Description
    parts.append(f"**{content}**")
    parts.append("")

    # Suggestion details
    if existing_code:
        parts.append(f"<details><summary>📝 Suggestion</summary>")
        parts.append("")
        parts.append(f"```{lang}")
        parts.append(existing_code)
        parts.append("```")
        parts.append("")
        parts.append("</details>")
        parts.append("")

    # Committable suggestion
    if new_code_snippet:
        parts.append("<details><summary>📝 Committable suggestion</summary>")
        parts.append("")
        parts.append("> Carefully review the code before committing.")
        parts.append("")
        parts.append("```suggestion")
        parts.append(new_code_snippet)
        parts.append("```")
        parts.append("")
        parts.append("</details>")
        parts.append("")

    # Prompt for AI Agents
    agent_prompt = _build_improve_agent_prompt(d, relevant_file, start_line, end_line)
    parts.append("<details><summary>🤖 Prompt for AI Agents</summary>")
    parts.append("")
    parts.append("```text")
    parts.append(agent_prompt)
    parts.append("```")
    parts.append("")
    parts.append("</details>")

    return "\n".join(parts)


def _build_improve_agent_prompt(d, filepath, start, end):
    content = d.get("suggestion_content", "").rstrip()
    label = d.get("label", "").strip()
    improved = d.get("improved_code", "").rstrip()
    existing = d.get("existing_code", "").rstrip()

    lines = [f"In file `{filepath}`"]
    if start and end and str(start) != str(end):
        lines.append(f"around lines {start}-{end}:")
    elif start:
        lines.append(f"around line {start}:")
    lines.append("")
    lines.append(f"[{label}] {content}")
    lines.append("")
    if existing and improved:
        lines.append("Current code:")
        lines.append(f"```")
        lines.append(existing)
        lines.append(f"```")
        lines.append("")
        lines.append("Suggested replacement:")
        lines.append(f"```")
        lines.append(improved)
        lines.append(f"```")
    elif improved:
        lines.append("Suggested fix:")
        lines.append(f"```")
        lines.append(improved)
        lines.append(f"```")
    else:
        lines.append(f"Action required: {content}")
    return "\n".join(lines)


# ── /review inline thread format ──────────────────────────────────

def format_review_finding_body(issue: dict) -> str:
    """Format a /review key_issue as inline thread."""
    header = issue.get("issue_header", "Issue").strip()
    content = issue.get("issue_content", "").strip()
    filepath = issue.get("relevant_file", "").strip()
    start = issue.get("start_line", "")
    end = issue.get("end_line", "")

    icon, severity = _get_review_severity(header)

    parts = []
    parts.append(f"_{icon} {severity}_ | _{header}_")
    parts.append("")
    parts.append(f"**{content}**")
    parts.append("")

    # Agent prompt
    agent_lines = [f"In file `{filepath}`"]
    if start and end and str(start) != str(end):
        agent_lines.append(f"around lines {start}-{end}:")
    elif start:
        agent_lines.append(f"around line {start}:")
    agent_lines.append("")
    agent_lines.append(f"[{header}] {content}")
    agent_lines.append("")
    agent_lines.append(f"Action required: Investigate and fix the issue described above.")

    parts.append("<details><summary>🤖 Prompt for AI Agents</summary>")
    parts.append("")
    parts.append("```text")
    parts.extend(agent_lines)
    parts.append("```")
    parts.append("")
    parts.append("</details>")

    return "\n".join(parts)


def build_aggregated_agent_prompt(issues: list) -> str:
    """Build a single aggregated prompt for all review findings (CodeRabbit style)."""
    lines = ["Verify each finding against the current code and only fix it if needed.", ""]
    lines.append("Inline comments:")

    for issue in issues:
        filepath = issue.get("relevant_file", "").strip()
        start = issue.get("start_line", "")
        end = issue.get("end_line", "")
        header = issue.get("issue_header", "").strip()
        content = issue.get("issue_content", "").strip()

        lines.append(f"In @{filepath}:")
        loc = f"lines {start}-{end}" if start and end and str(start) != str(end) else f"line {start}"
        lines.append(f"- Around {loc}: [{header}] {content}")
        lines.append("")

    return "\n".join(lines)


def build_review_body_additions(findings: list, inline_count: int,
                                diff_files=None) -> str:
    """Build CodeRabbit-style additions to prepend/append to review body.

    Adds: actionable comments count, aggregated AI prompt, review info.
    """
    parts = []

    # Actionable comments count (CodeRabbit header)
    parts.append(f"**Actionable comments posted: {inline_count}**")
    parts.append("")

    # Aggregated AI prompt for all findings
    if findings:
        prompt = build_aggregated_agent_prompt(findings)
        parts.append("<details>")
        parts.append("<summary>🤖 Prompt for all review comments with AI agents</summary>")
        parts.append("")
        parts.append("```")
        parts.append(prompt)
        parts.append("```")
        parts.append("")
        parts.append("</details>")
        parts.append("")

    # Review info section
    file_list = []
    if diff_files:
        try:
            file_list = [getattr(f, 'filename', str(f)) for f in diff_files[:30]]
        except Exception:
            pass

    parts.append("---")
    parts.append("")
    parts.append("<details>")
    parts.append("<summary>ℹ️ Review info</summary>")
    parts.append("")
    parts.append("<details>")
    parts.append("<summary>⚙️ Configuration</summary>")
    parts.append("")
    parts.append("**Engine**: PR-Agent 0.34 + Argus patches")
    parts.append("**Model**: gpt-5.3-codex")
    parts.append("**Mode**: CodeRabbit-compatible")
    parts.append("")
    parts.append("</details>")
    parts.append("")
    if file_list:
        parts.append("<details>")
        parts.append(f"<summary>📒 Files reviewed ({len(file_list)})</summary>")
        parts.append("")
        for f in file_list:
            parts.append(f"* `{f}`")
        parts.append("")
        parts.append("</details>")
        parts.append("")
    parts.append("</details>")

    return "\n".join(parts)


# ── Thread auto-resolve ──────────────────────────────────────────

def _get_changed_files_lines(auth_h, full_name, pr_number, since_sha=None):
    """Get set of (filepath, line) tuples changed since a given commit.

    If since_sha is provided, compares since_sha...HEAD (captures all fixes
    since the last Argus review).  Otherwise falls back to comparing the last
    two PR commits (single-commit delta).
    Falls back to empty set on failure (disables fix-detection, keeps isOutdated path).
    """
    import requests as _req

    try:
        # Get the PR commits
        r = _req.get(f"https://api.github.com/repos/{full_name}/pulls/{pr_number}/commits",
                     headers=auth_h, timeout=15)
        if r.status_code != 200:
            return set()
        commits = r.json()
        if len(commits) < 2:
            return set()

        head_sha = commits[-1]["sha"]
        base_sha = since_sha or commits[-2]["sha"]

        # Get diff between base and head
        r = _req.get(f"https://api.github.com/repos/{full_name}/compare/{base_sha}...{head_sha}",
                     headers=auth_h, timeout=15)
        if r.status_code != 200:
            return set()

        changed = set()
        for f in r.json().get("files", []):
            filepath = f.get("filename", "")
            if not filepath:
                continue
            # Parse patch hunks for changed line numbers
            patch = f.get("patch", "")
            if not patch:
                # Whole file changed (binary or rename) — mark all lines
                changed.add((filepath, None))
                continue
            for line in patch.split("\n"):
                if line.startswith("@@"):
                    # Parse @@ -a,b +c,d @@ format
                    try:
                        plus_part = line.split("+")[1].split("@@")[0].strip()
                        start = int(plus_part.split(",")[0])
                        count = int(plus_part.split(",")[1]) if "," in plus_part else 1
                        for ln in range(start, start + count):
                            changed.add((filepath, ln))
                    except (IndexError, ValueError):
                        pass
        return changed
    except Exception as e:
        print(f"[Argus] Changed-files detection failed: {e}")
        return set()


def _resolve_thread(auth_h, thread_id):
    """Resolve a single review thread via GraphQL mutation."""
    import requests as _req

    mutation = """mutation {
      resolveReviewThread(input: {threadId: "%s"}) {
        thread { isResolved }
      }
    }""" % thread_id
    m = _req.post("https://api.github.com/graphql",
                  json={"query": mutation}, headers=auth_h, timeout=10)
    if m.status_code == 200:
        data = m.json()
        if data.get("data", {}).get("resolveReviewThread", {}).get("thread", {}).get("isResolved"):
            return True
        if data.get("errors"):
            print(f"[Argus] Resolve failed for {thread_id}: {data['errors']}")
    else:
        print(f"[Argus] Resolve request failed: {m.status_code}")
    return False


def _reply_to_thread(auth_h, full_name, pr_number, thread_comment_id, body):
    """Post a reply to a review thread (uses REST pull request comment reply)."""
    import requests as _req

    r = _req.post(
        f"https://api.github.com/repos/{full_name}/pulls/{pr_number}/comments/{thread_comment_id}/replies",
        json={"body": body}, headers=auth_h, timeout=10)
    return r.status_code == 201


def _judge_reply_with_llm(original_finding, reply_body):
    """Use LLM to judge whether a reply justifies resolving a thread.

    Uses PR-Agent's AiHandler which manages model config and API keys.

    Returns: ("ACCEPT", reason) | ("REJECT", follow_up) | ("ESCALATE", reason)
    """
    try:
        from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
        from pr_agent.config_loader import get_settings
        import asyncio

        model = get_settings().get("config.model", "")
        if not model:
            return "ESCALATE", "LLM error: No model configured"

        system_prompt = (
            "You are a code review arbitrator. A reviewer posted a finding on a pull request, "
            "and the PR author replied. Judge the reply.\n\n"
            "Respond with EXACTLY one of these formats:\n"
            "ACCEPT: <one-line reason why the reply is valid>\n"
            "REJECT: <one-line follow-up question or counter-argument>\n"
            "ESCALATE: <one-line reason this needs human review>\n\n"
            "Rules:\n"
            "- ACCEPT if the reply provides a valid justification, demonstrates the finding "
            "is a false positive, explains why the current code is correct, OR resolves a "
            "trade-off with concrete technical reasoning such as cost estimates, scope/lifetime "
            "arguments (e.g. 'runs <N times before deprecation'), mitigation references "
            "(existing guard, fail-fast, alternate code path), or false-positive demonstration. "
            "Almost every engineering decision involves SOME trade-off — the mere presence of a "
            "trade-off is NOT grounds to escalate. If the reply supplies a dominant technical "
            "argument, ACCEPT it.\n"
            "- REJECT if the reply is vague (e.g. bare 'I prefer X' / 'I don't like this' with "
            "no reasoning), doesn't address the finding, or the justification is technically "
            "incorrect.\n"
            "- ESCALATE ONLY in these narrow cases:\n"
            "  (a) The reply surfaces an IRREDUCIBLE value preference that cannot be resolved "
            "from the code in this PR alone (e.g. vendor choice, product-policy decisions, "
            "cross-team ownership questions), where no technical argument in the thread is "
            "dominant; OR\n"
            "  (b) The author EXPLICITLY requests human judgment (phrases like 'needs maintainer "
            "decision', 'flagging for review', 'please have @someone decide', 'escalate this').\n"
            "- Do NOT escalate merely because the discussion mentions architecture, trade-offs, "
            "performance-vs-reliability tensions, or policy — those are normal engineering "
            "content and most are technically resolvable. Escalation is reserved for the two "
            "narrow cases above.\n"
            "- Mitigation-claim handling: the classifier CANNOT verify any claim "
            "against the actual code — it only sees the finding text and the reply "
            "text. So the rule is shape-based, not truth-based. A mitigation reference "
            "counts as SPECIFIC only when the reply contains at least one of: a "
            "backtick-wrapped identifier (e.g. `retry_guard`), a file:line reference "
            "(e.g. `utils/net.py:42`), a commit hash or PR number (e.g. `abc1234`, "
            "`#123`), a fully-qualified function / class name (e.g. `foo.bar.Baz`), "
            "or a named workflow / config key. If the reply contains at least one "
            "such concrete identifier, treat the claim at face value and ACCEPT — do "
            "NOT try to verify the referenced thing exists or does what the author "
            "says. If the reply has NO concrete identifier (e.g. 'we have guards', "
            "'it's already handled', 'the existing code covers this'), treat it as "
            "non-responsive and REJECT with a follow-up asking the author to name "
            "the specific mitigation with a code reference. This closes the "
            "trojan-horse ACCEPT path in the least-subjective way available to a "
            "classifier without code context: the author must at minimum produce "
            "something that looks like a code identifier. Fabricated-but-identifier-"
            "shaped references are out of scope for this classifier — that is what "
            "human review + the actual code author's conscience catch.\n"
            "- When uncertain between ACCEPT and REJECT, prefer REJECT (ask a follow-up). "
            "Do NOT default to ESCALATE when uncertain — escalation requires meeting criterion "
            "(a) or (b) above."
        )
        user_prompt = (
            f"## Original Finding\n{original_finding}\n\n"
            f"## Author Reply\n{reply_body}\n\n"
            f"Your judgment:"
        )

        ai_handler = LiteLLMAIHandler()

        # Get or create event loop for async call
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # Already in async context — create a task
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                text, _ = pool.submit(
                    asyncio.run,
                    ai_handler.chat_completion(
                        model=model, system=system_prompt, user=user_prompt)
                ).result(timeout=30)
        else:
            text, _ = asyncio.run(
                ai_handler.chat_completion(
                    model=model, system=system_prompt, user=user_prompt))

        text = text.strip()
        for verdict in ("ACCEPT", "REJECT", "ESCALATE"):
            if text.upper().startswith(verdict):
                reason = text[len(verdict):].lstrip(":").strip()
                return verdict, reason
        return "ESCALATE", f"LLM error: Unparseable response: {text[:100]}"

    except Exception as e:
        print(f"[Argus] LLM reply judgment failed: {e}")
        return "ESCALATE", f"LLM error: {e}"


# Max reply-judgment rounds per thread before forced escalation
MAX_REPLY_ROUNDS = 3


def _is_bot_author(login, bot_login):
    """Check if a login matches the bot, handling GraphQL vs REST naming.

    REST API returns 'argus-review[bot]', GraphQL returns 'argus-review'.
    """
    if not login or not bot_login:
        return False
    return login == bot_login or login == bot_login.replace("[bot]", "")


def auto_resolve_outdated_threads(provider, pr_number, bot_login="argus-review[bot]"):
    """Resolve Argus review threads using fix-detection + reply-aware judging.

    Resolution strategies (in priority order):
    1. Fix-detection: thread targets a file+line modified in latest commit → resolve
    2. isOutdated fallback: GitHub marks thread as outdated → resolve
    3. Reply-aware: thread has human reply → LLM judges ACCEPT/REJECT/ESCALATE

    Threads authored by non-Argus users are never touched.
    Threads with incomplete comment data are skipped for safety.

    Refs:
    - https://docs.github.com/en/graphql/reference/mutations#resolvereviewthread
    """
    import requests as _req

    try:
        repo = provider.repo
        if not repo:
            return 0
        full_name = repo.full_name if hasattr(repo, 'full_name') else str(repo)
        owner, name = full_name.split("/", 1)

        token = _get_github_token(provider)
        if not token:
            print("[Argus] No token — skipping auto-resolve")
            return 0

        auth_h = {"Authorization": f"Bearer {token}",
                  "Accept": "application/vnd.github+json"}

        # Find the commit SHA of the last Argus review so fix-detection covers
        # ALL changes since that review, not just the latest single commit.
        last_review_sha = None
        try:
            r = _req.get(f"https://api.github.com/repos/{full_name}/pulls/{pr_number}/reviews",
                         headers=auth_h, timeout=15)
            if r.status_code == 200:
                for rv in reversed(r.json()):
                    if _is_bot_author(rv.get("user", {}).get("login", ""), bot_login):
                        last_review_sha = rv.get("commit_id")
                        break
        except Exception:
            pass
        if last_review_sha:
            print(f"[Argus] Fix-detection base: last review commit {last_review_sha[:7]}")

        # Build set of (file, line) changed since last review for fix-detection
        changed_lines = _get_changed_files_lines(auth_h, full_name, pr_number,
                                                  since_sha=last_review_sha)

        # Query threads with path, line, body for fix-detection + reply judging
        query = """{
          repository(owner: "%s", name: "%s") {
            pullRequest(number: %d) {
              reviewThreads(first: 100) {
                nodes {
                  id
                  isResolved
                  isOutdated
                  path
                  line
                  comments(first: 20) {
                    totalCount
                    nodes {
                      id
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
            print(f"[Argus] Auto-resolve query failed: {g.status_code}")
            return 0

        threads = g.json()["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
        resolved_count = 0
        reply_judged = 0

        for t in threads:
            if t["isResolved"]:
                continue

            comments = t["comments"]
            authors = [c["author"]["login"] for c in comments["nodes"] if c.get("author")]

            # Only touch Argus-authored threads
            if not any(_is_bot_author(a, bot_login) for a in authors):
                continue

            # Skip if we couldn't fetch all comments
            if comments.get("totalCount", 0) > len(comments["nodes"]):
                continue

            human_authors = [a for a in authors if not _is_bot_author(a, bot_login)]
            thread_id = t["id"]
            thread_path = t.get("path", "")
            thread_line = t.get("line")

            # --- Strategy 1: Fix-detection (file+line in latest commit diff) ---
            if not human_authors and changed_lines and thread_path:
                # Check if the exact line was modified, or if the whole file changed
                file_touched = (thread_path, thread_line) in changed_lines or \
                               (thread_path, None) in changed_lines
                if file_touched:
                    if _resolve_thread(auth_h, thread_id):
                        resolved_count += 1
                        print(f"[Argus] Fix-detected: {thread_path}:{thread_line} → resolved")
                    continue

            # --- Strategy 2: isOutdated ---
            # Resolve unconditionally: if GitHub marks the code as changed,
            # the original context is moot (even if a human replied).
            if t.get("isOutdated", False):
                if _resolve_thread(auth_h, thread_id):
                    resolved_count += 1
                    print(f"[Argus] Outdated: {thread_path}:{thread_line} → resolved")
                continue

            # --- Strategy 3: Reply-aware judging (thread has human replies) ---
            if human_authors:
                # Extract first bot comment DB ID early (needed for escalation post)
                first_comment_db_id = None
                for c in comments["nodes"]:
                    if _is_bot_author(c.get("author", {}).get("login", ""), bot_login):
                        first_comment_db_id = c.get("databaseId")
                        break

                # Count how many Argus judgment replies already exist
                argus_judgment_count = sum(
                    1 for c in comments["nodes"]
                    if _is_bot_author(c.get("author", {}).get("login", ""), bot_login)
                    and any(tag in c.get("body", "") for tag in ("✅ Acknowledged", "❓ Follow-up", "⚠️ Escalated")))

                if argus_judgment_count >= MAX_REPLY_ROUNDS:
                    # Post final ⚠️ Escalated if not already done, then stop judging
                    already_escalated = any(
                        "⚠️ Escalated" in c.get("body", "")
                        for c in comments["nodes"]
                        if _is_bot_author(c.get("author", {}).get("login", ""), bot_login))
                    if not already_escalated and first_comment_db_id:
                        _reply_to_thread(auth_h, full_name, pr_number, first_comment_db_id,
                                         "⚠️ Escalated — Maximum reply rounds reached. "
                                         "Human reviewer decision required.")
                        print(f"[Argus] Max reply rounds: escalation posted for {thread_path}")
                    continue

                # Skip if the last comment is already a bot judgment — no new
                # human input to judge, re-running would duplicate the reply.
                JUDGMENT_TAGS = ("✅ Acknowledged", "❓ Follow-up", "⚠️ Escalated")
                last_comment = comments["nodes"][-1]
                last_is_bot = _is_bot_author(
                    last_comment.get("author", {}).get("login", ""), bot_login)
                if last_is_bot and any(tag in last_comment.get("body", "") for tag in JUDGMENT_TAGS):
                    continue

                # Get original finding (first Argus comment) and latest human reply
                original_finding = ""
                latest_reply = ""
                for c in comments["nodes"]:
                    if _is_bot_author(c.get("author", {}).get("login", ""), bot_login) and not original_finding:
                        original_finding = c.get("body", "")
                    if c.get("author", {}).get("login") in human_authors:
                        latest_reply = c.get("body", "")

                if not original_finding or not latest_reply or not first_comment_db_id:
                    continue  # first_comment_db_id set at top of human_authors block

                verdict, reason = _judge_reply_with_llm(original_finding, latest_reply)
                reply_judged += 1

                # Never post replies for LLM errors — log only
                if verdict == "ESCALATE" and "LLM error" in reason:
                    print(f"[Argus] Reply judgment LLM failed for {thread_path}: {reason}")
                    continue

                if verdict == "ACCEPT":
                    _reply_to_thread(auth_h, full_name, pr_number,
                                     first_comment_db_id,
                                     f"✅ Acknowledged — {reason}")
                    if _resolve_thread(auth_h, thread_id):
                        resolved_count += 1
                        print(f"[Argus] Reply accepted: {thread_path} → resolved")
                elif verdict == "REJECT":
                    _reply_to_thread(auth_h, full_name, pr_number,
                                     first_comment_db_id,
                                     f"❓ Follow-up — {reason}")
                    print(f"[Argus] Reply rejected: {thread_path} → follow-up posted")
                else:  # ESCALATE (genuine, not LLM error)
                    _reply_to_thread(auth_h, full_name, pr_number,
                                     first_comment_db_id,
                                     f"⚠️ Escalated — {reason}\n\n"
                                     f"*This thread requires human reviewer input.*")
                    print(f"[Argus] Reply escalated: {thread_path}")

        if resolved_count or reply_judged:
            print(f"[Argus] Auto-resolve: {resolved_count} resolved, {reply_judged} replies judged")
        return resolved_count

    except Exception as e:
        print(f"[Argus] Auto-resolve failed: {e}")
        return 0


_app_token_cache = {"token": None, "exp": 0}


def _get_app_installation_token():
    """Generate a GitHub App installation token from app credentials.

    This ensures API calls are made as argus-review[bot] rather than
    a personal user account.  Tokens are cached for up to 1 hour.
    """
    import time

    now = int(time.time())
    if _app_token_cache["token"] and now < _app_token_cache["exp"] - 60:
        return _app_token_cache["token"]

    try:
        import jwt
        import requests as _req
        from pr_agent.config_loader import get_settings

        app_id = get_settings().get("github.app_id", "")
        private_key = get_settings().get("github.private_key", "")
        if not app_id or not private_key:
            return None

        # 1. Create JWT
        payload = {"iat": now - 60, "exp": now + 600, "iss": int(app_id)}
        encoded = jwt.encode(payload, private_key, algorithm="RS256")
        if isinstance(encoded, bytes):
            encoded = encoded.decode("utf-8")

        headers = {"Authorization": f"Bearer {encoded}",
                   "Accept": "application/vnd.github+json"}

        # 2. Find installation ID
        r = _req.get("https://api.github.com/app/installations",
                     headers=headers, timeout=10)
        data = r.json() if r.status_code == 200 else []
        if not data:
            print(f"[Argus] Failed to list installations: {r.status_code}")
            return None
        installation_id = data[0]["id"]

        # 3. Create installation access token
        r = _req.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers=headers, timeout=10)
        if r.status_code == 201:
            token = r.json()["token"]
            _app_token_cache["token"] = token
            _app_token_cache["exp"] = now + 3600
            return token
        print(f"[Argus] Failed to create installation token: {r.status_code}")
        return None

    except Exception as e:
        print(f"[Argus] App token generation failed: {e}")
        return None


def _get_github_token(provider):
    """Extract GitHub token from PR-Agent provider."""
    token = None
    try:
        token = provider.github_client._Github__requester._Requester__auth.token
    except Exception:
        pass
    if not token:
        try:
            token = provider.github_client._Github__requester.auth.token
        except Exception:
            pass
    if not token:
        try:
            token = _get_app_installation_token()
        except Exception:
            pass
    return token


# ── Walkthrough format ────────────────────────────────────────────

def format_walkthrough_comment(data: dict, diagram: str = "") -> str:
    """Format /describe prediction as CodeRabbit-style walkthrough comment.

    Args:
        data: Parsed prediction dict with keys: description, pr_files, type
        diagram: Optional Mermaid diagram string
    """
    parts = []
    parts.append("<!-- walkthrough_start -->")
    parts.append("<details open>")
    parts.append("<summary>📝 Walkthrough</summary>")
    parts.append("")

    # Summary section
    description = data.get("description", "")
    if description:
        parts.append("## Summary")
        parts.append("")
        # description may be a string with bullet points or a plain string
        if isinstance(description, str):
            for line in description.strip().split("\n"):
                line = line.strip()
                if line and not line.startswith("-"):
                    line = f"- {line}"
                if line:
                    parts.append(line)
        parts.append("")

    # Changes table grouped by semantic label
    pr_files = data.get("pr_files", [])
    if pr_files and isinstance(pr_files, list):
        parts.append("## Changes")
        parts.append("")
        parts.append("| Files | Summary |")
        parts.append("|-------|---------|")

        # Group files by label
        groups = {}
        for f in pr_files:
            if not isinstance(f, dict):
                continue
            label = str(f.get("label", "other") or "other").strip()
            groups.setdefault(label, []).append(f)

        for label, files in groups.items():
            filenames = ", ".join(
                f"`{str(f.get('filename', '?') or '?').strip()}`" for f in files)
            # Combine summaries
            summaries = []
            for f in files:
                title = str(f.get("changes_title", "") or "").strip()
                # Escape pipe characters that break Markdown tables
                title = title.replace("|", "\\|").replace("\n", " ")
                if title:
                    summaries.append(title)
            summary_text = "; ".join(summaries) if summaries else "—"
            parts.append(f"| **{label}** {filenames} | {summary_text} |")

        if not groups:
            # Remove empty table header if no valid files
            parts.pop()  # "|-------|---------|"
            parts.pop()  # "| Files | Summary |"

        parts.append("")

    # Diagram section
    if diagram and isinstance(diagram, str) and diagram.strip():
        parts.append("## Diagram")
        parts.append("")
        # Diagram may already be wrapped in ```mermaid``` or not
        diag = diagram.strip()
        # Sanitize: strip backticks inside node labels — they break Mermaid rendering.
        # Matches `text` within ["..."] or ("...") node labels and removes the backticks.
        import re
        diag = re.sub(r'`([^`\n]+)`', r'\1', diag)
        if not diag.startswith("```"):
            parts.append("```mermaid")
            parts.append(diag)
            parts.append("```")
        else:
            parts.append(diag)
        parts.append("")

    parts.append("</details>")
    parts.append("<!-- walkthrough_end -->")

    return "\n".join(parts)


# ── Apply patches ─────────────────────────────────────────────────

def apply_patch():
    """Monkey-patch PR-Agent for CodeRabbit-style output."""

    # ── Patch 1: /improve inline suggestions ──
    try:
        from pr_agent.tools import pr_code_suggestions

        original_push = pr_code_suggestions.PRCodeSuggestions.push_inline_code_suggestions

        def patched_push(self, data):
            code_suggestions = []
            if not data.get("code_suggestions"):
                return original_push(self, data)

            for d in data["code_suggestions"]:
                try:
                    relevant_file = d["relevant_file"].strip()
                    relevant_lines_start = int(d["relevant_lines_start"])
                    relevant_lines_end = int(d["relevant_lines_end"])
                    new_code_snippet = d.get("improved_code", "").rstrip()
                    if new_code_snippet:
                        new_code_snippet = self.dedent_code(
                            relevant_file, relevant_lines_start, new_code_snippet)
                    body = format_suggestion_body(d, new_code_snippet)
                    code_suggestions.append({
                        "body": body,
                        "relevant_file": relevant_file,
                        "relevant_lines_start": relevant_lines_start,
                        "relevant_lines_end": relevant_lines_end,
                        "original_suggestion": d,
                    })
                except Exception as e:
                    print(f"[Argus] Could not format suggestion: {e}")

            is_successful = self.git_provider.publish_code_suggestions(code_suggestions)
            if not is_successful:
                for cs in code_suggestions:
                    self.git_provider.publish_code_suggestions([cs])
            return True

        pr_code_suggestions.PRCodeSuggestions.push_inline_code_suggestions = patched_push
        print("[Argus] /improve format patched")
    except Exception as e:
        print(f"[Argus] Failed to patch /improve: {e}")

    # ── Patch 2+3: /review → single unified GitHub Review (body + inline threads) ──
    #
    # Strategy:
    #   - Patch publish_persistent_comment/publish_comment to CAPTURE review body
    #     instead of posting it immediately (store in _argus_review_body)
    #   - Patch PRReviewer.run to: run original (captures body), then build inline
    #     comments from key_issues, then create ONE review with body + comments
    #   - Delete the placeholder "Preparing review..." comment
    #
    try:
        from pr_agent.tools import pr_reviewer
        from pr_agent.algo.utils import load_yaml
        from pr_agent.git_providers import github_provider as gh_mod
        from pr_agent.git_providers.github_provider import find_line_number_of_relevant_line_in_file

        # -- Step A: Intercept publish to capture review body --
        original_publish_persistent = gh_mod.GithubProvider.publish_persistent_comment
        original_publish_comment = gh_mod.GithubProvider.publish_comment

        def _is_review_content(body):
            return isinstance(body, str) and ("PR Reviewer Guide" in body or "Reviewer Guide" in body)

        def patched_publish_persistent(self, body, initial_header="", **kwargs):
            if _is_review_content(body):
                # Capture body, don't publish yet — patched_run will post unified review
                self._argus_review_body = body
                print(f"[Argus] Captured review body ({len(body)} chars), deferring publish")
                return
            return original_publish_persistent(self, body, initial_header=initial_header, **kwargs)

        def patched_publish_comment(self, body, is_temporary=False):
            if not is_temporary and _is_review_content(body):
                self._argus_review_body = body
                print(f"[Argus] Captured review body ({len(body)} chars), deferring publish")
                return
            # Suppress noisy PR-Agent error comments that leak to PR
            if isinstance(body, str) and "Failed to generate" in body:
                print(f"[Argus] Suppressed error comment: {body[:80]}")
                return
            return original_publish_comment(self, body, is_temporary=is_temporary)

        gh_mod.GithubProvider.publish_persistent_comment = patched_publish_persistent
        gh_mod.GithubProvider.publish_comment = patched_publish_comment

        # -- Step B: After original run, combine body + inline → one Review --
        # -- with conditional approval algorithm --
        original_run = pr_reviewer.PRReviewer.run

        # Approval config
        BLOCKING_SEVERITIES = {"Critical", "Major"}
        MAX_ITERATIONS = 10
        BOT_LOGIN = "argus-review[bot]"

        # Doc PR config: suppress Minor findings after this many iterations
        _DOC_EXTS = frozenset({'md', 'mdx', 'rst', 'txt', 'adoc'})
        _DOC_NITS_SUPPRESS_AFTER = 3

        def _is_doc_pr(df):
            """True if ≥70% of changed files are documentation files."""
            if not df:
                return False
            total = len(df)
            if not total:
                return False
            doc_count = sum(
                1 for f in df
                if (getattr(f, 'filename', None) or '').rsplit('.', 1)[-1].lower() in _DOC_EXTS
            )
            return doc_count / total >= 0.7

        def _classify_finding_severity(issue_header: str) -> str:
            h = issue_header.lower()
            if any(k in h for k in ("critical", "bug", "security")):
                return "Critical"
            if any(k in h for k in ("possible", "error", "major")):
                return "Major"
            if any(k in h for k in ("performance", "issue", "medium")):
                return "Medium"
            return "Minor"

        def _get_thread_state(provider, pr_number):
            """Query unresolved Argus threads + past review count.
            Uses REST API for reviews (reliable) + GraphQL for thread resolution.
            REST: GET /repos/{owner}/{repo}/pulls/{number}/reviews
            GraphQL: reviewThreads.isResolved (REST doesn't expose this)
            Refs: https://docs.github.com/en/rest/pulls/reviews
            """
            import requests as _req

            try:
                repo = provider.repo
                if not repo:
                    return [], 0, []
                full_name = repo.full_name if hasattr(repo, 'full_name') else str(repo)
                owner, name = full_name.split("/", 1)

                token = _get_github_token(provider)
                if not token:
                    print("[Argus] No token — skipping thread check")
                    return [], 0, []

                auth_h = {"Authorization": f"Bearer {token}",
                          "Accept": "application/vnd.github+json"}

                # Count past Argus /review iterations.
                # Check both review objects AND issue comments, because earlier
                # reviews may have fallen back to issue comments due to API errors.
                argus_review_count = 0
                # Source 1: review objects
                r = _req.get(f"https://api.github.com/repos/{full_name}/pulls/{pr_number}/reviews",
                             headers=auth_h, timeout=15)
                if r.status_code == 200:
                    argus_review_count += sum(
                        1 for rv in r.json()
                        if rv.get("user", {}).get("login") == BOT_LOGIN
                        and "Review Decision" in (rv.get("body") or ""))
                # Source 2: issue comments (fallback path)
                r2 = _req.get(f"https://api.github.com/repos/{full_name}/issues/{pr_number}/comments?per_page=100",
                              headers=auth_h, timeout=15)
                if r2.status_code == 200:
                    argus_review_count += sum(
                        1 for c in r2.json()
                        if _is_bot_author(c.get("user", {}).get("login", ""), BOT_LOGIN)
                        and "Review Decision" in (c.get("body") or ""))

                # GraphQL: thread resolution (exclude /improve suggestion threads)
                # Fetch up to 10 comments to reliably detect judgment round count.
                argus_unresolved = []
                argus_escalated = []
                query = '{repository(owner:"%s",name:"%s"){pullRequest(number:%d){reviewThreads(first:100){nodes{isResolved isOutdated comments(first:10){nodes{author{login} body}}}}}}}' % (owner, name, pr_number)
                g = _req.post("https://api.github.com/graphql",
                              json={"query": query}, headers=auth_h, timeout=15)
                if g.status_code == 200 and "data" in g.json():
                    threads = g.json()["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
                    for t in threads:
                        authors = [c["author"]["login"] for c in t["comments"]["nodes"] if c.get("author")]
                        if not any(_is_bot_author(a, BOT_LOGIN) for a in authors) or t["isResolved"]:
                            continue
                        # Skip /improve suggestion threads (optional style suggestions)
                        first_body = t["comments"]["nodes"][0].get("body", "") if t["comments"]["nodes"] else ""
                        if "Committable suggestion" in first_body or "📝 Suggestion" in first_body:
                            continue
                        # Separate escalated threads (⚠️ Escalated posted) from actively blocking ones
                        is_escalated = any(
                            "⚠️ Escalated" in c.get("body", "")
                            for c in t["comments"]["nodes"]
                            if _is_bot_author(c.get("author", {}).get("login", ""), BOT_LOGIN))
                        if is_escalated:
                            argus_escalated.append(t)
                        else:
                            argus_unresolved.append(t)

                print(f"[Argus] Thread state: {len(argus_unresolved)} unresolved, "
                      f"{len(argus_escalated)} escalated, {argus_review_count} past reviews")
                return argus_unresolved, argus_review_count, argus_escalated
            except Exception as e:
                print(f"[Argus] Thread check failed: {e}")
                return [], 0, []

        def _decide_review_event(findings, unresolved_threads, iteration,
                                 has_inline_comments=False,
                                 no_new_code=False,
                                 escalated_threads=None):
            """
            Returns (event, reason):
              "REQUEST_CHANGES" — blocking issues found
              "APPROVE"         — all clear after at least 2 iterations
              "COMMENT"         — non-blocking / first review / escalation

            has_inline_comments: True if this review will post new inline
            comments. Approval is deferred when new comments are posted,
            regardless of severity, because the author hasn't seen them yet.
            no_new_code: True if HEAD == last review commit (no new push).
            When set, new findings are noise and should not block approval.
            escalated_threads: threads that hit MAX_REPLY_ROUNDS; not counted
            as blocking (they are already flagged for human review).
            """
            escalated_threads = escalated_threads or []
            critical_major = [f for f in findings
                              if _classify_finding_severity(f.get("issue_header", "")) in BLOCKING_SEVERITIES]

            # Rule 1: Critical/Major findings always block (regardless of iteration)
            # But only on actual new code — re-analyzing unchanged code is noise.
            if critical_major and not no_new_code:
                return ("REQUEST_CHANGES",
                        f"🔴 {len(critical_major)} critical/major issue(s) — changes requested.")

            # Rule 2: Max iterations → COMMENT (escalate to human; do NOT REQUEST_CHANGES).
            # This must fire before the unresolved-thread check so that a bot stuck in a
            # disagree loop doesn't keep emitting REQUEST_CHANGES past the iteration cap.
            if iteration >= MAX_ITERATIONS:
                parts = []
                if unresolved_threads:
                    parts.append(f"{len(unresolved_threads)} thread(s) unresolved")
                if escalated_threads:
                    parts.append(f"{len(escalated_threads)} escalated to human")
                detail = " (" + ", ".join(parts) + ")" if parts else ""
                return ("COMMENT",
                        f"⚠️ Review iteration {iteration}/{MAX_ITERATIONS} reached{detail}. "
                        f"Escalating to human reviewer — no further bot blocking.")

            # Rule 3: Unresolved threads from previous reviews (only before max iterations)
            if unresolved_threads:
                return ("REQUEST_CHANGES",
                        f"🔴 {len(unresolved_threads)} unresolved thread(s) from previous review.")

            # Rule 4: First review → COMMENT (never approve on first pass)
            if iteration <= 1:
                return ("COMMENT",
                        "Initial review — no blocking issues. "
                        "Minor findings posted as inline threads.")

            # Rule 5: No new code + all threads resolved → APPROVE immediately
            # Threads were resolved via replies, no new commit to review.
            if no_new_code and not unresolved_threads:
                return ("APPROVE",
                        f"✅ All threads resolved, no new code to review. "
                        f"Iteration {iteration}/{MAX_ITERATIONS}.")

            # Rule 6: New inline comments being posted → COMMENT (defer approval)
            # Cannot APPROVE in the same API call that posts new comments,
            # because the author hasn't had a chance to see/address them.
            if has_inline_comments:
                minor_count = len(findings) - len(critical_major)
                return ("COMMENT",
                        f"💬 {minor_count} new finding(s) posted. "
                        f"Approval deferred until next review. "
                        f"Iteration {iteration}/{MAX_ITERATIONS}.")

            # Rule 7: No new comments, no blocking issues, all threads resolved → APPROVE
            return ("APPROVE",
                    f"✅ No issues found, all threads resolved. "
                    f"Iteration {iteration}/{MAX_ITERATIONS}.")

        async def patched_run(self):
            """Run /review with conditional approval + auto-resolve + format enhancements."""
            self.git_provider._argus_review_body = None

            # Phase 1: Auto-resolve outdated threads before running review
            pr_number = self.git_provider.pr_num if hasattr(self.git_provider, 'pr_num') else 0
            if pr_number:
                resolved = auto_resolve_outdated_threads(
                    self.git_provider, pr_number, BOT_LOGIN)
                if resolved:
                    print(f"[Argus] Pre-review: auto-resolved {resolved} outdated thread(s)")

            # Phase 2: Run original /review
            result = await original_run(self)

            review_body = getattr(self.git_provider, '_argus_review_body', None)
            if not review_body:
                return result

            _repo = getattr(getattr(self.git_provider, 'repo', None), 'full_name', None)
            emitter.emit(EventType.REVIEW_STARTED, pr_number=pr_number, repo=_repo)

            # Parse findings from prediction
            findings = []
            inline_comments = []
            diff_files = None
            try:
                if hasattr(self, 'prediction') and self.prediction:
                    data = load_yaml(self.prediction.strip(),
                                     keys_fix_yaml=["ticket_compliance_check",
                                                     "estimated_effort_to_review_[1-5]:",
                                                     "security_concerns:",
                                                     "key_issues_to_review:",
                                                     "relevant_file:", "relevant_line:", "suggestion:"],
                                     first_key="review", last_key="key_issues_to_review")

                    if data and 'review' in data:
                        findings = data['review'].get('key_issues_to_review', [])
                        diff_files = self.git_provider.diff_files or self.git_provider.get_diff_files()

                        for issue in findings:
                            try:
                                filepath = issue.get('relevant_file', '').strip().strip('`')
                                end_line = int(issue.get('end_line', 0))
                                if not filepath or not end_line:
                                    continue
                                position, _ = find_line_number_of_relevant_line_in_file(
                                    diff_files, filepath, "", end_line)
                                if position == -1:
                                    continue
                                body = format_review_finding_body(issue)
                                inline_comments.append({
                                    'body': body,
                                    'path': filepath,
                                    'position': position,
                                    '_end_line': end_line,
                                })
                            except Exception as e:
                                print(f"[Argus] Could not build inline comment: {e}")
            except Exception as e:
                print(f"[Argus] Failed to parse key_issues: {e}")

            # Get thread state + iteration count (after auto-resolve)
            unresolved_threads, past_reviews, escalated_threads = _get_thread_state(
                self.git_provider, pr_number)
            iteration = past_reviews + 1

            # Detect if there's new code since the last review
            no_new_code = False
            last_review_sha = None
            try:
                import requests as _req_inc
                token_inc = _get_github_token(self.git_provider)
                if token_inc:
                    repo_inc = self.git_provider.repo
                    fn_inc = repo_inc.full_name if hasattr(repo_inc, 'full_name') else str(repo_inc)
                    auth_inc = {"Authorization": f"Bearer {token_inc}",
                                "Accept": "application/vnd.github+json"}
                    r_rv = _req_inc.get(
                        f"https://api.github.com/repos/{fn_inc}/pulls/{pr_number}/reviews",
                        headers=auth_inc, timeout=15)
                    if r_rv.status_code == 200:
                        for rv in reversed(r_rv.json()):
                            if _is_bot_author(rv.get("user", {}).get("login", ""), BOT_LOGIN):
                                last_review_sha = rv.get("commit_id")
                                break
                    current_sha = getattr(self.git_provider, 'last_commit_id', None)
                    if current_sha and last_review_sha:
                        current_str = current_sha.sha if hasattr(current_sha, 'sha') else str(current_sha)
                        if current_str == last_review_sha:
                            no_new_code = True
                            print(f"[Argus] No new code since last review ({last_review_sha[:7]})")
            except Exception as e:
                print(f"[Argus] New-code detection failed (non-fatal): {e}")

            # --- Fix B: Incremental diff filter ---
            # After iteration 1, drop findings on lines NOT changed since last review.
            # Skip entirely if no new code (all findings would be noise).
            if no_new_code and inline_comments:
                print(f"[Argus] No new code: dropping all {len(inline_comments)} findings")
                inline_comments = []
            elif iteration >= 2 and inline_comments and pr_number and last_review_sha:
                try:
                    if token_inc:
                        changed = _get_changed_files_lines(auth_inc, fn_inc,
                                                            pr_number, since_sha=last_review_sha)
                        if changed:
                            def _line_in_changed(path, end_line, changed_set):
                                if (path, None) in changed_set:
                                    return True
                                return any((path, ln) in changed_set
                                           for ln in range(end_line - 5, end_line + 6))

                            before = len(inline_comments)
                            inline_comments = [
                                c for c in inline_comments
                                if _line_in_changed(c['path'], c['_end_line'], changed)
                            ]
                            dropped = before - len(inline_comments)
                            if dropped:
                                print(f"[Argus] Incremental filter: dropped {dropped}/{before} findings on unchanged lines")
                except Exception as e:
                    print(f"[Argus] Incremental filter failed (non-fatal): {e}")

            # --- Fix C: Deduplicate against existing open threads ---
            # Skip findings if an open thread already exists on same path within +-5 lines.
            if inline_comments and unresolved_threads:
                try:
                    existing = set()
                    for t in unresolved_threads:
                        tp = t.get("path", "")
                        if not tp:
                            continue
                        tl = t.get("line")
                        if tl:
                            for off in range(-5, 6):
                                existing.add((tp, tl + off))
                        else:
                            existing.add((tp, None))

                    before = len(inline_comments)
                    inline_comments = [
                        c for c in inline_comments
                        if (c['path'], None) not in existing
                        and not any((c['path'], c['_end_line'] + off) in existing
                                    for off in range(-2, 3))
                    ]
                    dropped = before - len(inline_comments)
                    if dropped:
                        print(f"[Argus] Dedup: dropped {dropped}/{before} findings near existing threads")
                except Exception as e:
                    print(f"[Argus] Dedup failed (non-fatal): {e}")

            # --- Fix D: Doc PR Minor suppression ---
            # For documentation-only PRs, suppress Minor findings after
            # _DOC_NITS_SUPPRESS_AFTER iterations to break the nit-loop cycle.
            is_doc = _is_doc_pr(diff_files)
            if is_doc and iteration > _DOC_NITS_SUPPRESS_AFTER and inline_comments:
                sev_map = {}
                for _f in findings:
                    try:
                        _k = (_f.get('relevant_file', '').strip().strip('`'),
                              int(_f.get('end_line') or 0))
                        sev_map[_k] = _classify_finding_severity(_f.get('issue_header', ''))
                    except (ValueError, TypeError):
                        pass  # malformed end_line — skip; inline_comments entry will default to Minor
                before = len(inline_comments)
                inline_comments = [
                    c for c in inline_comments
                    if sev_map.get((c['path'], c['_end_line']), 'Minor') in BLOCKING_SEVERITIES
                ]
                dropped = before - len(inline_comments)
                if dropped:
                    print(f"[Argus] Doc PR: suppressed {dropped} minor finding(s) "
                          f"(iteration {iteration} > {_DOC_NITS_SUPPRESS_AFTER})")

            # Decide review event
            event, reason = _decide_review_event(
                findings, unresolved_threads, iteration,
                has_inline_comments=bool(inline_comments),
                no_new_code=no_new_code,
                escalated_threads=escalated_threads)

            # Build enhanced review body (CodeRabbit-style)
            body_additions = build_review_body_additions(
                findings, len(inline_comments), diff_files)

            # Strip PR Reviewer Guide from incremental reviews (iteration >= 2)
            # The full guide is only useful on the first review; subsequent reviews
            # should focus on incremental findings only.
            if iteration >= 2 and "PR Reviewer Guide" in review_body:
                import re
                # Remove the guide section (## PR Reviewer Guide ... up to next ## or end)
                review_body = re.sub(
                    r'## PR Reviewer Guide.*?(?=\n## |\n---|\Z)',
                    '', review_body, flags=re.DOTALL).strip()

            review_body = body_additions + "\n\n" + review_body

            # Append decision footer
            review_body += f"\n\n---\n**Review Decision**: {reason}\n"
            review_body += f"*Iteration {iteration}/{MAX_ITERATIONS} | "
            review_body += f"{len(findings)} findings | "
            review_body += f"{len(unresolved_threads)} unresolved"
            if escalated_threads:
                review_body += f" | {len(escalated_threads)} escalated"
            if is_doc:
                review_body += " | doc PR"
            review_body += "*"

            # Post unified review
            # Strip internal keys before passing to PyGithub (GitHub API rejects unknown fields)
            api_comments = [{k: v for k, v in c.items() if not k.startswith('_')}
                            for c in inline_comments]
            try:
                self.git_provider.pr.create_review(
                    commit=self.git_provider.last_commit_id,
                    body=review_body,
                    event=event,
                    comments=api_comments,
                )
                n = len(inline_comments)
                print(f"[Argus] Posted {event} review ({n} inline, iteration {iteration})")
                emitter.emit(EventType.FINDING_POSTED, pr_number=pr_number, repo=_repo,
                             finding_count=n, review_event=event, iteration=iteration)
            except Exception as e:
                print(f"[Argus] Unified review failed ({e}), fallback to COMMENT")
                try:
                    self.git_provider.pr.create_review(
                        commit=self.git_provider.last_commit_id,
                        body=review_body,
                        event="COMMENT",
                        comments=api_comments,
                    )
                except Exception as e2:
                    print(f"[Argus] All posting failed ({e2})")
                    original_publish_comment(self.git_provider, review_body)

            # Edit placeholder
            try:
                if hasattr(self.git_provider, 'pr') and hasattr(self.git_provider.pr, 'comments_list'):
                    for c in getattr(self.git_provider.pr, 'comments_list', []):
                        if getattr(c, 'is_temporary', False):
                            n = len(inline_comments)
                            status = "✅" if event == "APPROVE" else "🔴" if event == "REQUEST_CHANGES" else "💬"
                            c.edit(f"{status} Review complete — **{n} findings** | {reason}")
                            break
            except Exception:
                try:
                    self.git_provider.remove_initial_comment()
                except Exception:
                    pass

            return result

        pr_reviewer.PRReviewer.run = patched_run
        print("[Argus] /review with conditional approval patched")
    except Exception as e:
        print(f"[Argus] Failed to patch /review: {e}")

    # ── Patch 4: /describe → CodeRabbit-style walkthrough comment ──
    #
    # Strategy:
    #   - Patch PRDescription.run to: run original, then build walkthrough
    #     comment from parsed prediction data, post as separate issue comment.
    #   - Original behavior (update PR body) is preserved.
    #   - Walkthrough is posted as a persistent comment with HTML markers
    #     so it can be updated on subsequent /describe runs.
    #
    try:
        from pr_agent.tools import pr_description

        original_describe_run = pr_description.PRDescription.run

        async def patched_describe_run(self):
            """Run /describe and additionally post walkthrough comment."""
            result = await original_describe_run(self)

            # Extract prediction data for walkthrough
            try:
                data = getattr(self, 'data', None)
                if not data or not isinstance(data, dict):
                    return result

                pr_files = data.get("pr_files", [])
                if not pr_files:
                    print("[Argus] /describe: no pr_files in prediction, skipping walkthrough")
                    return result

                # Extract diagram if available
                diagram = data.get("changes_diagram", "")

                # Build walkthrough comment
                walkthrough = format_walkthrough_comment(data, diagram)

                # Post as persistent comment (updates existing walkthrough)
                # Use git_provider.pr directly to avoid review-body interceptor
                pr = self.git_provider.pr
                marker = "<!-- walkthrough_start -->"
                updated = False
                try:
                    for comment in pr.get_issue_comments():
                        if marker in (comment.body or ""):
                            try:
                                comment.edit(walkthrough)
                                updated = True
                            except Exception as edit_err:
                                print(f"[Argus] Walkthrough edit failed: {edit_err}")
                            break
                except Exception:
                    pass  # comment scan failed, will create new
                if not updated:
                    pr.create_issue_comment(walkthrough)
                n_files = len(pr_files) if isinstance(pr_files, list) else 0
                print(f"[Argus] Posted walkthrough comment ({n_files} files)")

            except Exception as e:
                print(f"[Argus] Walkthrough comment failed: {e}")

            return result

        pr_description.PRDescription.run = patched_describe_run
        print("[Argus] /describe walkthrough patched")
    except Exception as e:
        print(f"[Argus] Failed to patch /describe: {e}")
