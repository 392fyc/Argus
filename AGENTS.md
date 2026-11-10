<!-- MERCURY:BEGIN managed by tools/mercury.py from the packs in .mercury/config.json; edits here stop the installer, change the pack and run apply -->
## Mercury-managed section

This section, from the begin marker to the end marker, is installed and kept up to date by Mercury
(`python tools/mercury.py apply`) from the packs the project selects in `.mercury/config.json`. Do not edit it by
hand: a hand edit stops the next apply. Everything outside the markers belongs to the project and is never touched.
Paths Mercury manages, and who may change them, are listed in `.mercury/packs/core/docs/mercury-harness-ownership.md`.

## Explanation and reasoning

These rules bind the main agent and every subagent in both Codex and Claude Code, in every kind of task.

- **Explain the Feynman way.** When you explain, report, review, or hand off, write so that someone outside the task can follow it. Use plain words, define each term the reader may not know, build from what the reader already knows, and back each point with a concrete example or the actual evidence. If you cannot explain a point simply, treat it as a gap in your own understanding: return to the source and close the gap, or mark the point as unresolved. Do not cover it with jargon.
- **Reason from first principles in research, design, and investigation.** Start from the goal, the hard constraints, and facts verified in primary sources or by direct observation, and build the conclusion up from them. Treat conventions, analogies, existing implementations, earlier conclusions, and other agents' claims as hypotheses to check, not as premises. State the assumptions a conclusion depends on and label any that remain unverified. Check the assumptions the conclusion actually depends on, in proportion to risk; evidence that stays applicable under the evidence-reuse rules counts as verified fact. User decisions and authorization are constraints, not hypotheses.
- **Carry the rules into delegation.** When you delegate to an agent that does not load these instructions, restate both rules in the delegation prompt. Examples: Claude Code's built-in Explore and Plan agents, agents defined with `omitClaudeMd`, and agents working in another repository or directory that has its own instructions.

## Scope, verification, and completion

Use .mercury/packs/core/docs/mercury-task-contract.md for
bounded scope, test relevance, evidence reuse, provenance-check triggers,
review, and completion. Read its details when needed for these decisions;
it does not add a mandatory workflow or weaken governing requirements.

## Waiting on long-running work

These rules bind the main agent and every subagent in both Codex and Claude Code (Mercury #706). A local Claude Code session puts no time limit on background commands, so a wait whose condition never comes true runs until someone notices.

- **Every wait has a deadline set before it starts.** Wrap a shell wait loop or a follow command such as `tail -f` in `timeout <duration>`, give a PowerShell loop a `Get-Date` or `Stopwatch` deadline, or use a watch the host bounds itself (Claude Code `Monitor` with `timeout_ms`). To learn when a background command finishes, rely on the host's completion notice instead of a second loop that polls its output.
- **End the wait on the watched process's exit and on every failure state**, not on counting success lines. When the deadline passes, report that the work did not finish and what it last showed; do not simply wait again.
- **Delegate with the deadline, and clean up before reporting.** Put the deadline in the delegation prompt. Before you report completion, check the background tasks you and your subagents started and stop any that ran past its deadline or is no longer needed.
- In Claude Code every role Mercury generates runs a `PreToolUse` guard (`.mercury/packs/core/hooks/wait_guard.sh`) that blocks a Bash or PowerShell wait loop or follow command without such a deadline. It is a heuristic check, not a time limit, and it does not cover the main session; details are in the task contract.

## Mercury roles and Git safety rules

- Mercury installs five roles for both hosts: `mercury-dev`, `mercury-reviewer`, `mercury-acceptance`, `mercury-design` and `mercury-research`, as `.claude/agents/mercury-*.md` and `.codex/agents/mercury-*.toml`. They are generated from `.mercury/packs/core/roles/`; change the source in Mercury, not the generated files. A project role of the same name in `.claude/agents/` takes priority over a user-level one, so these project roles apply in this project.
- `mercury-reviewer`, `mercury-acceptance`, `mercury-design` and `mercury-research` are read-only by instruction. In Codex a subagent follows the parent session's current sandbox and approval settings and an agent file cannot change them, so a read-only role there can write whenever the parent can (checked on Codex CLI 0.160.1 with a `workspace-write` parent). The Claude roles run a `PreToolUse` guard (`.mercury/packs/core/hooks/readonly_guard.sh`) that blocks write commands in Bash and PowerShell. The guard is best-effort, not an operating-system sandbox. A project can name more write-type scripts in `params.guard_write_scripts` in `.mercury/config.json`, and scripts that write only for some `-Action` values in `params.guard_write_actions`.
- A project's own roles and skills live in its project area: declared in `.mercury/local.json`, with role sources in `.mercury/local/roles/`. Mercury's installer (`tools/mercury.py apply`, run from a Mercury checkout) generates their `.claude/agents/`, `.codex/agents/` and skill entry files without the `mercury-` prefix; change the source or the manifest, never the generated files. Mercury updates never change the manifest or the sources; they only regenerate the outputs of unchanged sources when Mercury's renderer changes.
- `.codex/rules/mercury-git-safety.rules` forbids raw `git push` and direct switching to `develop`, `main` or `master` in Codex. Perform Git writes through the controlled entry point that this project's own instructions name; never bypass a denial through another shell.

## Controlled Git entry

- Perform Git writes in this project through `powershell -ExecutionPolicy Bypass -File .mercury/packs/git-safe/scripts/git-safe.ps1 <action>`: `add <files>` (explicit files only), `commit -Message "<message>"`, `push <args>`, `sync` (merge `origin/<integration branch>` into the task branch without committing; resolve, stage with `add`, review, conclude with `commit`) and `abort-sync` (abandon that merge). `fast-forward` brings the main checkout, which agents do not work in, up to `origin/<integration branch>`: only on that branch, only a fast-forward, never overwriting local changes. Raw `git push`, `git merge` and similar writes are not the project's route even where a host would allow them.
- Before `commit`, stage the intended files, inspect the staged diff, complete the required checks and review, then run `guard.ps1 mark-review` from the same directory. Changed staged content needs a fresh mark; the mark is a snapshot, not review evidence.
- The scripts act on the Git work tree that holds them, never on the caller's current directory: call the copy inside the task worktree you are working in, and give `add` paths relative to that work tree's root. Task branches are named `<type>/<purpose>[-<lane>]-<agent>` (for example `feature/TASK-7-claude`); `develop`, `main`, `master` and the integration branch are protected. The integration branch for `sync` and `guard.ps1 pre-merge` is `params.integration_branch` in `.mercury/config.json`.
- Pull requests follow `.mercury/packs/git-safe/docs/pr-procedure.md` (Mercury #773): opening one puts the main agent on it through independent review, CI, one Argus review of the final head (`@argus-review review`; Argus reviews only on request), and the merge, which the main agent does itself once every gate holds; subagents never merge. Never resolve review threads yourself, never use `--admin` or auto-merge, and stop to ask the user in the cases that document lists.
<!-- MERCURY:END -->

# Argus — Codex Agent Instructions

The sections below govern Codex self-check runs only (the Identity, MUST and DO NOT rules included). Claude Code sessions follow CLAUDE.md; the Mercury-managed section above applies to both.

## Identity

Agent: Codex CLI
Repo: 392fyc/Argus
Purpose: Self-hosted PR review bot running on QNAP NAS via Docker.
Stack: Python 3 · PR-Agent 0.38 · GitHub App webhook · ASGI (gunicorn)

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
