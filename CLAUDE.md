# Argus — Claude Code

## Identity

Argus is Mercury's self-hosted PR review bot. This CLAUDE.md governs Claude Code sessions operating on **this repo** (the Argus codebase), not Mercury itself.

Agent: Claude Code
Role definitions: `.claude/agents/*.md` (dev + acceptance — copied out of Mercury Phase 1)

## Role positioning

Argus reviews pull requests for Mercury and other 392fyc repos using PR-Agent (0.34) + Argus patches on `gpt-5.3-codex`. It runs as a Docker container on a QNAP NAS behind a Cloudflare tunnel (`argus.fyc-space.uk`). The GitHub App is `Argus-review`.

> **Operator-only facts** (not verifiable from this repo alone — sourced from the Mercury operator's notes, subject to operator confirmation before acting):
> - App ID: `3279157`
> - Installation ID: `121494697`
> - Webhook URL: `https://argus.fyc-space.uk/api/v1/github_webhooks`
>
> If you need to act on these values, confirm them against the GitHub App settings page before proceeding — do not trust this file as the source of truth.

**You are working on the review bot itself.** Every change you make here can affect how Argus reviews future Mercury PRs. That makes this one of the highest-leverage repos in the whole stack — and also one of the most dangerous to break.

## Branching

- **`master`** is the trunk and deploy branch. Feature PRs target `master`; merging a PR to `master` (a push) triggers `.github/workflows/deploy.yml`, which SSH-deploys to the NAS via the Cloudflare tunnel.
- **Open a PR for every change** — Argus-review (this same bot) reviews it; merge to `master` after the review findings are dispositioned. Admin merge is used when the bot only COMMENTs (it casts no formal approval) and CI is green.
- **`develop` is dormant** (currently behind `master`); the historical `develop` → `master` two-branch flow is retired. Revive it only by explicit decision (e.g. `git push origin master:develop`).
- Feature branches: `feat/<slug>`, `fix/<slug>`, `chore/<slug>`.

## MUST

- **Deploy safety**: any change that could affect container behavior (Dockerfile, docker-compose*.yml, entrypoint-guard.py, configuration.toml, .github/workflows/deploy.yml) requires explicit authorization in the TaskBundle's `allowedWriteScope`.
- **PR to master**: every change merges via PR; direct push to `master` (bypassing PR) is forbidden.
- **dual-verify before commit**: every milestone must pass `/dual-verify` before committing.
- **Web search before SDK/API code**: before referencing any PR-Agent / OpenAI / GitHub API behavior, verify against official docs. PR-Agent's API changes between minor versions — do not trust training data.
- **Self-review awareness**: when this repo opens a PR, Argus-review (this same bot) will review its own code. Be prepared for reply-aware classification of your disagreements — see Mercury PR #186 for a prior example.
- **Chinese for milestones**: milestone completion messages in Chinese.

## DO NOT

- Do not modify secrets, `.pem`, `.env`, or anything with the string `APP_PRIVATE_KEY` / `WEBHOOK_SECRET`.
- Do not modify the GitHub App Installation ID, App ID, or webhook URL (see Operator-only facts above).
- Do not change `argus.fyc-space.uk` routing without user confirmation — that touches Cloudflare tunnel state.
- Do not bypass `/dual-verify` as the pre-commit gate.
- Do not run `sudo DOCKER_HOST=... docker compose ... up` from an agent session — deploy changes go through GitHub Actions, not agent-initiated manual deploys.

## Mercury integration

This repo hosts the **Mercury validation roadmap** (a GitHub Project in this repo) tracking milestones M1/M2/M3 that Mercury uses to validate its Phase 1 dev-pipeline on a real external project. The first PR in this roadmap (`feat/phase0-claude-infra`) bootstraps the `.claude/` directory itself.

When a Mercury-driven session works on an Argus Issue, it uses:
- `/dev-pipeline` — Main → Dev → Acceptance chain (`.claude/skills/dev-pipeline/SKILL.md`)
- `/pr-flow` — PR lifecycle with Argus fix-detection resolve (`.claude/skills/pr-flow/SKILL.md`)
- `/dual-verify` — pre-commit gate (`.claude/skills/dual-verify/SKILL.md`)

No Mercury-specific skills (like `gh-project-flow`) live in this repo — they are Mercury self-development only.
