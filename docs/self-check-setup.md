# Argus Self-Check Setup

Argus M3 self-check agent runs a periodic log analysis and auto-files GitHub Issues
for detected problems. It consumes structured events produced by M2 (`argus_events.py`).

## Architecture

```
events.jsonl (M2 sink)
    │
    ▼
argus_log_analyzer.py  ── detects problem clusters
    │
    ▼
argus_issue_formatter.py ── formats GitHub Issue bodies
    │
    ▼
argus_self_check.py  ── deduplicates + calls gh issue create
    │
    ▼
GitHub Issues (label: source:self-check)
```

## Deployment options

### Plan A — GitHub Actions + Codex CLI (recommended)

**When to use**: Argus repo has access to an OpenAI API key via repository secrets.

**How it works**: `openai/codex-action@v1` runs `argus_self_check.py` inside a
constrained Codex CLI session every 3 days.

**Setup**:

1. Add repository secret: `OPENAI_API_KEY`
   - `GITHUB_TOKEN` is provided automatically by GitHub Actions (no manual secret needed)
   - The workflow grants `issues: write` permission so `${{ secrets.GITHUB_TOKEN }}` can file Issues
2. (Optional) Add repository variables:
   - `SELF_CHECK_DAYS` — analysis window days (default: 3)
   - `SELF_CHECK_MAX` — max Issues per run (default: 5)
   - `ARGUS_EVENTS_PATH` — events sink path (default: `/var/log/argus/events.jsonl`)
3. The workflow `.github/workflows/self-check.yml` fires automatically on schedule.

**Manual trigger**: GitHub UI → Actions → "Argus self-check" → Run workflow.

**Pause**: Set repository variable `SELF_CHECK_DISABLED=1`.

---

### Plan B — NAS cron via Codex CLI Docker container

**When to use**: The NAS can access the events sink directly, or you want a fully
on-premises scheduled run without GitHub Actions.

**How it works**: A custom Docker container bundles Codex CLI + Python + gh CLI.
NAS cron calls `docker run` every 3 days; Codex uses `OPENAI_API_KEY` directly —
no LiteLLM or Claude Code proxy needed.

> **Why Docker?** All CLIs (Codex, Claude Code, LiteLLM, ccproxy) run in isolated
> containers — injecting them into the Argus Python container would add Node.js and
> inflate the image. Container Station (QNAP Docker) supports this natively.

**Build the container (once)**:

```bash
# From Argus repo root on the NAS:
docker build -f scripts/Dockerfile.codex -t argus-selfcheck .
```

**Required environment variables** (set in NAS shell profile or cron env):

```bash
export OPENAI_API_KEY=sk-...       # OpenAI API key (used by Codex directly)
export GH_TOKEN=ghp_...            # GitHub token (issues:write)
```

**NAS cron entry** (every 3 days at 02:17 local time):

```cron
17 2 * * *  OPENAI_API_KEY=sk-... GH_TOKEN=ghp_... /path/to/argus/scripts/run_self_check.sh >> /var/log/argus/self-check.log 2>&1
```

> The script's interval gate handles pacing (starts at 3 days, adapts up to 7).
> Running the cron daily ensures the gate fires on the correct day.

**Pause**: `touch /var/log/argus/.self-check-disabled`

**Resume**: `rm /var/log/argus/.self-check-disabled`

**Dry run (preview only)**:

```bash
SELF_CHECK_DRY_RUN=1 bash scripts/run_self_check.sh
```

