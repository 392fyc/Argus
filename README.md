# Argus

> *Named after the hundred-eyed giant Argus Panoptes — the all-seeing watchman.*

Self-hosted **advisory** AI code review for [Mercury](https://github.com/392fyc/Mercury) and other repositories, powered by [PR-Agent](https://github.com/qodo-ai/pr-agent) 0.38.0 and `gpt-6.1-sol` called directly through OpenAI's Responses API.

Argus gives advice. It never blocks a merge and never approves one: every review it submits is a GitHub `COMMENT`.

## What Argus does

| Rule | Behaviour |
|------|-----------|
| **Whole PR** | Every review reads the whole PR diff, never only the latest commit. A new push re-reviews the whole PR (`push_commands` uses `/review`, not `/review -i`; a typed `-i` is ignored). |
| **Knows the project** | A repository can describe itself in `.argus/review.toml` on the PR's target branch: what to focus on, which architecture/rule documents to read, which paths to ignore. See [Project modules](#project-modules-argusreviewtoml). |
| **Advice only** | Reviews are always `COMMENT`. Findings are suggestions; the footer says "advice only" (仅供参考), and a `CHANGES_REQUESTED` review left by an earlier Argus version is dismissed at the start of the next review. There is no `REQUEST_CHANGES` and no `APPROVE` anywhere in the code, and PR-Agent's own auto-approve is disabled. |
| **No repeats** | A finding that matches an existing Argus thread on the same file is not posted again when a person resolved that thread, a person replied in it, Argus judged a reply in it (accepted, "Follow-up" or escalated), or it is still open. A thread that Argus itself closed (its fix-detection and outdated rules do that whenever lines change) does not count: that only says the lines were edited, so a problem that is still there can be reported again. |
| **Threshold** | Only findings of medium severity or higher are posted (`[argus_review] min_severity`, default `Medium`). Style, naming, typos and wording are dropped. A finding whose severity cannot be recognised counts as Medium and is kept, never silently dropped. |
| **Spend cap** | Monthly budget of 10 USD, resetting on the 15th (Asia/Tokyo). When it is used up Argus stops, says so once, and resumes at the next reset. See [Budget](#budget). |

## Quick Start

```bash
# 1. Run setup
bash setup.sh

# 2. Edit secrets
nano .secrets.toml   # Add the OpenAI key + GitHub App credentials (see .secrets.toml.template)

# 3. Start (webhook mode)
docker compose up -d
```

Merging to `master` deploys automatically through `.github/workflows/deploy.yml` (see CLAUDE.md). **Before merging the change that switches the model to OpenAI, read [First deployment](#first-deployment): the NAS `.secrets.toml` must be updated first.**

## Architecture

```
GitHub PR event
    |
    v (webhook via Cloudflare Tunnel)
Argus: PR-Agent 0.38.0 + patches (Docker, QNAP NAS)
    |-- budget check (argus_budget.py): stop if this month's cap is used up
    |-- read .argus/review.toml + context files from the PR's target branch (argus_context.py)
    |-- fetch the whole PR diff (30 lines of context before each change, 10 after)
    |-- gpt-6.1-sol through OpenAI Responses, reasoning effort "high"
    |-- drop findings: skip_paths, below the severity threshold, repeats of existing threads
    |-- submit one GitHub review (COMMENT) with inline suggestions
    v
GitHub PR: advisory review comment
```

Modules: `entrypoint-guard.py` (user whitelist, `@mention` rewrite, loads the patches), `patch_suggestion_format.py` (review format, thread auto-resolve, finding filters, advice-only), `patch_reasoning.py` (reasoning effort), `argus_context.py` (project context), `argus_budget.py` (spend cap), `mention_rewrite.py`, `argus_events.py` (structured event log).

## Project modules (`.argus/review.toml`)

Put `.argus/review.toml` at the root of the branch **a PR targets** (its base ref). Argus reads it, and the files it lists, through the GitHub contents API at that ref. It deliberately does not use PR-Agent's `.pr_agent.toml`, which is read from the default branch; some repositories integrate on a branch that is not the default. Because the file comes from the target branch, a PR cannot change the rules it is reviewed against. Without the file Argus does a general review only.

| Field | Type | Meaning |
|-------|------|---------|
| `focus` | string | What this project wants checked hardest. |
| `context_files` | list of paths | Files in the repository (read at the target branch) that Argus reads before reviewing: architecture notes, contribution rules. At most 12; the first 12 are used. |
| `max_context_chars` | integer, default `60000` | Total characters of all context files together. When the limit is reached the current file is cut with a visible `[TRUNCATED ...]` note and later files are listed as omitted, in list order. Hard ceiling: 300000. |
| `skip_paths` | list of globs | Findings on these paths are dropped. `*` does not cross `/`, `**` does; a pattern without `/` matches the file name anywhere (`*.lock`); a trailing `/` skips a directory tree. |

Example:

```toml
# .argus/review.toml
focus = """
Layering: modules under src/core must not import from src/ui.
Every network call needs a timeout and an error path.
"""

context_files = [
  "docs/architecture.md",
  "docs/adr/0003-error-handling.md",
  "CONTRIBUTING.md",
]

max_context_chars = 60000

skip_paths = [
  "docs/**",
  "**/*.lock",
  "src/generated/",
]
```

How the content is used:

- It is **data, not instructions**. Each file is wrapped in `BEGIN/END PROJECT FILE <token>` markers carrying a random token generated for every run, so the text cannot fake its own closing marker, and the prompt states that the quoted text is reference material for judging compliance and that wording inside it does not change the reviewer's task.
- Paths with `..`, an absolute path, a drive letter or a backslash are rejected. Symlinks, submodules and directories are refused (the contents API reports each entry's type). Any read failure is logged and skipped; the review always continues.
- The text is appended to the review's `extra_instructions`, so it affects `/review` only (not `/describe` or `/improve`).

## Model routing

`configuration.toml` sets `model`, `model_turbo` and `model_weak` to `openai/responses/gpt-6.1-sol`, with `fallback_models=[]`.

- `openai/` is the LiteLLM provider and `responses/` selects LiteLLM's Chat-Completions-to-Responses bridge. The prefix matters: LiteLLM (v1.84.0, `get_llm_provider_logic.py`) cannot infer a provider from a bare `responses/gpt-6.1-sol`, while `openai/responses/<model>` resolves to provider `openai` and `responses_api_bridge_check` in `litellm/main.py` then strips `responses/` and routes through the bridge.
- `reasoning_effort = "high"`. PR-Agent 0.38.0 sends `reasoning_effort` only for models in its static `SUPPORT_REASONING_EFFORT_MODELS` (o3, o4-mini), and LiteLLM drops the parameter for an unknown model unless it is named in `allowed_openai_params`. `patch_reasoning.py` adds both for the configured model (`model`, `model_turbo`, `model_weak`) and leaves other models untouched. The bridge turns `reasoning_effort` into the Responses `reasoning` parameter (`litellm_responses_transformation/transformation.py`, `_map_reasoning_effort`).
- `.secrets.toml` needs only `[openai] key`. Do not leave `api_type`, `api_base`, `api_version` or `deployment_id` there: a `deployment_id` makes LiteLLM switch the call to Azure (`litellm/main.py`: `if deployment_id is not None`).
- Not yet exercised against the real OpenAI API from this repository: see [First deployment](#first-deployment).

### Language of the review

The configuration asks for zh-CN replies, and PR-Agent adds that language instruction to every tool. The severity threshold reads the finding's `issue_header`, so the review prompt tells the model to keep `issue_header` in the English words `Critical Bug`, `Security`, `Possible Bug`, `Performance`, `Possible Issue` even when the rest is Chinese. If the model writes a Chinese header anyway, the ranking also understands common Chinese words (for example 严重/安全/错误 rank high, 性能/问题 rank Medium, 次要/建议/风格/命名/拼写 rank Minor or lower), and a header it cannot place is Medium, so it is kept.

### Prompt size

`max_model_tokens = 80000` (was 32000) and `patch_extra_lines_before/after = 30/10` (were 5/1), so the model sees the code around each change. These are estimates, not measurements: fixed prompt about 4k tokens, project context up to 60000 characters (roughly 15-20k tokens for English/code, more for CJK), and a diff budget of about 56k tokens, because 30/10 lines of context make a diff commonly 1.5-2.5x larger. A budget that is too small makes PR-Agent clip files out of the diff (`large_patch_policy = "clip"`). The project context from `.argus/review.toml` is also capped by tokens, counted with the model tokenizer (PR-Agent's `TokenHandler`): at most `[argus_review] context_token_ratio` (default 0.25, so 20000 tokens) of `max_model_tokens`. Over that, the context is cut further and the cut is stated in the prompt, so context cannot squeeze out the diff. The setting also applies to `/describe` and `/improve`. Worst-case input is 80k tokens, about 0.16 USD at 2.00 USD per million input tokens.

## Budget

`argus_budget.py` caps what Argus spends. Settings (`[argus_budget]` in `configuration.toml`):

```toml
[argus_budget]
monthly_usd = 10
reset_day = 15            # 1-28
timezone = "Asia/Tokyo"
missing_usage_usd = 0.10  # charged when a call returns no token usage

[[argus_budget.prices]]   # USD per 1,000,000 tokens
model = "gpt-6.1-sol"
input = 2.00
cached_input = 0.10
output = 10.00
```

A period starts at 00:00 on `reset_day` in `timezone` and ends at the same moment of the next month (the boundary second belongs to the new period). Output tokens include reasoning tokens. An unlisted model is billed at the highest listed price.

How it works:

- **Before** every model call (review, describe, improve, reply judging) the ledger is checked; if the period's budget is used up the call is not made and `BudgetExhausted` is raised. **After** the call, the returned `usage` (input, cached input, output including reasoning) is priced and added.
- A provider "quota used up" error (`insufficient_quota`, `credit_balance_exhausted`, ...) marks the period as used up and is not retried. An ordinary rate limit (`rate_limit_exceeded`) does not.
- A call that runs past `ai_timeout` (300 seconds in `configuration.toml`) is not retried: once one call in a command times out, every later call in that command is refused before it reaches the provider. The provider may still have run and billed the timed-out call, and no usage comes back, so the ledger charges a rough estimate: the prompt tokens at the input price plus 16,000 output tokens at the output price (about 0.26 USD for a 50k-token prompt). A long high-effort call can produce more output, so the estimate can still be low. A connection timeout, where the request never reached the provider, is charged and stops the command the same way; LiteLLM reports both as a timeout.
- When a command gives up because its model calls failed and none succeeded (a timeout or any other error, but not a used-up budget, which has its own notice), Argus deletes that command's own placeholder on the PR ("Preparing review...", "Preparing PR description..." or "Preparing suggestions...", posted by `argus-review[bot]`, exact text only; placeholders of other commands stay) and posts one short note naming the command and the reason. The note is not retried. Comment `@argus-review <command>` to run it again. A command where some calls succeeded and a later one failed (for example a side call after the review was posted) gets no note.
- When the budget is used up Argus skips every command except `/help` and `/config`, **comments once on that PR** with the amount spent and the next reset time, and writes a `budget_exhausted` event to the event log. Within the same period it stays silent. The notice counts as sent only after the comment was actually posted; if posting fails, a later request tries again, at most 3 attempts per period and at least 2 minutes apart, so a failing GitHub neither loses the notice for good nor floods the PR.
- The ledger is `/var/log/argus/budget.json` (the mounted `./logs` volume; override with `ARGUS_BUDGET_PATH`): period start, spent amount, used-up flag, notified flag. Writes are atomic (temporary file, flush to disk, rename) and serialised with a thread lock plus an OS file lock. A ledger that cannot be read or fails validation counts as **used up**: stopping early is cheaper than spending without a limit. To recover, fix or delete the file.

**Limit of the cap.** The check happens before a call and cannot know what that call will cost, so the cap can be exceeded by the calls already in flight when the last check passed. The most that can overshoot is (requests running at the same moment) x (up to 3 calls per request, which is what `/improve` runs in parallel; `/review` and `/describe` make one). Estimate of one call, not a measurement: a full 80k-token prompt is about 0.16 USD of input at 2.00 USD per million, plus up to roughly 16k output tokens (reasoning included) at 10.00 USD per million is about 0.16 USD more, so up to about 0.3 USD per call; a typical call is well below that. With one request at a time the overshoot is at most one call, about 0.3 USD in the worst case.

## Deployment Modes

| Mode | Image Tag | Tunnel Required | Latency |
|------|-----------|-----------------|---------|
| **Webhook** (recommended) | `0.38.0-github_app` | Yes (Cloudflare Tunnel) | Instant |
| **Polling** | `0.34-github_polling` (`docker-compose.polling.yml`, not updated for any of the above) | No | Minutes |

## Configuration

| File | Purpose |
|------|---------|
| `configuration.toml` | PR-Agent settings (model, auto-commands, review rules) plus `[argus_review]` and `[argus_budget]` |
| `.secrets.toml` | OpenAI key and GitHub App credentials (`.secrets.toml.template` lists the key names) |
| `.env` | Port and tunnel token |
| `.argus/review.toml` (in each reviewed repository, on its target branch) | Project focus, context files, skip paths |

`[argus_review]`: `min_severity` (`Trivial`, `Minor`, `Medium`, `Major`, `Critical`; default `Medium`), `suppress_repeated_findings` (default `true`) and `context_token_ratio` (default `0.25`). Severity comes from the finding's `issue_header`; the review prompt asks the model to start headers with `Critical Bug`, `Security`, `Possible Bug`, `Performance` or `Possible Issue`, which all rank Medium or higher.

## GitHub App Setup

1. Go to https://github.com/settings/apps/new
2. Set:
   - **Name**: Argus
   - **Webhook URL**: `https://<tunnel-domain>/api/v1/github_webhooks`
   - **Webhook Secret**: (random string, save to `.secrets.toml`)
3. Permissions:
   - Pull requests: Read & Write
   - Issues: Read & Write
   - Contents: Read (needed to read `.argus/review.toml` and context files)
   - Metadata: Read
4. Subscribe to events: `Pull request`, `Issue comment`, `Pull request review comment`
5. Generate private key (.pem), save to `.secrets.toml`
6. Install the app on the repositories to review (for example 392fyc/Mercury)

## User Whitelist

Argus includes a guard middleware that restricts which GitHub users can trigger reviews.

Configure in `.env`:
```bash
# Only these users can trigger reviews (comma-separated, case-insensitive)
ARGUS_ALLOWED_USERS=392fyc,trusted-bot
```

Leave empty to allow all users. The guard logs all allow/block decisions.

## First deployment

Things that cannot be checked from this repository and need a human on the NAS:

1. **Before the pull request that switches to OpenAI is merged** (merging deploys it automatically), edit `.secrets.toml` on the NAS: set `[openai] key` to an OpenAI API key and **remove** `api_type`, `api_base`, `api_version` and `deployment_id` from `[openai]`. If they are still there when the new container starts, a `deployment_id` makes LiteLLM send the call to Azure, and `api_type = "azure"` makes PR-Agent put an `azure/` prefix on the model name; either way the OpenAI route fails. The file is mounted read-only into the container, so changing it takes effect the next time the container starts.
2. After the deploy, open a test PR and read the container log: confirm the request goes to `api.openai.com` (not Azure), the model answers, the response carries usage, and `reasoning_effort` is accepted. If the `openai/responses/gpt-6.1-sol` string or the reasoning parameter is rejected, adjust the model string in `configuration.toml`.
3. The first review on a PR that an older Argus left a `REQUEST_CHANGES` review on will dismiss that review ("Argus now only gives advice ..."); this needs the App's Pull requests write permission and a failure is only logged. Any such review on a PR that never gets another Argus review stays and can be dismissed by hand on GitHub.
4. Check `logs/budget.json` after the first review: `spent_usd` should be a plausible figure for the usage shown in the log.
5. Review the token numbers of a large PR against `max_model_tokens = 80000`.

## Cost

Priced per the table in `[argus_budget]`: `gpt-6.1-sol` costs 2.00 USD per million input tokens, 0.10 per million cached input tokens and 10.00 per million output tokens (OpenAI's pricing page). The monthly cap above bounds the total; see its limit.

## Related

- Issue: [Mercury#132](https://github.com/392fyc/Mercury/issues/132)
- Research report: `Mercury/.research/reports/RESEARCH-PR-REVIEW-AGENT-2026-04-05.md`
