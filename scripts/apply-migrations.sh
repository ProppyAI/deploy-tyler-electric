#!/usr/bin/env bash
# apply-migrations.sh — HARNESS canonical migration mechanism for tenant
# Supabase projects. Applies client-app migrations via the Supabase
# Management API (no DB network reachability required).
#
# Tracks applied migrations in a public.schema_migrations table so reruns
# are idempotent: already-applied files are skipped, only new files are
# applied. Each migration applies in its own transaction so a failure
# in file N doesn't block diagnosis of file N+1.
#
# CANONICAL LOCATION (Phase 3 relocation, 2026-05-14):
#   This script lives in HARNESS/scripts/ — NOT in any tenant repo. Per the
#   HARNESS operating principle in CLAUDE.md, all tooling that operates
#   on tenants is owned here and `harness update-tenant` is the front door
#   for routine deploys. Tenant-repo copies are deprecated.
#
# REQUIRED ENV (auto-sourced from ~/.harness/operator.env):
#   SUPABASE_ACCESS_TOKEN  — Supabase PAT (sbp_*). Aliased from
#                            SUPABASE_MANAGEMENT_PAT if that name is set
#                            in operator.env instead (back-compat).
#
# REQUIRED ARGS:
#   --deploy-path PATH     — absolute path to the tenant deploy repo (the
#                            one with harness.json). Read for
#                            deployment.supabase_project_ref unless
#                            SUPABASE_PROJECT_REF is set.
#
# OPTIONAL ENV:
#   SUPABASE_PROJECT_REF   — override the project ref from harness.json
#   MIGRATIONS_DIR         — path to migrations dir, defaults to
#                            HARNESS/client-app/supabase/migrations
#                            relative to this script
#   HARNESS_OPERATOR_ENV   — override the operator.env path
#                            (default: ~/.harness/operator.env)
#
# MODES (mutually exclusive):
#   (default)              — Apply pending migrations
#   --bootstrap            — Greenfield: register ALL current files as applied
#                            without running them. ONLY safe if every current
#                            migration file has actually been applied.
#   --bootstrap-up-to FILE — Register migrations 00001..FILE as applied
#                            without running them. Use when schema_migrations
#                            is being introduced mid-stream.
#   --dry-run              — Show what would be applied without applying
#
# Usage:
#   bash $HARNESS/scripts/apply-migrations.sh --deploy-path /path/to/tenant
#
# Or via the harness CLI (preferred):
#   harness update-tenant tyler-electric
#
# CONCURRENCY: Do not run this script in parallel against the same project.
# Migrations are not serialized at the script level; callers must guarantee
# single-instance execution. The harness CLI dispatcher serializes via the
# session lock contract.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Source operator.env (sets SUPABASE_ACCESS_TOKEN, applies aliases).
# shellcheck source=../lib/operator-env.sh
source "$HARNESS_ROOT/lib/operator-env.sh"

# --- Arg parsing ------------------------------------------------------------

MODE="apply"
BOOTSTRAP_UP_TO=""
DEPLOY_PATH=""
MODE_SET_BY=""   # tracks which flag set MODE (for mutex error message)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --deploy-path)
      DEPLOY_PATH="${2:-}"
      if [[ -z "$DEPLOY_PATH" ]]; then
        echo "ERROR: --deploy-path requires a path argument" >&2
        exit 1
      fi
      shift 2
      ;;
    --bootstrap)
      if [[ -n "$MODE_SET_BY" ]]; then
        echo "ERROR: --bootstrap conflicts with already-set mode flag: $MODE_SET_BY" >&2
        echo "       Modes (--bootstrap, --bootstrap-up-to, --dry-run) are mutually exclusive." >&2
        exit 1
      fi
      MODE="bootstrap"
      MODE_SET_BY="--bootstrap"
      shift
      ;;
    --bootstrap-up-to)
      if [[ -n "$MODE_SET_BY" ]]; then
        echo "ERROR: --bootstrap-up-to conflicts with already-set mode flag: $MODE_SET_BY" >&2
        echo "       Modes (--bootstrap, --bootstrap-up-to, --dry-run) are mutually exclusive." >&2
        exit 1
      fi
      MODE="bootstrap"
      BOOTSTRAP_UP_TO="${2:-}"
      MODE_SET_BY="--bootstrap-up-to"
      if [[ -z "$BOOTSTRAP_UP_TO" ]]; then
        echo "ERROR: --bootstrap-up-to requires a filename argument" >&2
        echo "       e.g., --bootstrap-up-to 00010_payments_split_unique.sql" >&2
        exit 1
      fi
      shift 2
      ;;
    --dry-run)
      if [[ -n "$MODE_SET_BY" ]]; then
        echo "ERROR: --dry-run conflicts with already-set mode flag: $MODE_SET_BY" >&2
        echo "       Modes (--bootstrap, --bootstrap-up-to, --dry-run) are mutually exclusive." >&2
        exit 1
      fi
      MODE="dry-run"
      MODE_SET_BY="--dry-run"
      shift
      ;;
    -h|--help)
      sed -n '2,60p' "$0"
      exit 0
      ;;
    *)
      echo "ERROR: unknown flag: $1" >&2
      exit 1
      ;;
  esac
