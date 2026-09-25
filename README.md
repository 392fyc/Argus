# Argus

> *Named after the hundred-eyed giant Argus Panoptes — the all-seeing watchman.*

Self-hosted AI code review for [Mercury](https://github.com/392fyc/Mercury), powered by [Qodo PR-Agent](https://github.com/qodo-ai/pr-agent) and GPT-6 Luna through Azure OpenAI.

Replaces CodeRabbit SaaS with a self-hosted Docker deployment on QNAP NAS.

## Quick Start

```bash
# 1. Run setup
bash setup.sh

# 2. Edit secrets
nano .secrets.toml   # Add OpenAI key + GitHub App credentials

# 3. Start (webhook mode)
docker compose up -d

# 3b. Or start (polling mode — no tunnel needed)
docker compose -f docker-compose.polling.yml up -d
```

## Architecture

```
GitHub PR Event
    |
    v (webhook via Cloudflare Tunnel)
Argus: PR-Agent + gpt-6-luna (Docker)
    |-- Fetch PR diff
    |-- Send to gpt-6-luna through Azure Responses
    |-- Submit formal GitHub review
    v
GitHub PR: APPROVED / CHANGES_REQUESTED
```

## Deployment Modes

| Mode | Image Tag | Tunnel Required | Latency |
|------|-----------|-----------------|---------|
| **Webhook** (recommended) | `0.38.0-github_app` | Yes (Cloudflare Tunnel) | Instant |
| **Polling** | `0.34-github_polling` | No | Minutes |

## Model Routing (NAS Webhook)

This model route was verified on the NAS webhook deployment with PR-Agent 0.38.0 and LiteLLM 1.84.0. It describes the webhook service in `docker-compose.yml`; the separate polling deployment has its own older compose file.

`configuration.toml` sets `model`, `model_turbo`, and `model_weak` to `responses/gpt-6-luna`. With `api_type="azure"`, the PR-Agent LiteLLM handler adds the `azure/` provider prefix, so the configured model value must not include that prefix. All three settings use the same Azure deployment, so `fallback_models=[]` avoids a fallback to another model; PR-Agent's normal request retries remain enabled.

The webhook container sets `OPENAI__DEPLOYMENT_ID=gpt-6-luna`. This non-secret environment value takes precedence over the older `deployment_id` in `.secrets.toml`, so the secret file does not need to change. A successful NAS request through the PR-Agent handler returned `gpt-6-luna-2026-09-22` in the response's `model` field.

## Configuration

| File | Purpose |
|------|---------|
| `configuration.toml` | PR-Agent settings (model, auto-commands, review rules) |
| `.secrets.toml` | API keys and GitHub App credentials |
| `.env` | Port and tunnel token |
| `.pr_agent.toml` (in Mercury repo) | Repo-specific review rules |

## GitHub App Setup

1. Go to https://github.com/settings/apps/new
2. Set:
   - **Name**: Argus
   - **Webhook URL**: `https://<tunnel-domain>/api/v1/github_webhooks`
   - **Webhook Secret**: (random string, save to `.secrets.toml`)
3. Permissions:
   - Pull requests: Read & Write
   - Issues: Read & Write
   - Contents: Read
   - Metadata: Read
4. Subscribe to events: `Pull request`, `Issue comment`, `Pull request review comment`
5. Generate private key (.pem), save to `.secrets.toml`
6. Install the app on 392fyc/Mercury

## User Whitelist

Argus includes a guard middleware that restricts which GitHub users can trigger reviews.

Configure in `.env`:
```bash
# Only these users can trigger reviews (comma-separated, case-insensitive)
ARGUS_ALLOWED_USERS=392fyc,trusted-bot
```

Leave empty to allow all users. The guard logs all allow/block decisions.

## Cost

Cost depends on Azure's current price for the deployed model and the actual token usage. Check current Azure pricing and request usage records before estimating a per-review cost.

## Related

- Issue: [Mercury#132](https://github.com/392fyc/Mercury/issues/132)
- Research report: `Mercury/.research/reports/RESEARCH-PR-REVIEW-AGENT-2026-04-05.md`
