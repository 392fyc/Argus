"""
Argus — Patches PR-Agent output to CodeRabbit-style structured format.

Review body: summary + aggregated 🤖 Prompt for all comments
Inline threads: severity badge, description, suggestion, committable, agent prompt

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
        original_run = pr_reviewer.PRReviewer.run

        async def patched_run(self):
            """Run original /review, then post unified GitHub Review."""
            # Clear capture state
            self.git_provider._argus_review_body = None

            # Run original — it generates prediction, captures body via patched publish
            result = await original_run(self)

            review_body = getattr(self.git_provider, '_argus_review_body', None)
            if not review_body:
                # Body wasn't captured (maybe no issues found), nothing to do
                return result

            # Build inline comments from key_issues
            inline_comments = []
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
                        issues = data['review'].get('key_issues_to_review', [])
                        diff_files = self.git_provider.diff_files or self.git_provider.get_diff_files()

                        for issue in issues:
                            try:
                                filepath = issue.get('relevant_file', '').strip()
                                end_line = int(issue.get('end_line', 0))
                                if not filepath or not end_line:
                                    continue
                                position, _ = find_line_number_of_relevant_line_in_file(
                                    diff_files, filepath.strip('`'), "", end_line)
                                if position == -1:
                                    continue
                                body = format_review_finding_body(issue)
                                inline_comments.append({
                                    'body': body,
                                    'path': filepath.strip(),
                                    'position': position,
                                })
                            except Exception as e:
                                print(f"[Argus] Could not build inline comment: {e}")
            except Exception as e:
                print(f"[Argus] Failed to parse key_issues: {e}")

            # Post unified review: body + inline comments in ONE create_review call
            try:
                self.git_provider.pr.create_review(
                    commit=self.git_provider.last_commit_id,
                    body=review_body,
                    event="COMMENT",
                    comments=inline_comments,
                )
                n = len(inline_comments)
                print(f"[Argus] Posted unified review ({len(review_body)} chars, {n} inline threads)")
            except Exception as e:
                print(f"[Argus] Unified review failed ({e}), posting body only")
                try:
                    self.git_provider.pr.create_review(
                        commit=self.git_provider.last_commit_id,
                        body=review_body,
                        event="COMMENT",
                    )
                    # Post inline separately as fallback
                    if inline_comments:
                        self.git_provider.pr.create_review(
                            commit=self.git_provider.last_commit_id,
                            comments=inline_comments,
                        )
                except Exception as e2:
                    print(f"[Argus] All review posting failed ({e2}), using original publish")
                    original_publish_comment(self.git_provider, review_body)

            # Edit the placeholder comment to point to the review
            try:
                if hasattr(self.git_provider, 'pr') and hasattr(self.git_provider.pr, 'comments_list'):
                    for c in getattr(self.git_provider.pr, 'comments_list', []):
                        if getattr(c, 'is_temporary', False):
                            n = len(inline_comments)
                            c.edit(f"✅ Review complete — **{n} findings** posted as inline threads above.")
                            break
            except Exception:
                try:
                    self.git_provider.remove_initial_comment()
                except Exception:
                    pass

            return result

        pr_reviewer.PRReviewer.run = patched_run
        print("[Argus] /review unified (body + inline) patched")
    except Exception as e:
        print(f"[Argus] Failed to patch /review: {e}")
