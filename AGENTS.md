# Argus — Codex Agent Instructions

## Identity

Agent: Codex CLI
Repo: 392fyc/Argus
Purpose: Self-hosted PR review bot running on QNAP NAS via Docker.
Stack: Python 3 · PR-Agent 0.34 · GitHub App webhook · ASGI (gunicorn)

## Navigation

| Topic | Path |
|-------|------|
| Structured event schema | `argus_events.py` |
| Event extractor | `argus_extractor.py` |
| Self-check analyzer | `argus_log_analyzer.py` |
| Self-check formatter | `argus_issue_formatter.py` |
| Self-check orchestrator | `argus_self_check.py` |
| Self-check setup docs | `docs/self-check-setup.md` |
| Webhook guard + @mention patch | `entrypoint-guard.py` |
| CodeRabbit-style suggestion format | `patch_suggestion_format.py` |
| ADR: code-reading capability | `docs/adr/0001-code-reading-capability.md` |

## Self-check task (M3)

When asked to "run the Argus self-check" or "analyze recent logs":

1. Run `python argus_self_check.py --days 3` in the working directory
2. Print all output verbatim
3. Report the number of Issues filed and their URLs
4. Do NOT modify any source files, configuration files, or deploy files
5. Do NOT file Issues to any repo other than `392fyc/Argus`
6. Do NOT exceed 5 Issues per run

For preview without filing: `python argus_self_check.py --days 3 --dry-run`

## MUST

- Never modify `entrypoint-guard.py`, `patch_suggestion_format.py`, or any gunicorn/Docker config
- Never push code or create PRs — analysis and issue filing only
- Always print full `argus_self_check.py` output before summarizing
- If `ARGUS_EVENTS_PATH` is not set and `/var/log/argus/events.jsonl` does not exist, report the missing path and stop

## DO NOT

- Do not modify Argus source code in self-check runs
- Do not create or delete files outside the analysis pipeline
- Do not access external URLs other than GitHub API (via `gh` CLI)
