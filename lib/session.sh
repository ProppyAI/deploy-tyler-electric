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
  local subcategory="${6:-}"
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
  HARNESS_SUBCAT="$subcategory" \
  HARNESS_MANIFEST="$REPO_ROOT/contracts/chain-manifest.json" \
  python3 <<'PYEOF'
import json, os, sys
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
# Chain population: only when intent=feature, subcategory given, and manifest exists
subcat = os.environ.get('HARNESS_SUBCAT', '')
manifest_path = os.environ.get('HARNESS_MANIFEST', '')
if subcat and d.get('intent') == 'feature' and os.path.exists(manifest_path):
    with open(manifest_path) as mf:
        manifest = json.load(mf)
    key = f'feature:{subcat}'
    if key in manifest.get('chains', {}):
        d['subcategory'] = subcat
        d['chain'] = manifest['chains'][key]
        d['stages_completed'] = []
    elif subcat:
        sys.stderr.write(f'HARNESS: unknown subcategory "{subcat}" for intent "feature"; chain not populated\n')
tmp = f"{os.environ['HARNESS_SESSION_PATH']}.{os.getpid()}.tmp"
with open(tmp, 'w') as f:
    json.dump(d, f, indent=2)
os.rename(tmp, os.environ['HARNESS_SESSION_PATH'])
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

# Record completion of a chain stage with order validation + idempotency.
# Usage: harness_record_stage <stage_name>
# Behavior:
#   - If session status == "failed": exits nonzero immediately (cannot record into a failed session).
#   - If session has no `chain` field (legacy): update `stage` only, no append.
#   - If last completed == stage_name: no-op (idempotent).
#   - If chain[stages_completed.length] == stage_name: append + advance.
#   - Else: set status=failed with reason (out-of-order or overrun).
harness_record_stage() {
  local stage_name="${1:?Stage name required}"
  local session_file="${HARNESS_SESSION_FILE:?No active session}"
  local repo_root manifest_path
  repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
  manifest_path="$repo_root/contracts/chain-manifest.json"

  if [[ ! -f "$session_file" ]]; then
    echo "HARNESS: session file not found: $session_file" >&2
    return 1
  fi

  HARNESS_FILE="$session_file" HARNESS_STAGE="$stage_name" \
  HARNESS_MANIFEST="$manifest_path" \
  python3 <<'PYEOF'
import json, os, sys
from datetime import datetime, timezone
path = os.environ['HARNESS_FILE']
stage = os.environ['HARNESS_STAGE']
manifest_path = os.environ.get('HARNESS_MANIFEST', '')
with open(path) as f:
    d = json.load(f)
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

if d.get('status') == 'failed':
    sys.stderr.write(f"HARNESS: session already failed ({d.get('error', 'unknown')}); cannot record stage\n")
    sys.exit(1)

# Build advisory set from manifest (stages with advisory: true)
advisory_set = set()
if manifest_path and os.path.exists(manifest_path):
    with open(manifest_path) as mf:
        manifest = json.load(mf)
    for step_name, step_meta in manifest.get('stages', {}).items():
        if isinstance(step_meta, dict) and step_meta.get('advisory'):
            advisory_set.add(step_name)

chain = d.get('chain')
if not chain:
    # Legacy session without chain; just update the stage field
    d['stage'] = stage
    d['updated_at'] = now
else:
    completed = d.get('stages_completed', [])
    if completed and completed[-1] == stage:
        # Idempotent re-record
        d['updated_at'] = now
    else:
        idx = len(completed)
        # define-better invariant: cannot advance past define-metric until metric_sha set.
        # Additive: legacy chains without 'define-metric' skip the check entirely.
        if 'define-metric' in chain:
            dm_idx = chain.index('define-metric')
            if idx > dm_idx and not d.get('metric_sha'):
                d['status'] = 'failed'
                d['error'] = f'metric_sha not set; cannot advance past define-metric (stage={stage})'
                d['updated_at'] = now
                tmp = f'{path}.{os.getpid()}.tmp'
                with open(tmp, 'w') as f:
                    json.dump(d, f, indent=2)
                os.rename(tmp, path)
                sys.exit(0)
        if idx >= len(chain):
            d['status'] = 'failed'
            d['error'] = f'chain overrun: all {len(chain)} stages already complete, got {stage}'
            d['updated_at'] = now
        else:
            # Auto-advance past advisory steps that precede the requested stage
            while idx < len(chain) and chain[idx] != stage and chain[idx] in advisory_set:
                completed.append(chain[idx])
                idx += 1
            if idx >= len(chain):
                d['status'] = 'failed'
                d['error'] = f'chain overrun: all {len(chain)} stages already complete, got {stage}'
                d['updated_at'] = now
            elif chain[idx] != stage:
                d['status'] = 'failed'
                d['error'] = f'chain out-of-order: expected {chain[idx]}, got {stage}'
                d['updated_at'] = now
            else:
                completed.append(stage)
                d['stages_completed'] = completed
                d['stage'] = stage
                d['updated_at'] = now

tmp = f'{path}.{os.getpid()}.tmp'
with open(tmp, 'w') as f:
    json.dump(d, f, indent=2)
os.rename(tmp, path)
PYEOF
}

