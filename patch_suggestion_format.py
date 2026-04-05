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

def auto_resolve_outdated_threads(provider, pr_number, bot_login="argus-review[bot]"):
    """Resolve Argus review threads where the underlying code has changed.

    Uses GitHub GraphQL resolveReviewThread mutation.
    Only resolves threads that are:
    - Authored by Argus
    - Marked as outdated (code was modified in subsequent commit)
    - Have no human replies (to preserve discussion)

    Refs:
    - https://docs.github.com/en/graphql/reference/mutations#resolvereviewthread
    - Permission required: Contents: Write
    """
    import requests as _req

    try:
        repo = provider.repo
        if not repo:
            return 0
        full_name = repo.full_name if hasattr(repo, 'full_name') else str(repo)
        owner, name = full_name.split("/", 1)

        # Get token
        token = _get_github_token(provider)
        if not token:
            print("[Argus] No token — skipping auto-resolve")
            return 0

        auth_h = {"Authorization": f"Bearer {token}",
                  "Accept": "application/vnd.github+json"}

        # Query all unresolved threads with isOutdated flag
        # Fetch up to 20 comments per thread to reliably detect human replies
        query = """{
          repository(owner: "%s", name: "%s") {
            pullRequest(number: %d) {
              reviewThreads(first: 100) {
                nodes {
                  id
                  isResolved
                  isOutdated
                  comments(first: 20) {
                    totalCount
                    nodes {
                      author { login }
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

        for t in threads:
            # Only unresolved + outdated threads
            if t["isResolved"] or not t.get("isOutdated", False):
                continue

            # Only Argus-authored threads
            comments = t["comments"]
            authors = [c["author"]["login"]
                       for c in comments["nodes"]
                       if c.get("author")]
            if bot_login not in authors:
                continue

            # Skip threads with human replies (preserve discussion)
            # Also skip if we couldn't fetch all comments (totalCount > fetched)
            human_authors = [a for a in authors if a != bot_login]
            if human_authors:
                continue
            if comments.get("totalCount", 0) > len(comments["nodes"]):
                continue

            # Resolve via GraphQL mutation
            thread_id = t["id"]
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
                    resolved_count += 1
                elif data.get("errors"):
                    print(f"[Argus] Resolve failed for {thread_id}: {data['errors']}")
            else:
                print(f"[Argus] Resolve request failed: {m.status_code}")

        if resolved_count:
            print(f"[Argus] Auto-resolved {resolved_count} outdated thread(s)")
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
                        if BOT_LOGIN not in authors or t["isResolved"]:
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

        def _decide_review_event(findings, unresolved_threads, iteration):
            """
            Returns (event, reason):
              "REQUEST_CHANGES" — blocking issues found
              "APPROVE"         — all clear after at least 2 iterations
              "COMMENT"         — non-blocking / first review / escalation
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

            # Rule 5: Subsequent review, no blocking issues, all threads resolved → APPROVE
            # Minor-only findings don't block approval on iteration >= 2
            minor_count = len(findings) - len(critical_major)
            suffix = f" ({minor_count} minor findings noted.)" if minor_count else ""
            return ("APPROVE",
                    f"✅ No critical/major issues, all threads resolved.{suffix} "
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
            event, reason = _decide_review_event(findings, unresolved_threads, iteration)

            # Build enhanced review body (CodeRabbit-style)
            body_additions = build_review_body_additions(
                findings, len(inline_comments), diff_files)
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
