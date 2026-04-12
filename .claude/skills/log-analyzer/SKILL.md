# Log Analyzer Skill

Manual invocation of the Argus self-check analysis via Claude Code session.

**This skill is for manual/ad-hoc use.** For scheduled runs, see the deployment
options in `docs/self-check-setup.md`:
- **Plan A (primary)**: GitHub Actions + `openai/codex-action@v1` (`.github/workflows/self-check.yml`)
- **Plan B (fallback)**: Claude Code + ccproxy + LiteLLM (`scripts/run_self_check.sh`)

## When to invoke manually

- You suspect Argus has been misbehaving since the last scheduled run
- You want to preview findings before the next automatic run
- You need to test the self-check pipeline after code changes

## Prerequisites

Ensure `ARGUS_EVENTS_PATH` points to the events.jsonl sink with recent data.

## Execution steps

1. **Preview findings (dry run)**:
   ```bash
   python argus_self_check.py --days 3 --dry-run
   ```

2. **Review output** — confirm findings are real, not stale test data

3. **File Issues (production run)**:
   ```bash
   python argus_self_check.py --days 3 --max-issues 5
   ```

4. **Verify** filed Issues at https://github.com/392fyc/Argus/issues?q=label:source:self-check

## What the analyzer checks

| Problem type | Trigger condition |
|---|---|
| `error_spike` | ≥3 ERROR events in window |
| `escalate_overrate` | ≥30% ESCALATE among ≥3 reply events (LLM errors excluded) |
| `resolution_gap` | ≥3 ACCEPT verdicts without matching THREAD_RESOLVED |
| `access_pattern` | ≥5 REQUEST_BLOCKED events |
| `data_gap` | Zero events in window (sensor health check) |

## Safeguards

- Only files Issues to `392fyc/Argus`
- Max 5 Issues per run (configurable via `--max-issues`)
- Deduplicates: will not re-file if open Issue with same `sig:` already exists
- Never modifies source files, configs, or deploy files
- All hypotheses in filed Issues are marked `UNVERIFIED`
