"""
Argus — Patch PR-Agent inline suggestion format to CodeRabbit style.

Replaces the default single-line suggestion format with a structured format:
- Severity badge + label
- Problem description
- Collapsible sections: Suggestion, Committable suggestion, Prompt for AI Agents

Applied at import time by the entrypoint-guard module.
"""

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


def format_suggestion_body(d: dict, new_code_snippet: str) -> str:
    """Format a single code suggestion in CodeRabbit-like style."""
    content = d.get("suggestion_content", "").rstrip()
    label = d.get("label", "general").strip().lower()
    score = d.get("score")
    existing_code = d.get("existing_code", "").rstrip()
    relevant_file = d.get("relevant_file", "").strip()
    start_line = d.get("relevant_lines_start", "")
    end_line = d.get("relevant_lines_end", "")

    # Severity badge
    icon, severity = SEVERITY_MAP.get(label, ("🟡", "Medium"))
    score_text = f" | importance: {score}/10" if score else ""

    # Line range
    if start_line and end_line and start_line != end_line:
        line_ref = f"**Comment on lines {start_line}-{end_line}**"
    elif start_line:
        line_ref = f"**Comment on line {start_line}**"
    else:
        line_ref = ""

    # Build the body
    parts = []

    # Header: severity + label
    parts.append(f"{icon} **{severity}** | _{label}_{score_text}")
    if line_ref:
        parts.append(line_ref)
    parts.append("")

    # Problem description
    parts.append(content)
    parts.append("")

    # Collapsible: Suggestion details
    parts.append("<details><summary>💡 Suggestion</summary>")
    parts.append("")
    parts.append(content)
    if existing_code:
        parts.append("")
        parts.append("**Current code:**")
        # Detect language from file extension
        lang = _detect_lang(relevant_file)
        parts.append(f"```{lang}")
        parts.append(existing_code)
        parts.append("```")
    parts.append("")
    parts.append("</details>")
    parts.append("")

    # Committable suggestion (GitHub's native suggestion block)
    if new_code_snippet:
        parts.append("<details><summary>🔧 Committable suggestion</summary>")
        parts.append("")
        parts.append("```suggestion")
        parts.append(new_code_snippet)
        parts.append("```")
        parts.append("")
        parts.append("</details>")
        parts.append("")

    # Prompt for AI Agents
    agent_prompt = _build_agent_prompt(d, relevant_file, start_line, end_line)
    parts.append("<details><summary>🤖 Prompt for AI Agents</summary>")
    parts.append("")
    parts.append("```text")
    parts.append(agent_prompt)
    parts.append("```")
    parts.append("")
    parts.append("</details>")

    return "\n".join(parts)


def _detect_lang(filepath: str) -> str:
    """Detect language from file extension for code blocks."""
    ext_map = {
        ".ts": "typescript", ".tsx": "typescript",
        ".js": "javascript", ".jsx": "javascript",
        ".py": "python",
        ".sh": "bash", ".bash": "bash",
        ".yaml": "yaml", ".yml": "yaml",
        ".toml": "toml",
        ".json": "json",
        ".md": "markdown",
        ".css": "css", ".scss": "scss",
        ".html": "html",
        ".rs": "rust",
        ".go": "go",
    }
    for ext, lang in ext_map.items():
        if filepath.endswith(ext):
            return lang
    return ""


def _build_agent_prompt(d: dict, filepath: str, start: str, end: str) -> str:
    """Build an English prompt that AI coding agents can directly use."""
    content = d.get("suggestion_content", "").rstrip()
    label = d.get("label", "").strip()
    improved = d.get("improved_code", "").rstrip()
    existing = d.get("existing_code", "").rstrip()
    summary = d.get("one_sentence_summary", "").rstrip()

    # Use one_sentence_summary (typically shorter/English-ish) or label as fallback
    issue_title = summary if summary else label

    lines = [
        f"In file `{filepath}`",
    ]
    if start and end and start != end:
        lines.append(f"around lines {start}-{end}:")
    elif start:
        lines.append(f"around line {start}:")

    lines.append("")
    lines.append(f"[{label}] {issue_title}")
    lines.append("")
    lines.append(f"Description: {content}")
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