done

# --- Secret + path validation -----------------------------------------------

require_operator_secret SUPABASE_ACCESS_TOKEN \
  "Supabase PAT from https://supabase.com/dashboard/account/tokens (key name SUPABASE_MANAGEMENT_PAT also accepted)"

if [[ -z "$DEPLOY_PATH" ]]; then
  echo "ERROR: --deploy-path is required (path to the tenant deploy repo)." >&2
  echo "       Pass it explicitly, or use 'harness update-tenant <name>' which infers it." >&2
  exit 1
fi
if [[ ! -d "$DEPLOY_PATH" ]]; then
  echo "ERROR: --deploy-path does not exist or is not a directory: $DEPLOY_PATH" >&2
  exit 1
fi
DEPLOY_HARNESS_JSON="$DEPLOY_PATH/harness.json"
if [[ ! -f "$DEPLOY_HARNESS_JSON" ]]; then
  echo "ERROR: harness.json not found in deploy path: $DEPLOY_HARNESS_JSON" >&2
  exit 1
fi

# --- Filename safety guard --------------------------------------------------

assert_safe_filename() {
  local fname="$1"
  if [[ ! "$fname" =~ ^[0-9A-Za-z_.-]+\.sql$ ]]; then
    echo "ERROR: unsafe migration filename rejected: $fname" >&2
    echo "       Filenames must match: ^[0-9A-Za-z_.-]+\\.sql$" >&2
    exit 1
  fi
}

# --- Resolve PROJECT_REF + MIGRATIONS_DIR ----------------------------------

PROJECT_REF="${SUPABASE_PROJECT_REF:-$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['deployment']['supabase_project_ref'])" "$DEPLOY_HARNESS_JSON")}"
if [[ ! "$PROJECT_REF" =~ ^[a-z0-9]{20}$ ]]; then
  echo "ERROR: invalid SUPABASE_PROJECT_REF format: $PROJECT_REF" >&2
  echo "       Expected 20 lowercase alphanumeric chars (Supabase project ref shape)." >&2
  exit 1
fi

MIGRATIONS_DIR="${MIGRATIONS_DIR:-$HARNESS_ROOT/client-app/supabase/migrations}"
if [[ ! -d "$MIGRATIONS_DIR" ]]; then
  echo "ERROR: migrations dir not found: $MIGRATIONS_DIR" >&2
  echo "       Set MIGRATIONS_DIR to override." >&2
  exit 1
fi

API_URL="https://api.supabase.com/v1/projects/${PROJECT_REF}/database/query"

echo "Project:      $PROJECT_REF"
echo "Deploy path:  $DEPLOY_PATH"
echo "Migrations:   $MIGRATIONS_DIR"
echo "Mode:         $MODE"
echo ""

# --- Management API caller --------------------------------------------------

