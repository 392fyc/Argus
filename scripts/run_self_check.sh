#!/usr/bin/env bash
# Argus self-check runner — Plan B: NAS cron via Codex CLI Docker container
#
# Runs the self-check agent using Codex CLI in an isolated Docker container.
# No LiteLLM or Claude Code required; Codex uses OPENAI_API_KEY directly.
#
# Usage (NAS cron, daily check with adaptive interval gate):
#   17 2 * * *  /path/to/argus/scripts/run_self_check.sh >> /var/log/argus/self-check.log 2>&1
#
# Prerequisites (build once on NAS):
#   docker build -f scripts/Dockerfile.codex -t argus-selfcheck .
#
# Required env (set in NAS shell profile or cron env):
#   OPENAI_API_KEY    — OpenAI API key (Codex CLI uses this directly)
#   GH_TOKEN          — GitHub token with issues:write scope
#   ARGUS_REPO_DIR    — absolute path to Argus repo on NAS (default: directory of this script's parent)
#
# Optional:
#   SELF_CHECK_DAYS         — analysis window in days (default: 3)
#   SELF_CHECK_MAX_ISSUES   — cap on Issues per run (default: 5)
#   SELF_CHECK_DRY_RUN      — set to 1 to preview without filing
#   ARGUS_EVENTS_PATH       — path to events.jsonl inside container (default: /var/log/argus/events.jsonl)
#   EVENTS_HOST_PATH        — host path to events.jsonl (default: /var/log/argus/events.jsonl)
#   CODEX_IMAGE             — Docker image name (default: argus-selfcheck)
#   ARGUS_STATE_FILE        — path to adaptive scheduling state JSON
#                             (default: /var/log/argus/self-check-state.json)
#
# Adaptive scheduling:
#   On a quiet run (exit 0, no issues filed): interval += 1 day, max 7 days.
#   On a finding run (exit 2, issues filed):  interval resets to 3 days.
#   On error (any other exit code):           interval unchanged.
#   Set NAS cron to run daily; the interval gate inside this script handles pacing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${ARGUS_REPO_DIR:-$(dirname "$SCRIPT_DIR")}"
PAUSE_FLAG="/var/log/argus/.self-check-disabled"

DAYS="${SELF_CHECK_DAYS:-3}"
MAX_ISSUES="${SELF_CHECK_MAX_ISSUES:-5}"
DRY_RUN="${SELF_CHECK_DRY_RUN:-0}"
EVENTS_HOST="${EVENTS_HOST_PATH:-/var/log/argus/events.jsonl}"
EVENTS_CONTAINER="${ARGUS_EVENTS_PATH:-/var/log/argus/events.jsonl}"
IMAGE="${CODEX_IMAGE:-argus-selfcheck}"
STATE_FILE="${ARGUS_STATE_FILE:-/var/log/argus/self-check-state.json}"

# ── Docker environment (QNAP Container Station) ───────────────────────────────
# QNAP does not expose Docker on the standard /var/run/docker.sock.
# Container Station's daemon socket is used instead.
# Override DOCKER_CMD to use a different docker binary or connection settings.
_QNAP_DOCKER=/share/CACHEDEV1_DATA/.qpkg/container-station/bin/docker
_QNAP_SOCK=unix:///var/run/system-docker.sock
_DOCKER_CFG=/tmp/docker-selfcheck-$$

if [ -z "${DOCKER_CMD:-}" ]; then
  if [ -x "$_QNAP_DOCKER" ]; then
    mkdir -p "$_DOCKER_CFG"
    export DOCKER_CONFIG="$_DOCKER_CFG"
    export DOCKER_HOST="${DOCKER_HOST:-$_QNAP_SOCK}"
    DOCKER_CMD="$_QNAP_DOCKER"
  else
    DOCKER_CMD="docker"  # fallback: docker in PATH (non-QNAP environments)
  fi
fi

MIN_INTERVAL=3
MAX_INTERVAL=7

# ── Pause check ───────────────────────────────────────────────────────────────
if [ -f "$PAUSE_FLAG" ]; then
  echo "[self-check] Paused ($PAUSE_FLAG exists). Remove to re-enable."
  exit 0
fi

# ── Verify events file ────────────────────────────────────────────────────────
if [ ! -f "$EVENTS_HOST" ]; then
  echo "[self-check] WARNING: events file not found at $EVENTS_HOST — skipping run."
  echo "[self-check] Set EVENTS_HOST_PATH to the correct path and retry."
  exit 0