def format_review_finding_body(issue: dict) -> str:
    """Format a /review key_issue as an inline thread comment."""
    header = issue.get("issue_header", "Issue").strip()
    content = issue.get("issue_content", "").strip()
    filepath = issue.get("relevant_file", "").strip()
    start = issue.get("start_line", "")
    end = issue.get("end_line", "")

    # Map header to severity
    header_lower = header.lower()
    if "bug" in header_lower or "critical" in header_lower or "security" in header_lower:
        icon, severity = "🔴", "Critical"
    elif "possible" in header_lower or "error" in header_lower:
        icon, severity = "🟠", "Major"
    elif "performance" in header_lower or "issue" in header_lower:
        icon, severity = "🟡", "Medium"
    else:
        icon, severity = "🔵", "Minor"

    parts = []
    parts.append(f"{icon} **{severity}** | _{header}_")
    if start and end and str(start) != str(end):
        parts.append(f"**Comment on lines {start}-{end}**")
    elif start:
        parts.append(f"**Comment on line {start}**")
    parts.append("")
    parts.append(content)
    parts.append("")

    # Prompt for AI Agents
    agent_lines = [
        f"In file `{filepath}`",
    ]
    if start and end and str(start) != str(end):
        agent_lines.append(f"around lines {start}-{end}:")
    elif start:
        agent_lines.append(f"around line {start}:")
    agent_lines.append("")
    agent_lines.append(f"[{header}] {content}")
    agent_lines.append("")
    agent_lines.append(f"Action required: Investigate and fix the {header.lower()} described above.")

    parts.append("<details><summary>🤖 Prompt for AI Agents</summary>")
    parts.append("")
    parts.append("```text")
    parts.extend(agent_lines)
    parts.append("```")
    parts.append("")
    parts.append("</details>")

    return "\n".join(parts)


def apply_patch():
    """Monkey-patch PR-Agent's suggestion and review rendering."""

    # ── Patch 1: /improve inline suggestions (CodeRabbit style) ──
    try:
        from pr_agent.tools import pr_code_suggestions

        original_push = pr_code_suggestions.PRCodeSuggestions.push_inline_code_suggestions

        def patched_push(self, data):
            """Patched version that uses CodeRabbit-style formatting."""
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
                            relevant_file, relevant_lines_start, new_code_snippet
                        )

                    body = format_suggestion_body(d, new_code_snippet)

                    code_suggestions.append({
                        "body": body,
                        "relevant_file": relevant_file,
                        "relevant_lines_start": relevant_lines_start,
                        "relevant_lines_end": relevant_lines_end,
                        "original_suggestion": d,
                    })
                except Exception as e:
                    print(f"[Argus Patch] Could not format suggestion: {e}")

            is_successful = self.git_provider.publish_code_suggestions(code_suggestions)
            if not is_successful:
                for cs in code_suggestions:
                    self.git_provider.publish_code_suggestions([cs])

        pr_code_suggestions.PRCodeSuggestions.push_inline_code_suggestions = patched_push
        print("[Argus Patch] /improve suggestion format patched")
    except Exception as e:
        print(f"[Argus Patch] Failed to patch /improve: {e}")

    # ── Patch 2: /review key_issues as inline threads ────────────
    try:
        from pr_agent.tools import pr_reviewer

        original_run = pr_reviewer.PRReviewer.run

        async def patched_run(self):
            """Run original /review, then post key_issues as inline threads."""
            # Run the original review (posts summary comment)
            result = await original_run(self)

            # After summary is posted, also post inline threads for key issues
            try:
                from pr_agent.algo.utils import load_yaml
                if hasattr(self, 'prediction') and self.prediction:
                    data = load_yaml(self.prediction.strip(),
                                     keys_fix_yaml=["key_issues_to_review:",
                                                     "relevant_file:", "relevant_line:", "suggestion:"],
                                     first_key="review", last_key="key_issues_to_review")
                    if data and 'review' in data:
                        issues = data['review'].get('key_issues_to_review', [])
                        if issues:
                            code_comments = []
                            for issue in issues:
                                try:
                                    filepath = issue.get('relevant_file', '').strip()
                                    start_line = int(issue.get('start_line', 0))
                                    end_line = int(issue.get('end_line', 0))
                                    if not filepath or not start_line:
                                        continue

                                    body = format_review_finding_body(issue)
                                    code_comments.append({
                                        'body': body,
                                        'relevant_file': filepath,
                                        'relevant_lines_start': start_line,
                                        'relevant_lines_end': end_line,
                                    })
                                except Exception as e:
                                    print(f"[Argus Patch] Could not format review finding: {e}")

                            if code_comments:
                                self.git_provider.publish_code_suggestions(code_comments)
                                print(f"[Argus Patch] Posted {len(code_comments)} review inline threads")
            except Exception as e:
                print(f"[Argus Patch] Failed to post review inline threads: {e}")

            return result

        pr_reviewer.PRReviewer.run = patched_run
        print("[Argus Patch] /review inline threads patched")
    except Exception as e:
        print(f"[Argus Patch] Failed to patch /review: {e}")
