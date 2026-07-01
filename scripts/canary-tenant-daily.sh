#!/usr/bin/env bash
# canary-tenant-daily.sh — operator-runnable + cron-callable wrapper that
# invokes the harness:canary-smoke skill for one tenant.
#
# Usage: canary-tenant-daily.sh <tenant_name> [tenant_repo_path]
#
# Default tenant_repo_path: ../<tenant_name> relative to HARNESS.
# Writes .harness/canary/<tenant_name>/<YYYY-MM-DD>.json in the HARNESS repo.
#
# Cron entry pattern (per tenant harness.json):
#   {"name": "canary-tenant-daily", "schedule": "0 9 * * *",
#    "action": "canary-run", "enabled": false}
#
# Per CLAUDE.md macOS bash 3.2 compatibility: avoids bash-4-only builtins,
# case-modifier parameter expansions, associative arrays, and append-redirect.
# No unguarded empty-array expansion under set -u.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=../lib/operator-env.sh
source "$HARNESS_ROOT/lib/operator-env.sh"

TENANT="${1:?Usage: $0 <tenant_name> [tenant_repo_path]}"

# Validate tenant name to prevent shell injection (via claude -p invocation below)
# and path traversal (via OUT_DIR/mkdir below). Tenant names are slug-shaped.
if [[ ! "$TENANT" =~ ^[a-zA-Z0-9_-]+$ ]]; then
  echo "ERROR: invalid tenant name '$TENANT' — must match [a-zA-Z0-9_-]+" >&2
  exit 1
fi

TENANT_REPO="${2:-$HARNESS_ROOT/../$TENANT}"

# Validate tenant_repo path for the same defense-in-depth reasons as $TENANT.
# Allows '/' (it's a path), plus '.' for relative segments like '../tyler-electric'.
if [[ ! "$TENANT_REPO" =~ ^[a-zA-Z0-9_./\-]+$ ]]; then
  echo "ERROR: invalid tenant_repo '$TENANT_REPO' — must match [a-zA-Z0-9_./-]+" >&2
  exit 1
fi

if [[ ! -f "$TENANT_REPO/harness.json" ]]; then
  echo "ERROR: tenant harness.json not found at $TENANT_REPO/harness.json" >&2
  exit 1
fi

OUT_DIR="$HARNESS_ROOT/.harness/canary/$TENANT"
mkdir -p "$OUT_DIR"
DATE="$(date -u +%Y-%m-%d)"
OUT_PATH="$OUT_DIR/$DATE.json"

# Gstack-absent fallback. The script writes a skip artifact directly so the
# cron run produces an audit file even on developer machines without gstack
# (matches lib/skills/canary-smoke.md gate).
if [[ ! -f "$HOME/.claude/skills/gstack/SKILL.md" ]]; then
  TENANT="$TENANT" OUT_PATH="$OUT_PATH" python3 <<'PYEOF'
import json, os
from datetime import datetime, timezone
json.dump({
    "tenant": os.environ["TENANT"],
    "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "url": None,
    "endpoints_checked": [],
    "latency_p95_ms": None,
    "errors": [],
    "verdict": "skip",
    "gstack_session_id": None,
    "skip_reason": "gstack not installed"
}, open(os.environ["OUT_PATH"], "w"), indent=2)
PYEOF
  echo "SKIP (gstack not installed) — wrote $OUT_PATH"
  exit 0
fi

# Resolve URL from tenant harness.json deployment.subdomain.
URL="$(TENANT_REPO="$TENANT_REPO" python3 <<'PYEOF'
import json, os, sys
p = os.path.join(os.environ["TENANT_REPO"], "harness.json")
d = json.load(open(p))
sub = d.get("deployment", {}).get("subdomain")
if not sub:
    print("", end="")
    sys.exit(0)
print(f"https://{sub}", end="")
PYEOF
)"

if [[ -z "$URL" ]]; then
  TENANT="$TENANT" OUT_PATH="$OUT_PATH" python3 <<'PYEOF'
import json, os
from datetime import datetime, timezone
json.dump({
    "tenant": os.environ["TENANT"],
    "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "url": None,
    "endpoints_checked": [],
    "latency_p95_ms": None,
    "errors": [],
    "verdict": "skip",
    "gstack_session_id": None,
    "skip_reason": "tenant harness.json missing deployment.subdomain"
}, open(os.environ["OUT_PATH"], "w"), indent=2)
PYEOF
  echo "SKIP (no subdomain in tenant harness.json) — wrote $OUT_PATH"
  exit 0
fi

