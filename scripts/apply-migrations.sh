#!/usr/bin/env bash
# Apply client-app migrations to this tenant's Supabase project via the
# Supabase Management API. Works from anywhere — no DB network reachability
# required. Canonical migration mechanism for HARNESS tenant deployments.
#
# Tracks applied migrations in a public.schema_migrations table so reruns
# are idempotent: already-applied files are skipped, only new files are
# applied. Each migration is applied in its own transaction so a failure
# in file N doesn't block diagnosis of file N+1.
#
# Requires:
#   SUPABASE_ACCESS_TOKEN — personal access token from
#                           https://supabase.com/dashboard/account/tokens
#   SUPABASE_PROJECT_REF  — project ref (20-char slug), defaults to this
#                           deployment's ref from harness.json
#   MIGRATIONS_DIR        — path to migrations dir, defaults to
#                           ../HARNESS/client-app/supabase/migrations
#                           relative to this script
#
# Usage:
#   export SUPABASE_ACCESS_TOKEN=sbp_...
#
#   # Normal: apply pending migrations
#   bash scripts/apply-migrations.sh
#
#   # First time on a DB where SOME migrations are applied but newer ones
#   # aren't: register migrations 00001..<filename> as applied without
#   # running them. Use this when migration tracking is being introduced
#   # mid-stream (existing tenants with applied schema but no
#   # schema_migrations table yet).
#   bash scripts/apply-migrations.sh --bootstrap-up-to 00010_payments_split_unique.sql
#
#   # Greenfield bootstrap — register ALL current files as applied without
#   # running them. ONLY safe if every current migration file has actually
#   # been applied to the DB.
#   bash scripts/apply-migrations.sh --bootstrap
#
#   # See what would be applied without actually applying
#   bash scripts/apply-migrations.sh --dry-run
#
# CONCURRENCY: Do not run this script in parallel against the same project.
# Migrations are not serialized at the script level; callers must guarantee
# single-instance execution (CI: use concurrency.group; humans: don't share a
# project ref).

set -euo pipefail

MODE="apply"
BOOTSTRAP_UP_TO=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bootstrap)
      MODE="bootstrap"
      shift
      ;;
    --bootstrap-up-to)
      MODE="bootstrap"
      BOOTSTRAP_UP_TO="${2:-}"
      if [[ -z "$BOOTSTRAP_UP_TO" ]]; then
        echo "ERROR: --bootstrap-up-to requires a filename argument"
        echo "       e.g., --bootstrap-up-to 00010_payments_split_unique.sql"
        exit 1
      fi
      shift 2
      ;;
    --dry-run)
      MODE="dry-run"
      shift
      ;;
    -h|--help)
      sed -n '2,44p' "$0"
      exit 0
      ;;
    *)
      echo "ERROR: unknown flag: $1"
      exit 1
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Validate a migration filename matches the safe shape. Filenames flow into
# SQL string literals and shell loops; enforce the pattern the inline
# comments assume rather than trusting the source dir.
assert_safe_filename() {
  local fname="$1"
  if [[ ! "$fname" =~ ^[0-9A-Za-z_.-]+\.sql$ ]]; then
    echo "ERROR: unsafe migration filename rejected: $fname"
    echo "       Filenames must match: ^[0-9A-Za-z_.-]+\\.sql$"
    exit 1
  fi
}

if [[ -z "${SUPABASE_ACCESS_TOKEN:-}" ]]; then
  echo "ERROR: SUPABASE_ACCESS_TOKEN is required."
  echo "Generate one at: https://supabase.com/dashboard/account/tokens"
  echo "Then: export SUPABASE_ACCESS_TOKEN=sbp_..."
  exit 1
fi

PROJECT_REF="${SUPABASE_PROJECT_REF:-$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['deployment']['supabase_project_ref'])" "$REPO_ROOT/harness.json")}"
if [[ ! "$PROJECT_REF" =~ ^[a-z0-9]{20}$ ]]; then
  echo "ERROR: invalid SUPABASE_PROJECT_REF format: $PROJECT_REF"
  echo "       Expected 20 lowercase alphanumeric chars (Supabase project ref shape)."
  exit 1
fi
MIGRATIONS_DIR="${MIGRATIONS_DIR:-$REPO_ROOT/../HARNESS/client-app/supabase/migrations}"

