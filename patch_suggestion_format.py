"""
Argus — Patches PR-Agent output to CodeRabbit-style structured format.

Review body: summary + aggregated 🤖 Prompt for all comments
Inline threads: severity badge, description, suggestion, committable, agent prompt
Thread auto-resolve: resolve outdated threads after push
@mention support: rewrite @argus-review mentions as /ask commands

Reference: CodeRabbit PR review format (2026)
"""

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

def _get_changed_files_lines(auth_h, full_name, pr_number):
    """Get set of (filepath, line) tuples changed in the latest commit of a PR.

    Uses the compare API to get the diff between the last two commits.
    Falls back to empty set on failure (disables fix-detection, keeps isOutdated path).
    """
    import requests as _req

    try:
        # Get the latest two commits on the PR
        r = _req.get(f"https://api.github.com/repos/{full_name}/pulls/{pr_number}/commits",
                     headers=auth_h, timeout=15)
        if r.status_code != 200:
            return set()
        commits = r.json()
        if len(commits) < 2:
            return set()

        base_sha = commits[-2]["sha"]
        head_sha = commits[-1]["sha"]

        # Get diff between last two commits
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
            "is a false positive, or explains why the current code is correct.\n"
            "- REJECT if the reply is vague, doesn't address the finding, or the justification "
            "is technically incorrect.\n"
            "- ESCALATE if the discussion involves architecture decisions, trade-offs, or "
            "policy choices that need human judgment.\n"
            "- Be conservative: when uncertain, prefer REJECT over ACCEPT."
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

        # Build set of (file, line) changed in latest commit for fix-detection
        changed_lines = _get_changed_files_lines(auth_h, full_name, pr_number)

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

            # --- Strategy 2: isOutdated fallback (no human replies) ---
            if not human_authors and t.get("isOutdated", False):
                if _resolve_thread(auth_h, thread_id):
                    resolved_count += 1
                continue

            # --- Strategy 3: Reply-aware judging (thread has human replies) ---
            if human_authors:
                # Count how many Argus judgment replies already exist
                argus_judgment_count = sum(
                    1 for c in comments["nodes"]
                    if _is_bot_author(c.get("author", {}).get("login", ""), bot_login)
                    and any(tag in c.get("body", "") for tag in ("✅ Acknowledged", "❓ Follow-up", "⚠️ Escalated")))

                if argus_judgment_count >= MAX_REPLY_ROUNDS:
                    # Too many rounds — escalate silently (don't spam)
                    continue

                # Get original finding (first Argus comment) and latest human reply
                original_finding = ""
                latest_reply = ""
                first_comment_db_id = None
                for c in comments["nodes"]:
                    if _is_bot_author(c.get("author", {}).get("login", ""), bot_login) and not original_finding:
                        original_finding = c.get("body", "")
                        first_comment_db_id = c.get("databaseId")
                    if c.get("author", {}).get("login") in human_authors:
                        latest_reply = c.get("body", "")

                if not original_finding or not latest_reply or not first_comment_db_id:
                    continue

                # Skip if latest reply is itself a judgment tag (loop prevention)
                JUDGMENT_TAGS = ("✅ Acknowledged", "❓ Follow-up", "⚠️ Escalated")
                if any(tag in latest_reply for tag in JUDGMENT_TAGS):
                    continue

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
            from pr_agent.config_loader import get_settings
            token = get_settings().get("github.user_token", "")
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
                    return [], 0
                full_name = repo.full_name if hasattr(repo, 'full_name') else str(repo)
                owner, name = full_name.split("/", 1)

                token = _get_github_token(provider)
                if not token:
                    print("[Argus] No token — skipping thread check")
                    return [], 0

                auth_h = {"Authorization": f"Bearer {token}",
                          "Accept": "application/vnd.github+json"}

                # REST: count past Argus /review reviews only (exclude /improve, /describe)
                argus_review_count = 0
                r = _req.get(f"https://api.github.com/repos/{full_name}/pulls/{pr_number}/reviews",
                             headers=auth_h, timeout=15)
                if r.status_code == 200:
                    argus_review_count = sum(
                        1 for rv in r.json()
                        if rv.get("user", {}).get("login") == BOT_LOGIN
                        and "PR Reviewer Guide" in (rv.get("body") or ""))

                # GraphQL: thread resolution (exclude /improve suggestion threads)
                argus_unresolved = []
                query = '{repository(owner:"%s",name:"%s"){pullRequest(number:%d){reviewThreads(first:100){nodes{isResolved isOutdated comments(first:3){nodes{author{login} body}}}}}}}' % (owner, name, pr_number)
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
                        argus_unresolved.append(t)

                print(f"[Argus] Thread state: {len(argus_unresolved)} unresolved, {argus_review_count} past reviews")
                return argus_unresolved, argus_review_count
            except Exception as e:
                print(f"[Argus] Thread check failed: {e}")
                return [], 0

        def _decide_review_event(findings, unresolved_threads, iteration,
                                 has_inline_comments=False):
            """
            Returns (event, reason):
              "REQUEST_CHANGES" — blocking issues found
              "APPROVE"         — all clear after at least 2 iterations
              "COMMENT"         — non-blocking / first review / escalation

            has_inline_comments: True if this review will post new inline
            comments. Approval is deferred when new comments are posted,
            regardless of severity, because the author hasn't seen them yet.
            """
            critical_major = [f for f in findings
                              if _classify_finding_severity(f.get("issue_header", "")) in BLOCKING_SEVERITIES]

            # Rule 1: Critical/Major findings always block (regardless of iteration)
            if critical_major:
                return ("REQUEST_CHANGES",
                        f"🔴 {len(critical_major)} critical/major issue(s) — changes requested.")

            # Rule 2: Unresolved threads from previous reviews
            if unresolved_threads:
                return ("REQUEST_CHANGES",
                        f"🔴 {len(unresolved_threads)} unresolved thread(s) from previous review.")

            # Rule 3: Max iterations → escalate to human
            if iteration >= MAX_ITERATIONS:
                return ("COMMENT",
                        f"⚠️ Review iteration {iteration}/{MAX_ITERATIONS} reached. "
                        f"Escalating to human reviewer.")

            # Rule 4: First review → COMMENT (never approve on first pass)
            if iteration <= 1:
                return ("COMMENT",
                        "Initial review — no blocking issues. "
                        "Minor findings posted as inline threads.")

            # Rule 5: New inline comments being posted → COMMENT (defer approval)
            # Cannot APPROVE in the same API call that posts new comments,
            # because the author hasn't had a chance to see/address them.
            if has_inline_comments:
                minor_count = len(findings) - len(critical_major)
                return ("COMMENT",
                        f"💬 {minor_count} new finding(s) posted. "
                        f"Approval deferred until next review. "
                        f"Iteration {iteration}/{MAX_ITERATIONS}.")

            # Rule 6: No new comments, no blocking issues, all threads resolved → APPROVE
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
                                })
                            except Exception as e:
                                print(f"[Argus] Could not build inline comment: {e}")
            except Exception as e:
                print(f"[Argus] Failed to parse key_issues: {e}")

            # Get thread state + iteration count (after auto-resolve)
            unresolved_threads, past_reviews = _get_thread_state(self.git_provider, pr_number)
            iteration = past_reviews + 1

            # Decide review event
            event, reason = _decide_review_event(
                findings, unresolved_threads, iteration,
                has_inline_comments=bool(inline_comments))

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
            review_body += f"{len(unresolved_threads)} unresolved threads*"

            # Post unified review
            try:
                self.git_provider.pr.create_review(
                    commit=self.git_provider.last_commit_id,
                    body=review_body,
                    event=event,
                    comments=inline_comments,
                )
                n = len(inline_comments)
                print(f"[Argus] Posted {event} review ({n} inline, iteration {iteration})")
            except Exception as e:
                print(f"[Argus] Unified review failed ({e}), fallback to COMMENT")
                try:
                    self.git_provider.pr.create_review(
                        commit=self.git_provider.last_commit_id,
                        body=review_body,
                        event="COMMENT",
                        comments=inline_comments,
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
