#!/usr/bin/env bash
# HARNESS Session State Protocol
# Sources into any script to register, update, and close agent sessions.
# All sessions write to .harness/sessions/{session-id}.json within each repo.
# Designed to be per-repo: session tracking is scoped to the repo's working directory.

set -euo pipefail

# Note: REPO_ROOT, HARNESS_DIR, SESSIONS_DIR are set inside harness_register(),
# not at module scope. Scripts that need SESSIONS_DIR before calling
# harness_register must compute it themselves (see harness_gc for example).

# Generate a unique session ID (timestamp + random)
harness_session_id() {
  echo "$(date +%s)-$$-$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
}

# Detect repo context automatically
_harness_detect_repo() {
  local remote_url org repo
  remote_url=$(git remote get-url origin 2>/dev/null || echo "unknown")
  if [[ "$remote_url" == *"github.com"* ]]; then
    # Extract org/repo from https or ssh URL
    org=$(echo "$remote_url" | sed -E 's#.*github\.com[:/]([^/]+)/.*#\1#')
    repo=$(echo "$remote_url" | sed -E 's#.*github\.com[:/][^/]+/([^.]+)(\.git)?$#\1#')
  else
    org="local"
    repo=$(basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)")
  fi
  echo "$org" "$repo"
}

# Register a new session
# Usage: harness_register <stage> [pr_number] [session_source] [intent] [request_text]
# Stages: classifying, debugging, brainstorming, planning, executing, reviewing, complete,
#          brainstorm, categorize, new-project, new-feature, cleanup,
#          security-review, code-review, doc-update, auto-fix,
#          daily-maintenance, breaking-check, test-suite, browser-test,
#          skill-selection
harness_register() {
  # Detect repo root and ensure sessions directory exists
  local REPO_ROOT HARNESS_DIR SESSIONS_DIR
  REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
  HARNESS_DIR="${REPO_ROOT}/.harness"
  SESSIONS_DIR="$HARNESS_DIR/sessions"
  mkdir -p "$SESSIONS_DIR"

  local stage="${1:?Stage required}"
  local pr_number="${2:-}"
  local session_source="${3:-local}"
  local intent="${4:-}"
  local request="${5:-}"
  local session_id
  session_id=$(harness_session_id)

  local org repo branch
  read -r org repo <<< "$(_harness_detect_repo)"
  branch=$(git branch --show-current 2>/dev/null || echo "unknown")

  local session_file="$SESSIONS_DIR/$session_id.json"
  local user
  user=$(git config user.name 2>/dev/null || echo "unknown")

  HARNESS_SESSION_PATH="$session_file" \
  HARNESS_SID="$session_id" HARNESS_ORG="$org" HARNESS_REPO="$repo" \
  HARNESS_BRANCH="$branch" HARNESS_PR="${pr_number:-}" \
  HARNESS_SOURCE="$session_source" HARNESS_INTENT="$intent" \
  HARNESS_REQUEST="$request" HARNESS_STAGE="$stage" \
  HARNESS_USER="$user" HARNESS_PID="$$" \
  python3 <<'PYEOF'
import json, os
from datetime import datetime, timezone
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
pr = os.environ['HARNESS_PR']
d = {
    'session_id': os.environ['HARNESS_SID'],
    'org': os.environ['HARNESS_ORG'],
    'repo': os.environ['HARNESS_REPO'],
    'branch': os.environ['HARNESS_BRANCH'],
    'pr': int(pr) if pr and pr.isdigit() else None,
    'source': os.environ['HARNESS_SOURCE'],
    'intent': os.environ['HARNESS_INTENT'],
    'request': os.environ['HARNESS_REQUEST'],
    'user': os.environ['HARNESS_USER'],
    'stage': os.environ['HARNESS_STAGE'],
    'status': 'running',
    'iteration': 0,
    'max_iterations': 5,
    'started_at': now,
    'updated_at': now,
    'ended_at': None,
    'error': None,
    'pid': int(os.environ['HARNESS_PID'])
}
with open(os.environ['HARNESS_SESSION_PATH'], 'w') as f:
    json.dump(d, f, indent=2)
PYEOF

  # Export so downstream scripts can update this session
  export HARNESS_SESSION_ID="$session_id"
  export HARNESS_SESSION_FILE="$session_file"
  echo "$session_id"
}