if [[ ! -d "$MIGRATIONS_DIR" ]]; then
  echo "ERROR: migrations dir not found: $MIGRATIONS_DIR"
  echo "Set MIGRATIONS_DIR to override, e.g.:"
  echo "  MIGRATIONS_DIR=/path/to/HARNESS/client-app/supabase/migrations bash $0"
  exit 1
fi

API_URL="https://api.supabase.com/v1/projects/${PROJECT_REF}/database/query"

echo "Project:      $PROJECT_REF"
echo "Migrations:   $MIGRATIONS_DIR"
echo "Mode:         $MODE"
echo ""

# Run a single SQL statement (or batch) against the Management API.
# Args:
#   $1 — SQL to execute
#   $2 — human label for error messages
# Echoes the response body; exits non-zero on HTTP failure.
run_sql() {
  local sql="$1"
  local label="$2"
  local body
  body=$(python3 -c "import json,sys; print(json.dumps({'query': sys.argv[1]}))" "$sql")
  local response
  response=$(curl -sS \
    --fail-with-body \
    -X POST "$API_URL" \
    -H "Authorization: Bearer ${SUPABASE_ACCESS_TOKEN}" \
    -H "Content-Type: application/json" \
    --data "$body" \
    2>&1) || {
    echo "ERROR ($label): Management API call failed."
    echo "$response"
    exit 1
  }
  # Defense in depth: catch HTTP-200-with-error-body. Supabase Management API
  # normally returns 4xx for SQL errors (caught by --fail-with-body above),
  # but probe for an error/message key as a safety net.
  local probe
  probe=$(printf '%s' "$response" | python3 -c "
import json, sys
try:
    data = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
if isinstance(data, dict):
    for key in ('error', 'message'):
        if key in data and data[key]:
            print(f'API_ERROR: {key}={data[key]}')
            sys.exit(1)
" 2>&1) || {
    echo "ERROR ($label): API returned a non-error HTTP status but the response body contains an error key."
    echo "$probe"
    echo "$response"
    exit 1
  }
  echo "$response"
}

# Ensure schema_migrations exists. CREATE TABLE IF NOT EXISTS is idempotent.
ensure_tracking_table() {
  run_sql \
    "CREATE TABLE IF NOT EXISTS public.schema_migrations (filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now());" \
    "ensure schema_migrations table" \
    >/dev/null
}

# Read list of applied migration filenames from schema_migrations.
list_applied() {
  local response
  response=$(run_sql \
    "SELECT filename FROM public.schema_migrations ORDER BY filename;" \
    "list applied migrations")
  python3 -c "
import json, sys
data = json.loads(sys.argv[1])
# Management API returns a list of rows when SELECT returns rows
if isinstance(data, list):
    for row in data:
        # row may be {'filename': '00001_initial_schema.sql'} or similar
        if isinstance(row, dict) and 'filename' in row:
            print(row['filename'])
" "$response"
}

# Record a migration as applied (called after a successful per-file apply).
record_applied() {
  local filename="$1"
  assert_safe_filename "$filename"
  # Use parameterized-safe value via printf %q-style escaping; the filename
  # is constrained to NN_name.sql shapes so this is safe.
  run_sql \
    "INSERT INTO public.schema_migrations (filename) VALUES ('${filename}') ON CONFLICT (filename) DO NOTHING;" \
    "record ${filename}" \
    >/dev/null
}