# run_sql SQL LABEL
#   POST SQL to the Supabase Management API. Streams SQL through printf|python
#   into curl's --data @<(...) so the body never hits argv (ARG_MAX safety).
#   Echoes the response body; exits non-zero on HTTP or content-level error.
run_sql() {
  local sql="$1"
  local label="$2"
  local response
  response=$(curl -sS \
    --fail-with-body \
    -X POST "$API_URL" \
    -H "Authorization: Bearer ${SUPABASE_ACCESS_TOKEN}" \
    -H "Content-Type: application/json" \
    --data @<(printf '%s' "$sql" | python3 -c "import json,sys; print(json.dumps({'query': sys.stdin.read()}))") \
    2>&1) || {
    echo "ERROR ($label): Management API call failed." >&2
    echo "$response" >&2
    exit 1
  }
  # Defense in depth: catch HTTP-200-with-error-body shape.
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
    echo "ERROR ($label): API returned a non-error HTTP status but body contains an error key." >&2
    echo "$probe" >&2
    echo "$response" >&2
    exit 1
  }
  echo "$response"
}

ensure_tracking_table() {
  run_sql \
    "CREATE TABLE IF NOT EXISTS public.schema_migrations (filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now());" \
    "ensure schema_migrations table" \
    >/dev/null
}

list_applied() {
  local response
  response=$(run_sql \
    "SELECT filename FROM public.schema_migrations ORDER BY filename;" \
    "list applied migrations")
  python3 -c "
import json, sys
data = json.loads(sys.argv[1])
if isinstance(data, list):
    for row in data:
        if isinstance(row, dict) and 'filename' in row:
            print(row['filename'])
" "$response"
}

record_applied() {
  local filename="$1"
  assert_safe_filename "$filename"
  run_sql \
    "INSERT INTO public.schema_migrations (filename) VALUES ('${filename}') ON CONFLICT (filename) DO NOTHING;" \
    "record ${filename}" \
    >/dev/null
}

