"""
Argus — @mention → PR-Agent slash-command rewriting (pure, unit-testable).

Extracted from entrypoint-guard.py so the intent-classification logic can be
unit-tested without importing pr_agent (Argus #23).

Intent is detected from the verb that FOLLOWS an @argus-review mention, and the
LAST explicit-command mention wins — the trailing-trigger convention: users
write a fix summary as context, then "@argus-review review" on the final line
to re-trigger an incremental review. Body text preceding the trailing trigger
is context, NOT a question. This fixes the bug where a findings-list body was
misclassified as a question and routed to /ask, so the intended /review never
fired and the PR stalled in a nit-loop (Argus #23 / Mercury PR #253).
"""

import re

# Matches @argus-review or @argus-review[bot], plus any trailing whitespace.
BOT_MENTION_RE = re.compile(r'@argus-review(?:\[bot\])?\s*', re.IGNORECASE)

# A line beginning with '>' is a Markdown quote — a mention there is not a trigger.
QUOTE_PREFIX_RE = re.compile(r'^\s*>')

PR_AGENT_COMMANDS = {
    "review", "describe", "improve", "ask", "help",
    "update_changelog", "similar_issue", "add_docs", "test",
}


def should_rewrite_mention(body: str) -> bool:
    """True if body contains a direct (non-quoted) @argus-review mention."""
    if not body or not BOT_MENTION_RE.search(body):
        return False
    # Don't rewrite if the mention appears only inside quoted lines.
    for line in body.split('\n'):
        if BOT_MENTION_RE.search(line) and not QUOTE_PREFIX_RE.match(line):
            return True
    return False


def _line_containing(body: str, pos: int) -> str:
    """Return the full text of the line that contains character index `pos`."""
    start = body.rfind('\n', 0, pos) + 1
    end = body.find('\n', pos)
    if end == -1:
        end = len(body)
    return body[start:end]


def rewrite_mention(body: str):
    """Rewrite @argus-review mentions to a PR-Agent slash command.

    Returns ``(command, method)`` where ``method`` records the decision so the
    rewrite is debuggable from events.jsonl (Argus #23 item 3):

      - ``explicit-trailing-verb``: a known command verb (review/ask/...) directly
        followed a non-quoted mention; the LAST such mention wins (trailing
        trigger). Preceding body is treated as context, not a question.
      - ``slash-passthrough``: the body (minus mentions) already starts with a
        slash command.
      - ``freeform-ask-fallback``: no explicit command verb → treat the whole
        body as a question for /ask.
      - ``empty``: nothing remained after stripping mentions.

    Examples::

        "@argus-review review"                 -> ("/review", explicit-trailing-verb)
        "@argus-review review -i"              -> ("/review -i", explicit-trailing-verb)
        "<fix summary>\\n@argus-review review"  -> ("/review", explicit-trailing-verb)
        "@argus-review why is X bad?"          -> ("/ask why is X bad?", freeform-ask-fallback)
    """
    if not body:
        return ("", "empty")
    explicit = None  # (verb, args) from the LAST non-quoted command mention
    for m in BOT_MENTION_RE.finditer(body):
        if QUOTE_PREFIX_RE.match(_line_containing(body, m.start())):
            continue  # mention inside a quote is context, not a trigger
        rest_of_line = body[m.end():].split('\n', 1)[0].strip()
        if not rest_of_line:
            continue
        first = rest_of_line.split()[0]
        verb = first.lstrip('/').lower()
        if verb in PR_AGENT_COMMANDS:
            args = rest_of_line[len(first):].strip()
            explicit = (verb, args)

    if explicit:
        verb, args = explicit
        return (f"/{verb} {args}".rstrip(), "explicit-trailing-verb")

    cleaned = BOT_MENTION_RE.sub("", body).strip()
    if not cleaned:
        return ("", "empty")
    if cleaned.startswith("/"):
        return (cleaned, "slash-passthrough")
    return (f"/ask {cleaned}", "freeform-ask-fallback")
