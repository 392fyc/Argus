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

### Plan A — GitHub Actions + Codex CLI (RETIRED 2026-06-20)

> **Retired.** `.github/workflows/self-check.yml` was removed. The GitHub Actions
> runner has no `OPENAI_API_KEY` and cannot reach the NAS events sink
> (`/var/log/argus/events.jsonl` lives on the NAS, not on the cloud runner), so
> every scheduled run failed daily. **Plan B (below) is the active self-check
> mechanism** — it runs on the NAS, where the events sink is local. Verified live
> on 2026-06-20 (`last_run` advancing, no `exit 127`). See Mercury #476 / Argus #33.
>
> Reviving a cloud-runner self-check would first require solving events-sink access
> (ship events to the runner, or have the runner query a NAS-hosted API) and key
> provisioning — neither is in place today.

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