fi

# ── Read adaptive scheduling state ───────────────────────────────────────────
CURRENT_INTERVAL=$MIN_INTERVAL
LAST_RUN=""
if [ -f "$STATE_FILE" ]; then
  CURRENT_INTERVAL=$(python3 - <<PYEOF
import json, sys
try:
    d = json.load(open("$STATE_FILE"))
    print(int(d.get("interval_days", $MIN_INTERVAL)))
except Exception:
    print($MIN_INTERVAL)
PYEOF
)
  LAST_RUN=$(python3 - <<PYEOF
import json, sys
try:
    d = json.load(open("$STATE_FILE"))
    print(d.get("last_run", ""))
except Exception:
    print("")
PYEOF
)
fi

# ── Interval gate ─────────────────────────────────────────────────────────────
if [ -n "$LAST_RUN" ]; then
  DAYS_SINCE=$(python3 - <<PYEOF
from datetime import date
try:
    delta = date.today() - date.fromisoformat("$LAST_RUN")
    print(delta.days)
except Exception:
    print(999)
PYEOF
)
  if [ "$DAYS_SINCE" -lt "$CURRENT_INTERVAL" ]; then
    echo "[self-check] Skipping: ${DAYS_SINCE} day(s) since last run; interval is ${CURRENT_INTERVAL} days."
    exit 0
  fi
fi

echo "[self-check] Starting at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[self-check] Window: ${DAYS} days | max-issues: ${MAX_ISSUES} | dry-run: ${DRY_RUN} | interval: ${CURRENT_INTERVAL} days"

# ── Build Codex args ──────────────────────────────────────────────────────────
EXTRA_ARGS=""
[ "$DRY_RUN" = "1" ] && EXTRA_ARGS="--dry-run"

PROMPT="Run the Argus self-check: execute 'python argus_self_check.py --days ${DAYS} --max-issues ${MAX_ISSUES} ${EXTRA_ARGS}' and report results. Do NOT modify any source files, configs, or deploy files."

# ── Run Codex in container ────────────────────────────────────────────────────
DOCKER_EXIT=0
$DOCKER_CMD run --rm \
  --name "argus-self-check-$$" \
  -e "OPENAI_API_KEY=${OPENAI_API_KEY}" \
  -e "GH_TOKEN=${GH_TOKEN:-}" \
  -e "ARGUS_EVENTS_PATH=${EVENTS_CONTAINER}" \
  -v "${REPO_DIR}:/workspace:ro" \
  -v "${EVENTS_HOST}:${EVENTS_CONTAINER}:ro" \
  -w /workspace \
  "${IMAGE}" \
  codex exec --approval-mode full-auto "${PROMPT}" || DOCKER_EXIT=$?

# ── Update adaptive schedule state ───────────────────────────────────────────
TODAY=$(date +%Y-%m-%d)
if [ $DOCKER_EXIT -eq 0 ]; then
  # Quiet run: no issues filed — gradually widen interval
  NEW_INTERVAL=$(python3 -c "print(min($CURRENT_INTERVAL + 1, $MAX_INTERVAL))")
  echo "[self-check] Quiet run. Interval: ${CURRENT_INTERVAL} → ${NEW_INTERVAL} days."
elif [ $DOCKER_EXIT -eq 2 ]; then
  # Findings detected: reset to minimum interval
  NEW_INTERVAL=$MIN_INTERVAL
  echo "[self-check] Findings detected (exit 2). Interval reset to ${MIN_INTERVAL} days."
else
  # Error: keep current interval, do not advance last_run date
  echo "[self-check] Run error (exit ${DOCKER_EXIT}). Interval unchanged (${CURRENT_INTERVAL} days)."
  echo "[self-check] Completed at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  exit $DOCKER_EXIT
fi

# Ensure state directory exists
mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null || true

python3 - <<PYEOF
import json
state = {"interval_days": $NEW_INTERVAL, "last_run": "$TODAY"}
with open("$STATE_FILE", "w") as f:
    json.dump(state, f)
print(f"[self-check] State saved: interval={state['interval_days']} days, last_run={state['last_run']}")
PYEOF

echo "[self-check] Completed at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
exit $DOCKER_EXIT