**Direct Python mode** (bypass Codex — workaround for [openai/codex#13103](https://github.com/openai/codex/issues/13103) WebSocket auth bug):

```bash
USE_DIRECT_PYTHON=1 bash scripts/run_self_check.sh
```

> In this mode `OPENAI_API_KEY` is not required; only `GH_TOKEN` and the events file.
> Use this when Codex CLI connectivity is broken but the container + Python path is working.

---

### Plan C — Claude Code + LiteLLM + ccproxy (if Codex unavailable)

**When to use**: Codex CLI cannot be used (licensing, access, or quota constraints),
and you want Claude Code to orchestrate the run while using an **OpenAI API key as
the model backend** (reduces Anthropic subscription load).

**Architecture** (all separate Docker containers, connected via Docker network):

```
Claude Code container
  → ANTHROPIC_BASE_URL=http://ccproxy:4000
  → ccproxy container (starbased-co/ccproxy)
  → LiteLLM container (ghcr.io/berriai/litellm:main-stable)
  → OpenAI API
```

**LiteLLM container**:
```bash
docker run -d --name litellm -p 4000:4000 \
  -e OPENAI_API_KEY="$OPENAI_API_KEY" \
  -v "$(pwd)/scripts/litellm_config.yaml:/app/config.yaml" \
  ghcr.io/berriai/litellm:main-stable \
  --config /app/config.yaml
```

**ccproxy + Claude Code** (custom container — see ccproxy docs):
- Source: https://github.com/starbased-co/ccproxy
- Install: `uv tool install claude-ccproxy --with 'litellm[proxy]'`
- Set: `ANTHROPIC_BASE_URL=http://ccproxy:4000` (Claude Code → ccproxy → LiteLLM → OpenAI)

`scripts/litellm_config.yaml` contains the model routing config for this scenario.

---

## Manual invocation (ad-hoc)

Without any agent runner — Python only:

```bash
# Preview
python argus_self_check.py --days 3 --dry-run

# File issues
python argus_self_check.py --days 3

# Custom window
python argus_self_check.py --days 7 --max-issues 3 --sink-path /custom/path.jsonl
```

---

## Adaptive scheduling (Plan B)

`run_self_check.sh` implements adaptive scheduling so quiet periods reduce check
frequency automatically.

**Behavior:**

| Run outcome | Exit code | Interval change |
|-------------|-----------|-----------------|
| No issues filed | 0 | `interval += 1 day` (max 7) |
| Issues filed | 2 | reset to 3 days |
| Error | other | unchanged |

**Progression example** (all quiet runs):

```
Run 1: interval=3 → Run 2 in 4 days
Run 2: interval=4 → Run 3 in 5 days
Run 3: interval=5 → Run 4 in 6 days
Run 4: interval=6 → Run 5 in 7 days  ← stabilises at max
```

**State file:** `${ARGUS_STATE_FILE}` (default: `/var/log/argus/self-check-state.json`)

```json
{"interval_days": 5, "last_run": "2026-04-12"}
```

**NAS cron** should be set to **daily** (not `*/3`) so the interval gate inside the
script controls pacing:

```cron
17 2 * * *  OPENAI_API_KEY=sk-... GH_TOKEN=ghp_... /path/to/argus/scripts/run_self_check.sh >> /var/log/argus/self-check.log 2>&1
```

**Reset to 3-day interval manually:**

```bash
echo '{"interval_days": 3, "last_run": ""}' > /var/log/argus/self-check-state.json
```

**Plan A note:** GitHub Actions cron is static YAML — adaptive scheduling is not
available in Plan A. To change the interval, update the `schedule.cron` field in
`.github/workflows/self-check.yml` or configure `SELF_CHECK_DAYS` and re-trigger
manually via `workflow_dispatch`.

---

## Deduplication

Each problem cluster has a stable `signature` (16-char SHA-256 prefix of `type:title`).
Before filing, the self-check searches for open Issues with `sig:<signature>` in the
title. If found, the cluster is skipped.

To reset deduplication for a cluster: close or label its existing Issue as `wontfix`.

---

## Filed Issue format

Every auto-filed Issue:
- Title: `[self-check] <emoji> <description> — sig:<signature>`
- Labels: `source:self-check`, `type:bug` or `type:analysis`, `priority:p0–p2`
- Body: observed evidence (facts) + UNVERIFIED hypothesis + suggested next step

---

## Safeguards

- Agent MUST NOT modify source files, configs, or deploy files
- Agent only files Issues to `392fyc/Argus`
- Max 5 Issues per run (configurable)
- All hypotheses labeled `UNVERIFIED` in the Issue body
- Human review required before any fix is implemented