# Seed chain[] on an existing session from contracts/chain-manifest.json.
# Usage: harness_upgrade_chain <subcategory>   (where subcategory ∈ ui|data|infra|migration)
# Operator-initiated. Does NOT auto-populate stages_completed; operator can manually
# seed via repeated harness_record_stage calls if stages already completed.
harness_upgrade_chain() {
  local subcategory="${1:?Subcategory required (ui|data|infra|migration)}"
  local session_file="${HARNESS_SESSION_FILE:?No active session}"
  local repo_root manifest_path
  repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
  manifest_path="$repo_root/contracts/chain-manifest.json"

  if [[ ! -f "$session_file" ]]; then
    echo "HARNESS: session file not found: $session_file" >&2
    return 1
  fi
  if [[ ! -f "$manifest_path" ]]; then
    echo "HARNESS: chain manifest not found: $manifest_path" >&2
    return 1
  fi

  HARNESS_FILE="$session_file" HARNESS_MANIFEST="$manifest_path" \
  HARNESS_SUBCAT="$subcategory" \
  python3 <<'PYEOF'
import json, os, sys
from datetime import datetime, timezone
session_path = os.environ['HARNESS_FILE']
manifest_path = os.environ['HARNESS_MANIFEST']
subcat = os.environ['HARNESS_SUBCAT']
with open(manifest_path) as f:
    manifest = json.load(f)
key = f'feature:{subcat}'
if key not in manifest.get('chains', {}):
    sys.stderr.write(f'HARNESS: unknown subcategory: {subcat}\n')
    sys.exit(1)
with open(session_path) as f:
    d = json.load(f)
d['subcategory'] = subcat
d['chain'] = manifest['chains'][key]
d['stages_completed'] = []  # always reset on chain change
d['updated_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
tmp = f'{session_path}.{os.getpid()}.tmp'
with open(tmp, 'w') as f:
    json.dump(d, f, indent=2)
os.rename(tmp, session_path)
PYEOF
}

# Verify the metric file's blob sha matches metric_sha pinned in the session.
# Usage: harness_metric_verify <session_id>
# Exit 0 = match, exit 1 = mismatch or missing.
harness_metric_verify() {
  local session_id="${1:?Session ID required}"
  local repo_root session_file
  repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
  session_file="$repo_root/.harness/sessions/${session_id}.json"
  [[ -f "$session_file" ]] || { echo "HARNESS: session not found: $session_file" >&2; return 1; }

  HARNESS_FILE="$session_file" HARNESS_REPO="$repo_root" python3 <<'PYEOF'
import json, os, subprocess, sys
d = json.load(open(os.environ['HARNESS_FILE']))
mf = d.get('metric_file')
ms = d.get('metric_sha')
if not mf or not ms:
    sys.stderr.write('HARNESS: metric_file or metric_sha not set\n')
    sys.exit(1)
path = os.path.join(os.environ['HARNESS_REPO'], mf)
if not os.path.exists(path):
    sys.stderr.write(f'HARNESS: metric file missing on disk: {path}\n')
    sys.exit(1)
actual = subprocess.check_output(['git', 'hash-object', path], cwd=os.environ['HARNESS_REPO']).decode().strip()
if actual != ms:
    sys.stderr.write(f'HARNESS: sha mismatch: pinned={ms} current={actual}\n')
    sys.exit(1)
print('OK')
PYEOF
}

# Approve the drafted metric: rewrite the Approved-by line into a durable
# certificate, then pin the post-rewrite sha, set status=running, append
# define-metric. The certificate replaces `<pending operator approval...>` with
# `<git config user.name> at <iso8601 UTC>`. The sha lives in session JSON only;
# the metric file does NOT carry the sha (avoids chicken-and-egg with hashing).
# Usage: harness_metric_approve <session_id>
harness_metric_approve() {
  local session_id="${1:?Session ID required}"
  local repo_root session_file user
  repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
  session_file="$repo_root/.harness/sessions/${session_id}.json"
  [[ -f "$session_file" ]] || { echo "HARNESS: session not found: $session_file" >&2; return 1; }
  user=$(git config user.name 2>/dev/null || echo "unknown")

  HARNESS_FILE="$session_file" HARNESS_REPO="$repo_root" HARNESS_USER="$user" python3 <<'PYEOF'
import json, os, re, subprocess, sys
from datetime import datetime, timezone
d = json.load(open(os.environ['HARNESS_FILE']))
mf = d.get('metric_file') or f"docs/superpowers/metrics/{d['session_id']}.md"
path = os.path.join(os.environ['HARNESS_REPO'], mf)
if not os.path.exists(path):
    sys.stderr.write(f'HARNESS: metric file not on disk: {path}\n')
    sys.exit(1)
content = open(path).read()
# Idempotency guard: refuse if Approved-by line is already a certificate.
cert_pat = re.compile(r'^\*\*Approved by:\*\*\s+.+? at \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\s*$', re.M)
if cert_pat.search(content):
    sys.stderr.write('HARNESS: metric file already has approval certificate — refusing to double-rewrite\n')
    sys.exit(1)
# Placeholder match: <anything in angle brackets> on the Approved-by line.
ph_pat = re.compile(r'^\*\*Approved by:\*\*\s+<[^>]*>\s*$', re.M)
matches = ph_pat.findall(content)
if len(matches) != 1:
    sys.stderr.write(f'HARNESS: metric file Approved-by line not in expected pending-placeholder shape (matched {len(matches)} lines)\n')
    sys.exit(1)
user = os.environ['HARNESS_USER']
iso = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
certificate = f'**Approved by:** {user} at {iso}'
new_content = ph_pat.sub(lambda m: certificate, content, count=1)
# Atomic write: tmp + rename.
tmp_md = path + '.tmp'
try:
    with open(tmp_md, 'w') as f:
        f.write(new_content)
    os.rename(tmp_md, path)
except Exception:
    if os.path.exists(tmp_md):
        os.remove(tmp_md)
    raise
# Now hash the rewritten file.
sha = subprocess.check_output(['git', 'hash-object', path], cwd=os.environ['HARNESS_REPO']).decode().strip()
d['metric_file'] = mf
d['metric_sha'] = sha
d['status'] = 'running'
# Append define-metric to stages_completed if chain is set and not already last
if 'chain' in d:
    sc = d.get('stages_completed', [])
    if not sc or sc[-1] != 'define-metric':
        sc.append('define-metric')
        d['stages_completed'] = sc
    d['stage'] = 'define-metric'
d['updated_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
tmp = os.environ['HARNESS_FILE'] + '.tmp'
with open(tmp, 'w') as f:
    json.dump(d, f, indent=2)
os.rename(tmp, os.environ['HARNESS_FILE'])
print(f'METRIC APPROVED: {sha}')
PYEOF
}

# Reject the drafted metric: status=needs-human; do NOT append define-metric.
# Usage: harness_metric_reject <session_id> <reason>
harness_metric_reject() {
  local session_id="${1:?Session ID required}"
  local reason="${2:?Reason required}"
  local repo_root session_file
  repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
  session_file="$repo_root/.harness/sessions/${session_id}.json"
  [[ -f "$session_file" ]] || { echo "HARNESS: session not found: $session_file" >&2; return 1; }

  HARNESS_FILE="$session_file" HARNESS_REASON="$reason" python3 <<'PYEOF'
import json, os, sys
from datetime import datetime, timezone
d = json.load(open(os.environ['HARNESS_FILE']))
d['status'] = 'needs-human'
d['error'] = f"metric rejected: {os.environ['HARNESS_REASON']}"
d['updated_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
tmp = os.environ['HARNESS_FILE'] + '.tmp'
with open(tmp, 'w') as f:
    json.dump(d, f, indent=2)
os.rename(tmp, os.environ['HARNESS_FILE'])
print('METRIC REJECTED')
PYEOF
}

# Generate the trace file (helper used by trace-audit skill / harness trace generate).
# Implementation note: this helper writes session.trace_file but DOES NOT generate the
# file content — that is the skill's responsibility. The helper just records the
# pointer once the file is on disk.
# Usage: harness_trace_generate <session_id>
harness_trace_generate() {
  local session_id="${1:?Session ID required}"
  local repo_root session_file trace_path
  repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
  session_file="$repo_root/.harness/sessions/${session_id}.json"
  trace_path="docs/superpowers/traces/${session_id}.md"
  [[ -f "$session_file" ]] || { echo "HARNESS: session not found: $session_file" >&2; return 1; }
  [[ -f "$repo_root/$trace_path" ]] || { echo "HARNESS: trace file not on disk: $repo_root/$trace_path" >&2; return 1; }

  HARNESS_FILE="$session_file" HARNESS_TRACE="$trace_path" python3 <<'PYEOF'
import json, os
from datetime import datetime, timezone
d = json.load(open(os.environ['HARNESS_FILE']))
d['trace_file'] = os.environ['HARNESS_TRACE']
d['updated_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
tmp = os.environ['HARNESS_FILE'] + '.tmp'
with open(tmp, 'w') as f:
    json.dump(d, f, indent=2)
os.rename(tmp, os.environ['HARNESS_FILE'])
print('TRACE RECORDED')
PYEOF
}

# Verify the trace file exists, ends with a Verdict line, and reports PASS.
# Usage: harness_trace_verify <session_id>
# Exit 0 = PASS, exit 1 = FAIL/NEEDS-HUMAN/missing/not-tracked/missing-section.
harness_trace_verify() {
  local session_id="${1:?Session ID required}"
  local repo_root session_file
  repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
  session_file="$repo_root/.harness/sessions/${session_id}.json"
  [[ -f "$session_file" ]] || { echo "HARNESS: session not found: $session_file" >&2; return 1; }

  HARNESS_FILE="$session_file" HARNESS_REPO="$repo_root" python3 <<'PYEOF'
import json, os, sys, re, subprocess
d = json.load(open(os.environ['HARNESS_FILE']))
tf = d.get('trace_file')
if not tf:
    sys.stderr.write('HARNESS: trace_file not set\n')
    sys.exit(1)
repo = os.environ['HARNESS_REPO']
path = os.path.join(repo, tf)
if not os.path.exists(path):
    sys.stderr.write(f'HARNESS: trace file missing: {path}\n')
    sys.exit(1)
# Git-tracked check: file must be staged (git add) before commit; this catches
# the case where the trace was written but never added to git, leaving it
# unauditable across machines and absent from the PR diff.
try:
    subprocess.run(
        ['git', 'ls-files', '--error-unmatch', '--', tf],
        cwd=repo, check=True, capture_output=True,
    )
except subprocess.CalledProcessError:
    sys.stderr.write(f'HARNESS: trace file not tracked in git: {tf}\n')
    sys.exit(1)
contents = open(path).read()
# Required-section presence check (order is enforced by trace-template, not here).
required_sections = [
    '## Per-stage evidence',
    '## Metric verification',
    '## Gaming pre-mortem cross-check',
    '## Silent skips',
    '## Verdict',
]
for section in required_sections:
    if section not in contents:
        sys.stderr.write(f'HARNESS: trace file missing required section: {section}\n')
        sys.exit(1)
# Look for "## Verdict" section terminating in PASS|NEEDS-HUMAN|FAIL
m = re.search(r'## Verdict\s*\n+\s*(PASS|NEEDS-HUMAN|FAIL)', contents)
if not m:
    sys.stderr.write('HARNESS: no Verdict section found\n')
    sys.exit(1)
verdict = m.group(1)
if verdict != 'PASS':
    sys.stderr.write(f'HARNESS: trace verdict is {verdict}\n')
    sys.exit(1)
print('TRACE PASS')
PYEOF
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
