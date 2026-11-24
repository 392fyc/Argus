# Argus

> *Named after the hundred-eyed giant Argus Panoptes — the all-seeing watchman.*

Self-hosted **advisory** AI code review for [Mercury](https://github.com/392fyc/Mercury) and other repositories, powered by [PR-Agent](https://github.com/qodo-ai/pr-agent) 0.38.0 and two models that review each PR side by side: `gpt-6.2-sol` called directly through OpenAI's Responses API, and `claude-opus-5-5` through the Anthropic Messages API (see [Dual review](#dual-review)).

Argus gives advice. It never blocks a merge and never approves one: every review it submits is a GitHub `COMMENT`.

## What Argus does

| Rule | Behaviour |
|------|-----------|
| **Whole PR** | Every review reads the whole PR diff, never only the latest commit (a typed `-i` is ignored). |
| **On request only** | Argus reviews only when someone comments `@argus-review review` (or `/review`); opening a PR or pushing to it starts nothing (`handle_pr_actions = []`, `handle_push_trigger = false`), because every run costs money. A reviewed repository could turn push reviews back on in its own `.pr_agent.toml` (`[github_app] handle_push_trigger = true`, read through `use_repo_settings_file`); none of the five repositories has one. |
| **Knows the project** | A repository can describe itself in `.argus/review.toml` on the PR's target branch: what to focus on, which architecture/rule documents to read, which paths to ignore. See [Project modules](#project-modules-argusreviewtoml). |
| **Advice only** | Reviews are always `COMMENT`. Findings are suggestions; the footer says "advice only" (仅供参考), and a `CHANGES_REQUESTED` review left by an earlier Argus version is dismissed at the start of the next review. There is no `REQUEST_CHANGES` and no `APPROVE` anywhere in the code, and PR-Agent's own auto-approve is disabled. |
| **No repeats** | A finding that matches an existing Argus thread on the same file is not posted again when a person resolved that thread, a person replied in it, Argus judged a reply in it (accepted, "Follow-up" or escalated), or it is still open. A thread that Argus itself closed (its fix-detection and outdated rules do that whenever lines change) does not count: that only says the lines were edited, so a problem that is still there can be reported again. A finding is a repeat only when it says the same thing (same file and shared distinctive words); a different problem next to an old thread is posted. A review a person asks for on an unchanged head still reports what it found; only an automatic run on an unchanged head is cut short (Argus #58). |
| **Threshold** | Only findings of medium severity or higher are posted (`[argus_review] min_severity`, default `Medium`). Style, naming, typos and wording are dropped. A finding whose severity cannot be recognised counts as Medium and is kept, never silently dropped. |
| **Spend cap** | A monthly budget the owner sets on the deployment host (not in this public repository), resetting on the 15th (Asia/Tokyo). When it is used up Argus stops, says so once, and resumes at the next reset. See [Budget](#budget). |
| **Dual review** | Every review also asks Claude, at the same time as GPT, and merges the two answers: findings both models report are marked as agreed, and the review body compares the two lines. When one line is unavailable the review runs on the other alone. See [Dual review](#dual-review). |

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
    |-- fetch the whole PR diff (30 lines of context before each change, 10 after) — `.gitignore` included, which PR-Agent's default extension filter drops (Argus #58)
    |-- at the same time (argus_dual.py):
    |     |-- gpt-6.2-sol through OpenAI Responses, reasoning effort "high"
    |     |-- claude-opus-5-5 through the Anthropic Messages API, adaptive thinking, effort "high"
    |-- merge the two answers; mark findings both report as agreed
    |-- drop findings: skip_paths, below the severity threshold, repeats of existing threads;
    |   keep at most num_max_findings (5) inline
    |-- submit one GitHub review (COMMENT) with inline suggestions
    v
GitHub PR: advisory review comment
```

Modules: `entrypoint-guard.py` (user whitelist, `@mention` rewrite, loads the patches), `patch_suggestion_format.py` (review format, thread auto-resolve, finding filters, advice-only), `patch_reasoning.py` (reasoning effort), `argus_context.py` (project context), `argus_budget.py` (spend cap), `argus_dual.py` (dual review), `mention_rewrite.py`, `argus_events.py` (structured event log).

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

`configuration.toml` sets `model`, `model_turbo` and `model_weak` to `openai/responses/gpt-6.2-sol`, with `fallback_models=[]`.

- `openai/` is the LiteLLM provider and `responses/` selects LiteLLM's Chat-Completions-to-Responses bridge. The prefix matters: LiteLLM (v1.84.0, `get_llm_provider_logic.py`) cannot infer a provider from a bare `responses/gpt-6.2-sol`, while `openai/responses/<model>` resolves to provider `openai` and `responses_api_bridge_check` in `litellm/main.py` then strips `responses/` and routes through the bridge.
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
reset_day = 15            # 1-28
timezone = "Asia/Tokyo"
missing_usage_usd = 0.10  # charged when a call returns no token usage

[[argus_budget.prices]]   # USD per 1,000,000 tokens
model = "gpt-6.2-sol"
input = 2.00
cached_input = 0.10
output = 10.00
```

A period starts at 00:00 on `reset_day` in `timezone` and ends at the same moment of the next month (the boundary second belongs to the new period). Output tokens include reasoning tokens. An unlisted model is billed at the highest listed price.

How it works:

- **Before** every model call (review, describe, improve, reply judging) the ledger is checked; if the period's budget is used up the call is not made and `BudgetExhausted` is raised. **After** the call, the returned `usage` (input, cached input, output including reasoning) is priced and added.
- A provider "quota used up" error (`insufficient_quota`, `credit_balance_exhausted`, ...) marks the period as used up and is not retried. An ordinary rate limit (`rate_limit_exceeded`) does not.
- A call that runs past `ai_timeout` (300 seconds in `configuration.toml`, changeable as described in [Runtime settings](#runtime-settings)) is not retried: once one call in a command times out, every later call in that command is refused before it reaches the provider. The provider may still have run and billed the timed-out call, and no usage comes back, so the ledger charges a rough estimate: the prompt tokens at the input price plus 16,000 output tokens at the output price (about 0.26 USD for a 50k-token prompt). With a timeout longer than 300 seconds the output part grows in proportion, because the model had longer to write (32,000 tokens at 600 seconds, about 0.42 USD for a 50k-token prompt). A long high-effort call can produce more output, so the estimate can still be low. The failure note names the settings a rerun may change (see below), since rerunning unchanged usually times out again and is billed again. A connection timeout, where the request never reached the provider, is charged and stops the command the same way; LiteLLM reports both as a timeout.
- When a command gives up because its model calls failed and none succeeded (a timeout or any other error, but not a used-up budget, which has its own notice), Argus deletes that command's own placeholder on the PR ("Preparing review...", "Preparing PR description..." or "Preparing suggestions...", posted by `argus-review[bot]`, exact text only; placeholders of other commands stay; when the same command runs twice on one PR, only the earliest placeholder posted after this run started is removed, which is usually but not always this run's own) and posts one short note naming the command and the reason. The note and the budget notice make blocking GitHub calls, so they run in a worker thread instead of on the event loop. The note is not retried. Comment `@argus-review <command>` to run it again. A command where some calls succeeded and a later one failed (for example a side call after the review was posted) gets no note.
- When the budget is used up Argus skips every command except `/help` and `/config`, **comments once on that PR** with the next reset time (no amounts: the PR is public; the host log has them), and writes a `budget_exhausted` event to the event log. Within the same period it stays silent. The notice counts as sent only after the comment was actually posted; if posting fails, a later request tries again, at most 3 attempts per period and at least 2 minutes apart, so a failing GitHub neither loses the notice for good nor floods the PR.
- The ledger is `/var/log/argus/budget.json` (the mounted `./logs` volume; override with `ARGUS_BUDGET_PATH`): period start, spent amount, used-up flag, notified flag. Writes are atomic (temporary file, flush to disk, rename) and serialised with a thread lock plus an OS file lock. A ledger that cannot be read or fails validation counts as **used up**: stopping early is cheaper than spending without a limit. To recover, fix or delete the file. To start the period over by hand (a decision of the owner, as in Argus #65), copy the file aside, then, inside the container and holding the lock (`argus_budget._file_lock(path + ".lock")`), set `spent_usd` to 0, `exhausted` and `notified` to false, `notice_attempts` to 0 and `notice_claimed_at` to null, and write it the same atomic way; `period_start` stays; the next request reads it, with no restart. Raising the cap (below) also resumes Argus when the cap, not the provider, stopped it, but leaves `notified` set, so if the new cap is reached in the same period no notice is posted.

**The cap is set on the host, never in this repository.** The repository is public, so `monthly_usd` is not in `configuration.toml`. The owner sets it on the deployment host as `ARGUS_BUDGET__MONTHLY_USD=<amount in USD>` in the `.env` next to `docker-compose.yml` (PR-Agent reads it as `[argus_budget] monthly_usd`, see [Runtime settings](#runtime-settings); the deploy workflow never copies `.env`, so it survives deploys), then recreates the container the way the deploy workflow does (its "Rebuild and restart" step: the NAS's Container Station `docker` with `-f <argus dir>/docker-compose.yml up -d --force-recreate pr-agent`). The budget settings are read once, when each server process starts, from PR-Agent's global settings, not from a request's settings, so a reviewed repository's `.pr_agent.toml` cannot set the cap. The start-up log line `[Argus Budget] monthly cap: ...` shows the value in force. Recreating stops a review that is running, so do it between reviews. Changing the cap needs no pull request, and a pull request comment cannot change it. Without the variable, or with a value that is not a positive number (an empty value included), Argus makes no model call and says once per period that no cap is configured. Only the cap is meant to be set this way: changing `reset_day` or `timezone` mid-period can start a new period and so clear what the ledger counted. The notice on a PR names no amounts; the amount spent and the cap are in the host's log and ledger.

**Limit of the cap.** The check happens before a call and cannot know what that call will cost, so the cap can be exceeded by the calls already in flight when the last check passed. The most that can overshoot is (requests running at the same moment) x (up to 3 calls per request, which is what `/improve` runs in parallel; `/review` and `/describe` make one). Estimate of one call, not a measurement: a full 80k-token prompt is about 0.16 USD of input at 2.00 USD per million, plus up to roughly 16k output tokens (reasoning included) at 10.00 USD per million is about 0.16 USD more, so up to about 0.3 USD per call; a typical call is well below that. With one request at a time the overshoot is at most one call, about 0.3 USD in the worst case.

## Dual review

`argus_dual.py` (Mercury #821) runs every `review` on two lines at the same time and merges the answers before Argus's filters run. Other commands (`describe`, `improve`, `ask`, reply judging) stay on GPT alone.

| Line | Model | Called through |
|------|-------|----------------|
| GPT | `model` in `configuration.toml` (`openai/responses/gpt-6.2-sol`) | PR-Agent and LiteLLM, as before |
| Claude | `[argus_dual_review] model` (`claude-opus-5-5`) | the official `anthropic` SDK (pinned to 0.112.0 in the image), streaming, adaptive thinking, `effort` from the table (`high`) |

The Claude line does not go through LiteLLM: PR-Agent's LiteLLM path sends a thinking `budget_tokens`, which current Claude models reject, and has no effort setting. It receives the same system and user prompts PR-Agent built for GPT, so the project context, the language and the output format are the same. Both calls start together, so a review takes about as long as the slower line. The SDK does not retry; a failed Claude call does not hold the review back.

**Merging.** Two findings are the same when they are on the same file and either their lines are within 3 of each other and their descriptions share at least 20% of their features (`SIMILAR_TEXT`, Jaccard; features are identifiers and words of 3+ letters plus pairs of adjacent Chinese characters, generic words and phrases removed), or, when one of them names no lines, they share distinctive words the way repeated findings are recognised. Nearby lines alone never merge two findings, and two findings that both name lines further apart stay separate even when they share identifiers (one function can have two defects in two branches). Among the pairs allowed, the pairing keeps the most pairs and, among those, the highest total score (character-pair share plus line overlap), solved exactly. It is a deliberately conservative heuristic. On hand-written pairs, different defects scored at most 0.14 and the same defect between 0.00 and 0.24, so text cannot separate them well; a wrong merge would hide a finding, while a missed one only shows a defect twice. The agreed count is therefore a lower bound, which the comparison section says; the `dual_review` events give the data to tune the threshold. An agreed finding shows the header and location of the more severe version (a security header wins a tie) and keeps the other line's description below it, and the other line's lines when they differ, so a pair matched wrongly hides nothing. The repeat filter (`suppress_repeated_findings`) judges only the two versions of an agreed finding, each on its own, never the merged text: both repeat, it is dropped; one repeats, the other is posted on its own as a one-sided finding (the comparison section says how many); neither repeats, it is posted. Findings are ranked by severity, agreed first among equals, and the same inline limit as before applies (`[pr_reviewer] num_max_findings`, 5); findings over the limit are listed in the review body instead of being lost (not when an automatic run on an unchanged head drops every finding). A security concern from either answer is kept, even from an answer whose finding list could not be used. The merged review says there is no security concern only when every usable line answered so explicitly; an answer cut off before that field keeps the review marked incomplete. When neither finding list can be used, the review stays incomplete as before, still carrying any concern either answer stated.

**What the PR shows.** When both lines answered, each inline finding ends with its source: `来源：GPT 与 Claude 一致`, `仅 GPT` or `仅 Claude`; a single-line review carries no source tag. The review body has a "双线对照" section with each line's model, status and finding count, and the counts of agreed and one-sided findings. The events log gets a `dual_review` record per review with the same numbers, for comparing the two lines over time.

When a manual review loses its GitHub connection while opening the PR, Argus records the failure and attempts to post a separate notice. Replies in existing threads report their own findings. The notice identifies a run that did not publish a review.

**When one line is missing.** The review runs on the other line alone and the comparison section says so. A line is missing when: the Claude key is not set; its budget is spent or has no cap; the call times out (`ai_timeout`) or fails with another error; Claude refuses or the answer is cut off at `max_tokens`; or the answer has no valid finding list. Only when both lines fail does the review fail, with the GPT error and the usual failure note. When the GPT budget is spent while the Claude line is available, `review` still runs on Claude and the other commands stop; the one-time budget notice, posted by whichever command first meets the spent budget, says so.

**The Claude budget.** The Claude line has its own ledger (`logs/budget-anthropic.json`, path from `ARGUS_BUDGET_ANTHROPIC_PATH`) and its own cap, with the same rules as [Budget](#budget): `[argus_budget_anthropic]` in `configuration.toml` holds the reset day (1st), timezone and prices; the cap is `ARGUS_BUDGET_ANTHROPIC__MONTHLY_USD` in the host's `.env`. Without a cap the Claude line makes no call. A 402, a spend-limit 400 or a "credit balance" error marks the Claude period used up; an ordinary 429 or 529 does not. A request the API refused with an error status costs nothing; a connection that could not be opened (DNS failure, refused connection, connect timeout) or arguments the SDK rejected before building the request cost nothing either; any other failure (a timeout, a stream that broke off or reported an error after it started, a dropped connection) is charged an estimate (the prompt, counted in tokens as the GPT line counts its own, plus the request's output limit `max_tokens`, the most it can have produced), since the provider may have billed it and no usage comes back. This can over-count, never under-count. Every charge is written before the connection is closed and cannot be interrupted by cancelling the review, so a cancellation never loses one. The amounts never appear on a PR.

**Turning it on or off.** `[argus_dual_review] enabled = true` in `configuration.toml` (read from the global settings, so a reviewed repository cannot change it). With `enabled = false` the review path is PR-Agent's own, unchanged. Leaving the Anthropic key or the Claude cap unset keeps reviews on GPT alone, with the comparison section stating why.

## Runtime settings

The model settings live in `configuration.toml` and change through a pull request to this repository. Two ways change them without one (Argus #63). The spend cap is different: it lives only on the host ([Budget](#budget)).

**For one command, in the trigger comment.** PR-Agent applies `--section.key=value` arguments of a command to that request only. Argus lets exactly two through (`argus_runtime.py`):

```text
@argus-review review --config.ai_timeout=600
@argus-review review --config.reasoning_effort=medium
```

- `--config.ai_timeout`: whole seconds from 60 to `[argus_review] max_ai_timeout` (900).
- `--config.reasoning_effort`: one of `[argus_review] reasoning_efforts` (`low`, `medium`, `high`).
- Any other `--x=y` argument (another model, a different budget, other prompt settings) stops the command before a setting is applied or the model is called, and the PR gets one note saying why. Arguments without `=`, such as `-i`, are unaffected, and so are the location arguments PR-Agent itself adds to a question asked on a code line (`/ask_line --line_start=... --file_name=...`). This includes the text of a question: PR-Agent would apply a `--x=y` written in `@argus-review why does --config.model=x fail?` as a setting, so such a question is refused too; write it without the leading `--`.
- The usual case is a review that timed out on a large pull request: rerun it with a longer timeout or a lower reasoning effort. The failure note suggests both.

**For every command, on the deployment host.** PR-Agent reads environment variables over the TOML files (`pr_agent/config_loader.py` loads `dynaconf.loaders.env_loader` with no prefix, and re-applies it after a repository's `.pr_agent.toml`), and `docker-compose.yml` passes the host's `.env` into the container. A line `SECTION__KEY=value` overrides `[section] key`; the value is read as TOML, so `600` is a number:

```bash
# .env on the deployment host (not in Git)
CONFIG__AI_TIMEOUT=600
CONFIG__REASONING_EFFORT=medium
CONFIG__MAX_MODEL_TOKENS=120000
ARGUS_REVIEW__MAX_AI_TIMEOUT=1200   # raises the per-comment limit
```

- The container reads `.env` only when it is created, so a change needs a new container: `docker compose up -d pr-agent` (add `--force-recreate` to be sure it is recreated). A plain `docker compose restart` keeps the old values.
- Changing the model this way (`CONFIG__MODEL`, `CONFIG__MODEL_TURBO`, `CONFIG__MODEL_WEAK`) also works, but the price table in `[argus_budget]` must list the new model, or the budget bills it at the highest listed price.
- The values in effect are printed when the container starts: `[Argus] runtime settings: {...}` in the container log.
- PR-Agent's documentation names these variables `PR_AGENT__SECTION__KEY`; the 0.38.0 code reads `SECTION__KEY`, which is what was tested.

## Tests

`.github/workflows/tests.yml` runs `python -m pytest -q` with pr-agent 0.38.0, LiteLLM 1.84.0, pytest 9.0.2 and anthropic 0.112.0 on every pull request, and `deploy.yml` runs it before deploying: a master that fails its tests is not deployed. Locally: `python -m pip install pr-agent==0.38.0 litellm==1.84.0 pytest==9.0.2 anthropic==0.112.0`, then `python -m pytest -q`.

## Deployment Modes

| Mode | Image Tag | Tunnel Required | Latency |
|------|-----------|-----------------|---------|
| **Webhook** (recommended) | `0.38.0-github_app` | Yes (Cloudflare Tunnel) | Instant |
| **Polling** | `0.34-github_polling` (`docker-compose.polling.yml`, not updated for any of the above) | No | Minutes |

## Configuration

| File | Purpose |
|------|---------|
| `configuration.toml` | PR-Agent settings (model, auto-commands, review rules) plus `[argus_review]`, `[argus_budget]`, `[argus_dual_review]` and `[argus_budget_anthropic]` |
| `.secrets.toml` | OpenAI key, Anthropic key (optional, for the Claude line) and GitHub App credentials (`.secrets.toml.template` lists the key names) |
| `.env` | Port and tunnel token; the monthly spend cap `ARGUS_BUDGET__MONTHLY_USD`, required ([Budget](#budget)); the Claude line's cap `ARGUS_BUDGET_ANTHROPIC__MONTHLY_USD`, optional ([Dual review](#dual-review)); optional overrides of the model settings ([Runtime settings](#runtime-settings)) |
| `.argus/review.toml` (in each reviewed repository, on its target branch) | Project focus, context files, skip paths |

`[argus_review]`: `min_severity` (`Trivial`, `Minor`, `Medium`, `Major`, `Critical`; default `Medium`), `suppress_repeated_findings` (default `true`), `context_token_ratio` (default `0.25`), and the limits of the trigger-comment arguments, `max_ai_timeout` (default `900`) and `reasoning_efforts` (default `["low", "medium", "high"]`). Severity comes from the finding's `issue_header`; the review prompt asks the model to start headers with `Critical Bug`, `Security`, `Possible Bug`, `Performance` or `Possible Issue`, which all rank Medium or higher.

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

Blocked requests are recorded as events, and the self-check files an Issue when one window has five or more. Accounts that are blocked by design (Argus reviews only on request) are left out of that count: `ARGUS_SELF_CHECK_EXPECTED_ACTORS` (comma-separated, case-insensitive; default `mercury-deploy[bot],Copilot` in `docker-compose.yml`, override in `.env`). A report's signature depends on the problem type and, for blocked requests, on the set of blocked accounts, so a repeat with a different count does not open a second Issue while the first is open (Argus #72).

## First deployment

Things that cannot be checked from this repository and need a human on the NAS:

1. **Before the pull request that switches to OpenAI is merged** (merging deploys it automatically), edit `.secrets.toml` on the NAS: set `[openai] key` to an OpenAI API key and **remove** `api_type`, `api_base`, `api_version` and `deployment_id` from `[openai]`. If they are still there when the new container starts, a `deployment_id` makes LiteLLM send the call to Azure, and `api_type = "azure"` makes PR-Agent put an `azure/` prefix on the model name; either way the OpenAI route fails. The file is mounted read-only into the container, so changing it takes effect the next time the container starts.
2. After the deploy, open a test PR and read the container log: confirm the request goes to `api.openai.com` (not Azure), the model answers, the response carries usage, and `reasoning_effort` is accepted. If the `openai/responses/gpt-6.2-sol` string or the reasoning parameter is rejected, adjust the model string in `configuration.toml`.
3. The first review on a PR that an older Argus left a `REQUEST_CHANGES` review on will dismiss that review ("Argus now only gives advice ..."); this needs the App's Pull requests write permission and a failure is only logged. Any such review on a PR that never gets another Argus review stays and can be dismissed by hand on GitHub.
4. Check `logs/budget.json` after the first review: `spent_usd` should be a plausible figure for the usage shown in the log.
5. Review the token numbers of a large PR against `max_model_tokens = 80000`.
6. **Before the pull request that moved the cap out of `configuration.toml` is merged** (Argus #65), set `ARGUS_BUDGET__MONTHLY_USD` in the NAS `.env`; without it the deployed Argus makes no model call. After the deploy, the start-up log shows `[Argus Budget] monthly cap: <value>`.
7. **For the Claude line of the dual review**, add `[anthropic] key` to the NAS `.secrets.toml` and `ARGUS_BUDGET_ANTHROPIC__MONTHLY_USD` to the NAS `.env`, then recreate the container. The start-up log line `[Argus Dual] second line: on, claude-opus-5-5; API key set; monthly cap set` confirms both. After the first review, check the "双线对照" section on the PR and `logs/budget-anthropic.json`.

## Cost

Priced per the table in `[argus_budget]`: `gpt-6.2-sol` costs 2.00 USD per million input tokens, 0.10 per million cached input tokens and 10.00 per million output tokens (OpenAI's pricing page). The monthly cap above bounds the total; see its limit.

The Claude line is priced per `[argus_budget_anthropic]`: `claude-opus-5-5` costs 4.00 USD per million input tokens, 0.20 per million cache-hit tokens (a cache write 1.25 x input) and 20.00 per million output tokens, thinking included (Anthropic's pricing page). Its tokenizer counts the same text as more tokens than GPT's, so a review on the Claude line costs roughly two to three times the GPT line's. Its own cap bounds it.

## Related

- Issue: [Mercury#132](https://github.com/392fyc/Mercury/issues/132)
- Research report: `Mercury/.research/reports/RESEARCH-PR-REVIEW-AGENT-2026-04-05.md`