# apply_one PATH
#   Wraps a migration file's contents in BEGIN/COMMIT alongside the
#   schema_migrations INSERT so both commit atomically. If the file already
#   has its own BEGIN/COMMIT (whitespace + comment tolerant), strip those
#   to avoid nested-transaction errors.
apply_one() {
  local file="$1"
  local fname
  fname=$(basename "$file")
  assert_safe_filename "$fname"
  local content
  content=$(cat "$file")
  local stripped
  stripped=$(printf '%s\n' "$content" | python3 -c "
import re, sys
text = sys.stdin.read()
lines = text.split('\n')
def strip_inline_blocks(s):
    while True:
        start = s.find('/*')
        if start < 0:
            return s
        end = s.find('*/', start + 2)
        if end < 0:
            return s
        s = s[:start] + s[end + 2:]
# Strip leading BEGIN (allow blank lines, -- and /* */ before it)
i = 0
in_block = False
block_start = -1
while i < len(lines):
    s = strip_inline_blocks(lines[i]).strip()
    if in_block:
        idx = s.find('*/')
        if idx >= 0:
            in_block = False
            remainder = s[idx + 2:].strip()
            if remainder and not remainder.startswith('--') and not remainder.startswith('/*'):
                for k in range(block_start, i):
                    lines[k] = ''
                lines[i] = remainder
                break
            block_start = -1
        i += 1
        continue
    if s == '' or s.startswith('--'):
        i += 1
        continue
    if s.startswith('/*'):
        in_block = True
        block_start = i
        i += 1
        continue
    break
if i < len(lines) and re.match(r'^BEGIN\b', strip_inline_blocks(lines[i]).strip().upper()):
    lines[i] = ''
# Strip trailing COMMIT/END (mirror, bottom-up)
j = len(lines) - 1
in_block = False
while j >= 0:
    s = strip_inline_blocks(lines[j]).strip()
    if in_block:
        if '/*' in s:
            in_block = False
        j -= 1
        continue
    if s == '' or s.startswith('--'):
        j -= 1
        continue
    if s.endswith('*/'):
        in_block = True
        j -= 1
        continue
    break
if j >= 0 and re.match(r'^(COMMIT|END)\b', strip_inline_blocks(lines[j]).strip().upper()):
    lines[j] = ''
print('\n'.join(lines))
")
  local wrapped
  wrapped="BEGIN;
${stripped}
INSERT INTO public.schema_migrations (filename) VALUES ('${fname}') ON CONFLICT (filename) DO NOTHING;
COMMIT;"
  run_sql "$wrapped" "apply ${fname}" >/dev/null
}

# --- Mode dispatch ----------------------------------------------------------

[[ "$MODE" != "dry-run" ]] && ensure_tracking_table

# bash-3.2 compatible array population (replaces `mapfile -t` from PR #1).
# macOS ships bash 3.2.57; mapfile is bash-4+ only and silently fails the
# script with "mapfile: command not found" on a fresh checkout. The
# while-IFS=read loop below is functionally identical and portable.
ALL_FILES=()
while IFS= read -r _f; do
  [[ -n "$_f" ]] && ALL_FILES+=("$_f")
done < <(ls -1 "$MIGRATIONS_DIR"/*.sql 2>/dev/null | sort)

if [[ ${#ALL_FILES[@]} -eq 0 ]]; then
  echo "No migration files found in $MIGRATIONS_DIR — nothing to do."
  exit 0
fi

# Validate every discovered filename before any further processing.
for _f in "${ALL_FILES[@]}"; do
  assert_safe_filename "$(basename "$_f")"
done

# Capture list_applied output explicitly so a failure (network blip, auth
# expiry, table missing) is caught before APPLIED is populated. Without
# this, downstream code would see an empty applied list and re-apply
# every migration — catastrophic.
if [[ "$MODE" == "dry-run" ]]; then
  # Dry-run is non-fatal but must surface degradation. If schema_migrations
  # is missing or the API errors transiently, the pending list would
  # otherwise misleadingly include already-applied files.
  _stderr_capture=$(mktemp)
  trap 'rm -f "$_stderr_capture"' EXIT
  if ! applied_output=$(list_applied 2>"$_stderr_capture"); then
    echo "WARNING: could not read schema_migrations (table missing or API error). Pending list below may be inaccurate." >&2
    if [[ -s "$_stderr_capture" ]]; then
      sed 's/^/  detail: /' "$_stderr_capture" >&2
    fi
    applied_output=""
  fi
  rm -f "$_stderr_capture"
  trap - EXIT
else
  if ! applied_output=$(list_applied); then
    echo "ERROR: failed to read applied-migrations list — aborting to prevent re-apply." >&2
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
  for a in "${APPLIED[@]+"${APPLIED[@]}"}"; do
    if [[ "$a" == "$fname" ]]; then found=1; break; fi
  done
  if [[ $found -eq 0 ]]; then PENDING+=("$f"); fi
done

case "$MODE" in
  bootstrap)
    BOOTSTRAP_SET=()
    if [[ -n "$BOOTSTRAP_UP_TO" ]]; then
      target_found=0
      if [[ ${#PENDING[@]} -gt 0 ]]; then
        for f in "${PENDING[@]}"; do
          if [[ "$(basename "$f")" == "$BOOTSTRAP_UP_TO" ]]; then
            target_found=1
            break
          fi
        done
      fi
      if [[ $target_found -eq 0 ]]; then
        # Differentiate "already applied" from "missing".
        for f in "${ALL_FILES[@]}"; do
          if [[ "$(basename "$f")" == "$BOOTSTRAP_UP_TO" ]]; then
            echo "Note: --bootstrap-up-to target is already recorded as applied: $BOOTSTRAP_UP_TO" >&2
            echo "Nothing to bootstrap. Run without --bootstrap-up-to to see pending state." >&2
            exit 0
          fi
        done
        echo "ERROR: --bootstrap-up-to target not found in migrations dir: $BOOTSTRAP_UP_TO" >&2
        echo "       Available files in $MIGRATIONS_DIR:" >&2
        for f in "${ALL_FILES[@]}"; do echo "         $(basename "$f")" >&2; done
        exit 1
      fi
      for f in "${PENDING[@]}"; do
        fname=$(basename "$f")
        BOOTSTRAP_SET+=("$f")
        if [[ "$fname" == "$BOOTSTRAP_UP_TO" ]]; then break; fi
      done
    else
      if [[ ${#PENDING[@]} -gt 0 ]]; then
        BOOTSTRAP_SET=("${PENDING[@]}")
      fi
    fi

    if [[ ${#BOOTSTRAP_SET[@]} -eq 0 ]]; then
      echo "Nothing to bootstrap." >&2
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
