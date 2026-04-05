#!/usr/bin/env bash
# Argus — Quick Setup Script
# Prepares the deployment environment on the current machine or NAS.

set -euo pipefail

echo "=== Argus Setup ==="
echo ""

# ── 1. Copy templates ───────────────────────────────────────────
if [ ! -f .env ]; then
    cp .env.template .env
    echo "[OK] Created .env from template"
else
    echo "[SKIP] .env already exists"
fi

if [ ! -f .secrets.toml ]; then
    cp .secrets.toml.template .secrets.toml
    echo "[OK] Created .secrets.toml from template"
    echo ""
    echo ">>> IMPORTANT: Edit .secrets.toml with your API keys <<<"
    echo "    - OpenAI API key"
    echo "    - GitHub App credentials (app_id, webhook_secret, private_key)"
    echo "    - Or GitHub user_token (for polling mode)"
else
    echo "[SKIP] .secrets.toml already exists"
fi

echo ""

# ── 2. Pull Docker images ──────────────────────────────────────
echo "Pulling Docker images..."
docker pull codiumai/pr-agent:0.34-github_app
docker pull cloudflare/cloudflared:latest
echo "[OK] Docker images pulled"

echo ""

# ── 3. Verify ──────────────────────────────────────────────────
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .secrets.toml with your OpenAI API key and GitHub App credentials"
echo "  2. Edit .env with your Cloudflare Tunnel token (if using webhook mode)"
echo "  3. Start webhook mode:  docker compose up -d"
echo "  4. Start polling mode:  docker compose -f docker-compose.polling.yml up -d"
echo ""
echo "Health check:  curl http://localhost:3000/"
echo "Webhook URL:   https://<your-tunnel-domain>/api/v1/github_webhooks"