# Dispatch the canary-smoke skill via claude -p (subagent context). The
# skill itself invokes gstack:canary and produces the report. We then
# transform it into our schema.

# Pre-check: claude binary must be in PATH. Fail fast with a clear message
# rather than a confusing "180s timeout" when the CLI is not installed.
if ! command -v claude >/dev/null 2>&1; then
  TENANT="$TENANT" URL="$URL" OUT_PATH="$OUT_PATH" python3 <<'PYEOF'
import json, os
from datetime import datetime, timezone
json.dump({
    "tenant": os.environ["TENANT"],
    "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "url": os.environ["URL"],
    "endpoints_checked": [],
    "latency_p95_ms": None,
    "errors": ["claude CLI not in PATH"],
    "verdict": "fail",
    "gstack_session_id": None
}, open(os.environ["OUT_PATH"], "w"), indent=2)
PYEOF
  echo "FAIL (claude CLI not in PATH) — wrote $OUT_PATH" >&2
  exit 0
fi

LOG_DIR="$HARNESS_ROOT/.harness/logs"
mkdir -p "$LOG_DIR" 2>/dev/null || true
LOG_FILE="$LOG_DIR/canary-tenant-daily.log"

echo "Dispatching canary-smoke skill for $TENANT against $URL ..."

# Background claude -p runs the skill; capture its stdout. We don't need to
# block on output — the skill writes the artifact directly. But for the
# CLI path (harness canary run) we tail until the artifact appears.
# Export TENANT_REPO + URL + HARNESS_ROOT so the canary-smoke skill (executed
# in a subprocess) sees them regardless of PWD (cron PWD=$HOME would break
# the documented `../<tenant_name>` default otherwise). cd into HARNESS_ROOT
# so the skill's relative `.harness/canary/<tenant>/<date>.json` write lands
# at the absolute OUT_PATH we computed above.
#
# `exec` is load-bearing: without it the subshell forks claude as a child
# process, $! captures the SUBSHELL pid (not claude's), and the EXIT trap's
# `kill "$CLAUDE_PID"` SIGTERMs the subshell — which dies WITHOUT propagating
# to claude, orphaning the child to init. Verified empirically (R1 review).
# With `exec`, the subshell process IS claude after replacement, so $! is the
# real claude pid and the trap's kill reaches it.
(
  cd "$HARNESS_ROOT"
  TENANT_REPO="$TENANT_REPO" URL="$URL" HARNESS_ROOT="$HARNESS_ROOT" \
    exec claude -p "/harness:canary-smoke $TENANT" \
    --permission-mode bypassPermissions \
    --no-session-persistence \
    >> "$LOG_FILE" 2>&1
) &
CLAUDE_PID=$!

# Safety net: ensure the background claude is killed on any exit path
# (timeout path below would otherwise leave a zombie per daily cron run).
trap 'kill "$CLAUDE_PID" 2>/dev/null || true; wait "$CLAUDE_PID" 2>/dev/null || true' EXIT

# Wait up to 180s for the artifact to land, polling every 5s. If it doesn't,
# write a fail-with-timeout artifact.
DEADLINE=$(( $(date +%s) + 180 ))
while [[ $(date +%s) -lt $DEADLINE ]]; do
  if [[ -f "$OUT_PATH" ]]; then
    echo "canary-smoke artifact landed at $OUT_PATH"
    # Detach the background claude (it may still be wrapping up logging)
    wait "$CLAUDE_PID" 2>/dev/null || true
    exit 0
  fi
  sleep 5
done

# Timeout — write a fail artifact rather than leaving the cron silent.
TENANT="$TENANT" URL="$URL" OUT_PATH="$OUT_PATH" python3 <<'PYEOF'
import json, os
from datetime import datetime, timezone
json.dump({
    "tenant": os.environ["TENANT"],
    "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "url": os.environ["URL"],
    "endpoints_checked": [],
    "latency_p95_ms": None,
    "errors": ["canary-smoke skill did not produce artifact within 180s"],
    "verdict": "fail",
    "gstack_session_id": None
}, open(os.environ["OUT_PATH"], "w"), indent=2)
PYEOF
echo "FAIL (skill timeout) — wrote $OUT_PATH"
# Intentional exit 0 on timeout: this is a CRON-FRIENDLINESS choice, not a bug.
# The verdict field in $OUT_PATH ('fail' with errors[] populated) carries the
# signal for any downstream consumer (review-time visibility, audit). Exiting
# non-zero would cause cron to email the operator on EVERY timeout — noise
# that drowns the actual signal in the artifact. Same pattern as the
# gstack-absent SKIP path above and the claude-not-in-PATH path further up.
exit 0
