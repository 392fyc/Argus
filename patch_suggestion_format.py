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
    """Build a prompt that AI coding agents can directly use."""
    content = d.get("suggestion_content", "").rstrip()
    label = d.get("label", "").strip()
    improved = d.get("improved_code", "").rstrip()

    lines = [
        f"In file {filepath}",
    ]
    if start and end and start != end:
        lines.append(f"at lines {start}-{end}:")
    elif start:
        lines.append(f"at line {start}:")
    else:
        lines.append(":")

    lines.append("")
    lines.append(f"Issue: {content}")
    lines.append(f"Category: {label}")
    lines.append("")

    if improved:
        lines.append("Replace the existing code with:")
        lines.append(improved)
    else:
        lines.append(f"Fix: {content}")

    return "\n".join(lines)


def apply_patch():
    """Monkey-patch PR-Agent's suggestion rendering."""
    try:
        from pr_agent.tools import pr_code_suggestions

        original_push = pr_code_suggestions.PRCodeSuggestions.push_inline_code_suggestions

        def patched_push(self, data):
            """Patched version that uses CodeRabbit-style formatting."""
            import copy
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
        print("[Argus Patch] Suggestion format patched to CodeRabbit style")
    except Exception as e:
        print(f"[Argus Patch] Failed to apply patch: {e} — using default format")