# Update session stage/status
# Usage: harness_update <field> <value>
# Fields: stage, status, iteration, error
harness_update() {
  local field="${1:?Field required}"
  local value="${2:?Value required}"
  local session_file="${HARNESS_SESSION_FILE:?No active session}"

  if [[ ! -f "$session_file" ]]; then
    echo "HARNESS: session file not found: $session_file" >&2
    return 1
  fi

  HARNESS_FILE="$session_file" HARNESS_FIELD="$field" HARNESS_VALUE="$value" \
  python3 <<'PYEOF'
import json, os
from datetime import datetime, timezone
path = os.environ['HARNESS_FILE']
field = os.environ['HARNESS_FIELD']
val = os.environ['HARNESS_VALUE']
with open(path) as f:
    d = json.load(f)
if field in ('iteration', 'max_iterations'):
    d[field] = int(val)
elif field == 'pr' and val != 'null':
    d[field] = int(val)
elif val == 'null':
    d[field] = None
else:
    d[field] = val
d['updated_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
tmp = f'{path}.{os.getpid()}.tmp'
with open(tmp, 'w') as f:
    json.dump(d, f, indent=2)
os.rename(tmp, path)
PYEOF
}

# Increment auto-fix iteration
harness_next_iteration() {
  local session_file="${HARNESS_SESSION_FILE:?No active session}"

  if [[ ! -f "$session_file" ]]; then
    echo "HARNESS: session file not found: $session_file" >&2
    return 1
  fi

  HARNESS_FILE="$session_file" \
  python3 <<'PYEOF'
import json, os
from datetime import datetime, timezone
path = os.environ['HARNESS_FILE']
with open(path) as f:
    d = json.load(f)
d['iteration'] += 1
if d['iteration'] >= d['max_iterations']:
    d['status'] = 'circuit-breaker'
d['updated_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
tmp = f'{path}.{os.getpid()}.tmp'
with open(tmp, 'w') as f:
    json.dump(d, f, indent=2)
os.rename(tmp, path)
print(d['iteration'])
PYEOF
}

# Close a session (success or failure)
# Usage: harness_close [status] [error_message]
harness_close() {
  local status="${1:-complete}"
  local error="${2:-}"
  local session_file="${HARNESS_SESSION_FILE:?No active session}"

  if [[ ! -f "$session_file" ]]; then
    echo "HARNESS: session file not found: $session_file" >&2
    unset HARNESS_SESSION_ID HARNESS_SESSION_FILE
    return 1
  fi

  HARNESS_FILE="$session_file" HARNESS_STATUS="$status" HARNESS_ERROR="$error" \
  python3 <<'PYEOF'
import json, os
from datetime import datetime, timezone
path = os.environ['HARNESS_FILE']
with open(path) as f:
    d = json.load(f)
d['status'] = os.environ['HARNESS_STATUS']
err = os.environ['HARNESS_ERROR']
d['error'] = err if err else None
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
d['updated_at'] = now
d['ended_at'] = now
tmp = f'{path}.{os.getpid()}.tmp'
with open(tmp, 'w') as f:
    json.dump(d, f, indent=2)
os.rename(tmp, path)
PYEOF

  unset HARNESS_SESSION_ID HARNESS_SESSION_FILE
}

# Clean up stale sessions (PID no longer running)
harness_gc() {
  local sessions_dir
  sessions_dir="$(git rev-parse --show-toplevel 2>/dev/null || pwd)/.harness/sessions"
  for f in "$sessions_dir"/*.json; do
    [[ -f "$f" ]] || continue
    local pid status
    pid=$(HARNESS_FILE="$f" python3 2>/dev/null <<'PYEOF' || echo "0"
import json, os
print(json.load(open(os.environ['HARNESS_FILE'])).get('pid', 0))
PYEOF
    )
    status=$(HARNESS_FILE="$f" python3 2>/dev/null <<'PYEOF' || echo "unknown"
import json, os
print(json.load(open(os.environ['HARNESS_FILE'])).get('status', 'unknown'))
PYEOF
    )

    # Validate pid is a positive integer before using kill
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || continue
    if [[ "$status" == "running" ]] && ! kill -0 "$pid" 2>/dev/null; then
      HARNESS_FILE="$f" HARNESS_PID="$pid" \
      python3 <<'PYEOF'
import json, os
from datetime import datetime, timezone
path = os.environ['HARNESS_FILE']
pid = os.environ['HARNESS_PID']
with open(path) as fh:
    d = json.load(fh)
d['status'] = 'stale'
d['error'] = f'Process {pid} no longer running'
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
d['updated_at'] = now
d['ended_at'] = now
tmp = f'{path}.{os.getpid()}.tmp'
with open(tmp, 'w') as fh:
    json.dump(d, fh, indent=2)
os.rename(tmp, path)
PYEOF
    fi
  done
}
