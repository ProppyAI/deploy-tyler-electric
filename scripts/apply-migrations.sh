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
      sed -n '2,40p' "$0"
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

if [[ -z "${SUPABASE_ACCESS_TOKEN:-}" ]]; then
  echo "ERROR: SUPABASE_ACCESS_TOKEN is required."
  echo "Generate one at: https://supabase.com/dashboard/account/tokens"
  echo "Then: export SUPABASE_ACCESS_TOKEN=sbp_..."
  exit 1
fi

PROJECT_REF="${SUPABASE_PROJECT_REF:-$(python3 -c "import json; print(json.load(open('$REPO_ROOT/harness.json'))['deployment']['supabase_project_ref'])")}"
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
  # Use parameterized-safe value via printf %q-style escaping; the filename
  # is constrained to NN_name.sql shapes so this is safe.
  run_sql \
    "INSERT INTO public.schema_migrations (filename) VALUES ('${filename}') ON CONFLICT (filename) DO NOTHING;" \
    "record ${filename}" \
    >/dev/null
}

# Apply a single migration file in its own transaction.
# Args: $1 = path to .sql file
apply_one() {
  local file="$1"
  local fname
  fname=$(basename "$file")
  local content
  content=$(cat "$file")
  # Wrap in BEGIN/COMMIT so this file's statements are atomic. If the
  # caller's content already has its own BEGIN/COMMIT (like 00011), the
  # outer transaction will conflict — strip leading BEGIN; / trailing COMMIT;
  # if they're present at the start/end of the file (whitespace-tolerant).
  local stripped
  stripped=$(printf '%s\n' "$content" | python3 -c "
import sys
text = sys.stdin.read()
lines = text.split('\n')
# Strip leading BEGIN; (allow trailing whitespace + comments before it)
i = 0
while i < len(lines) and (lines[i].strip() == '' or lines[i].strip().startswith('--')):
    i += 1
if i < len(lines) and lines[i].strip().upper().rstrip(';').strip() == 'BEGIN':
    lines[i] = ''
# Strip trailing COMMIT;
j = len(lines) - 1
while j >= 0 and (lines[j].strip() == '' or lines[j].strip().startswith('--')):
    j -= 1
if j >= 0 and lines[j].strip().upper().rstrip(';').strip() == 'COMMIT':
    lines[j] = ''
print('\n'.join(lines))
")
  local wrapped
  wrapped="BEGIN;
${stripped}
COMMIT;"
  run_sql "$wrapped" "apply ${fname}" >/dev/null
}

# --- Mode dispatch ---

ensure_tracking_table

mapfile -t ALL_FILES < <(ls -1 "$MIGRATIONS_DIR"/*.sql 2>/dev/null | sort)
if [[ ${#ALL_FILES[@]} -eq 0 ]]; then
  echo "No migration files found in $MIGRATIONS_DIR — nothing to do."
  exit 0
fi

mapfile -t APPLIED < <(list_applied)

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
        echo "ERROR: --bootstrap-up-to target not in pending list: $BOOTSTRAP_UP_TO"
        echo "       Pending files:"
        for f in "${PENDING[@]}"; do echo "         $(basename "$f")"; done
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
      record_applied "$fname"
      echo "OK"
    done
    echo ""
    echo "All pending migrations applied successfully."
    ;;
esac

echo ""
echo "Verifying applied list:"
applied_after=$(run_sql \
  "SELECT filename, applied_at FROM public.schema_migrations ORDER BY filename;" \
  "verify applied list")
echo "$applied_after" | python3 -m json.tool 2>/dev/null || echo "$applied_after"