# CONCURRENCY: This script does not serialize concurrent invocations against
# the same Supabase project. If two CI jobs run simultaneously and both see
# the same file as pending, both will execute it. Migrations should be
# idempotent (CREATE TABLE IF NOT EXISTS, ALTER TABLE ADD COLUMN IF NOT EXISTS)
# OR callers must serialize themselves (e.g., GitHub Actions concurrency: group).
# A pg_advisory_xact_lock approach was considered but each Management API
# POST is a separate session, so session-locks do not span the pending-list
# read and the per-file apply. Pre-claiming the tracking row would break the
# current single-transaction atomicity guarantee. Document instead of work
# around.
#
# Apply a single migration file in its own transaction.
# Args: $1 = path to .sql file
apply_one() {
  local file="$1"
  local fname
  fname=$(basename "$file")
  assert_safe_filename "$fname"
  local content
  content=$(cat "$file")
  # Wrap in BEGIN/COMMIT so this file's statements are atomic. If the
  # caller's content already has its own BEGIN/COMMIT (like 00011), the
  # outer transaction will conflict — strip leading BEGIN; / trailing COMMIT;
  # if they're present at the start/end of the file (whitespace-tolerant).
  local stripped
  stripped=$(printf '%s\n' "$content" | python3 -c "
import re, sys
text = sys.stdin.read()
lines = text.split('\n')
# Strip leading BEGIN / BEGIN TRANSACTION / BEGIN ISOLATION LEVEL ...
# (allow blank lines, -- line comments, and /* */ block comments before it)
i = 0
in_block = False
while i < len(lines):
    s = lines[i].strip()
    if in_block:
        if '*/' in s:
            in_block = False
        i += 1
        continue
    if s == '' or s.startswith('--'):
        i += 1
        continue
    if s.startswith('/*'):
        # Single-line /* ... */ vs multi-line opener
        if '*/' in s[2:]:
            i += 1
            continue
        in_block = True
        i += 1
        continue
    break
if i < len(lines) and re.match(r'^BEGIN\b', lines[i].strip().upper()):
    lines[i] = ''
# Strip trailing COMMIT; or END; (Postgres synonym for COMMIT)
# Mirror the leading skip: tolerate blank lines, -- comments, and /* */ blocks.
# Scanning bottom-up, a block comment is identified by '*/' on a line (close)
# and we continue past lines until we consume the matching '/*' opener.
j = len(lines) - 1
in_block = False
while j >= 0:
    s = lines[j].strip()
    if in_block:
        if '/*' in s:
            in_block = False
        j -= 1
        continue
    if s == '' or s.startswith('--'):
        j -= 1
        continue
    if s.endswith('*/'):
        # Single-line /* ... */ vs multi-line closer
        # Check if '/*' appears on the same line BEFORE the trailing '*/'.
        if '/*' in s[:-2]:
            j -= 1
            continue
        in_block = True
        j -= 1
        continue
    break
if j >= 0 and re.match(r'^(COMMIT|END)\b', lines[j].strip().upper()):
    lines[j] = ''
print('\n'.join(lines))
")
  # Include the schema_migrations INSERT inside the same transaction so the
  # migration and its tracking row commit atomically — prevents the failure
  # mode where the migration lands but record_applied is never called.
  local wrapped
  wrapped="BEGIN;
${stripped}
INSERT INTO public.schema_migrations (filename) VALUES ('${fname}') ON CONFLICT (filename) DO NOTHING;
COMMIT;"
  run_sql "$wrapped" "apply ${fname}" >/dev/null
}

# --- Mode dispatch ---

# Don't create the tracking table in dry-run — that mode must be read-only.
[[ "$MODE" != "dry-run" ]] && ensure_tracking_table

mapfile -t ALL_FILES < <(ls -1 "$MIGRATIONS_DIR"/*.sql 2>/dev/null | sort)
if [[ ${#ALL_FILES[@]} -eq 0 ]]; then
  echo "No migration files found in $MIGRATIONS_DIR — nothing to do."
  exit 0
fi

# Validate every discovered filename before any further processing — catches
# hostile filenames even for files that won't be applied this run.
for _f in "${ALL_FILES[@]}"; do
  assert_safe_filename "$(basename "$_f")"
done

# Capture list_applied output explicitly so a failure (network blip, auth
# expiry, table missing) is caught before APPLIED is populated. If mapfile
# were fed from a failed process substitution directly, it would silently
# produce an empty array and cause every migration to be re-applied.
if [[ "$MODE" == "dry-run" ]]; then
  # Keep dry-run non-fatal but make any degradation visible. If
  # schema_migrations is missing or the API errors transiently, the pending
  # list below would otherwise misleadingly include already-applied files —
  # surface a WARNING so the operator can decide whether to trust the output.
  local_err=$(mktemp)
  if ! applied_output=$(list_applied 2>"$local_err"); then
    echo "WARNING: could not read schema_migrations (table missing or API error). Pending list below may be inaccurate." >&2
    if [[ -s "$local_err" ]]; then
      sed 's/^/  detail: /' "$local_err" >&2
    fi
    applied_output=""
  fi
  rm -f "$local_err"
else
  if ! applied_output=$(list_applied); then
    echo "ERROR: failed to read applied-migrations list — aborting to prevent re-apply."
    exit 1
  fi
fi
APPLIED=()
while IFS= read -r _line; do
  [[ -n "$_line" ]] && APPLIED+=("$_line")
done <<<"$applied_output"

# Compute pending = ALL_FILES minus APPLIED (by basename match)
PENDING=()
for f in "${ALL_FILES[@]}"; do
  fname=$(basename "$f")
  found=0
  for a in "${APPLIED[@]:-}"; do
    if [[ "$a" == "$fname" ]]; then found=1; break; fi
  done
  if [[ $found -eq 0 ]]; then PENDING+=("$f"); fi
done

case "$MODE" in
  bootstrap)
    # If --bootstrap-up-to <filename> was specified, only mark migrations
    # whose basename is <= that filename (lexicographic) as applied. This
    # is the safe path when an existing DB has migrations 00001..N applied
    # and N+1..M still pending. Without --bootstrap-up-to, bootstrap marks
    # ALL pending files as applied — use ONLY on greenfield setups where
    # every current migration file has actually been applied.
    BOOTSTRAP_SET=()
    if [[ -n "$BOOTSTRAP_UP_TO" ]]; then
      # Verify the named file exists in pending list
      target_found=0
      for f in "${PENDING[@]}"; do
        if [[ "$(basename "$f")" == "$BOOTSTRAP_UP_TO" ]]; then
          target_found=1
          break
        fi
      done
      if [[ $target_found -eq 0 ]]; then
        # Check whether the target exists in ALL_FILES (already applied) vs truly missing.
        for f in "${ALL_FILES[@]}"; do
          if [[ "$(basename "$f")" == "$BOOTSTRAP_UP_TO" ]]; then
            echo "Note: --bootstrap-up-to target is already recorded as applied: $BOOTSTRAP_UP_TO"
            echo "Nothing to bootstrap. Run without --bootstrap-up-to to see pending state."
            exit 0
          fi
        done
        echo "ERROR: --bootstrap-up-to target not found in migrations dir: $BOOTSTRAP_UP_TO"
        echo "       Available files in $MIGRATIONS_DIR:"
        for f in "${ALL_FILES[@]}"; do echo "         $(basename "$f")"; done
        exit 1
      fi
      # Collect pending files up to and including BOOTSTRAP_UP_TO (sorted order)
      for f in "${PENDING[@]}"; do
        fname=$(basename "$f")
        BOOTSTRAP_SET+=("$f")
        if [[ "$fname" == "$BOOTSTRAP_UP_TO" ]]; then break; fi
      done
    else
      BOOTSTRAP_SET=("${PENDING[@]}")
    fi

    if [[ ${#BOOTSTRAP_SET[@]} -eq 0 ]]; then
      echo "Nothing to bootstrap."
      exit 0
    fi
    echo "Bootstrap: registering ${#BOOTSTRAP_SET[@]} migration file(s) as already-applied (no SQL executed):"
    for f in "${BOOTSTRAP_SET[@]}"; do
      fname=$(basename "$f")
      echo "  + $fname"
      record_applied "$fname"
    done
    echo ""
    echo "Done. Future runs without --bootstrap* will apply only NEW migration files."
    ;;
  dry-run)
    if [[ ${#PENDING[@]} -eq 0 ]]; then
      echo "No pending migrations. Database is up to date."
      exit 0
    fi
    echo "Would apply ${#PENDING[@]} pending migration(s):"
    for f in "${PENDING[@]}"; do
      echo "  - $(basename "$f")"
    done
    ;;
  apply)
    if [[ ${#PENDING[@]} -eq 0 ]]; then
      echo "No pending migrations. Database is up to date."
      exit 0
    fi
    echo "Applying ${#PENDING[@]} pending migration(s):"
    for f in "${PENDING[@]}"; do
      fname=$(basename "$f")
      echo -n "  - $fname ... "
      apply_one "$f"
      echo "OK"
    done
    echo ""
    echo "All pending migrations applied successfully."
    ;;
esac

if [[ "$MODE" != "dry-run" ]]; then
  echo ""
  echo "Verifying applied list:"
  applied_after=$(run_sql \
    "SELECT filename, applied_at FROM public.schema_migrations ORDER BY filename;" \
    "verify applied list")
  echo "$applied_after" | python3 -m json.tool 2>/dev/null || echo "$applied_after"
fi
