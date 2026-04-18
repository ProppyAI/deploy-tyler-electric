#!/usr/bin/env bash
# Apply client-app migrations to this tenant's Supabase project via the
# Supabase Management API. Works from anywhere — no DB network reachability
# required. Canonical migration mechanism for HARNESS tenant deployments.
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
#   bash scripts/apply-migrations.sh

set -euo pipefail

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

echo "Project:      $PROJECT_REF"
echo "Migrations:   $MIGRATIONS_DIR"
echo ""

# Concat migrations in-order, wrapped in a transaction so any failure rolls back
SEED_FILE="$(mktemp -t tyler_migrations.XXXXXX.sql)"
trap "rm -f '$SEED_FILE'" EXIT

{
  echo "BEGIN;"
  for f in $(ls -1 "$MIGRATIONS_DIR"/*.sql | sort); do
    echo "-- ==== $(basename "$f") ===="
    cat "$f"
    echo ""
  done
  echo "COMMIT;"
} > "$SEED_FILE"

BYTES=$(wc -c < "$SEED_FILE" | tr -d ' ')
echo "Concatenated $(ls -1 "$MIGRATIONS_DIR"/*.sql | wc -l | tr -d ' ') migrations (${BYTES} bytes). Posting to Management API..."

# POST to the Management API — single transactional apply
# Docs: https://supabase.com/docs/reference/api/v1-run-a-query
response=$(curl -sS \
  --fail-with-body \
  -X POST "https://api.supabase.com/v1/projects/${PROJECT_REF}/database/query" \
  -H "Authorization: Bearer ${SUPABASE_ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  --data @<(python3 -c "import json,sys; print(json.dumps({'query': open('$SEED_FILE').read()}))") \
  2>&1) || {
    echo ""
    echo "ERROR: Management API call failed."
    echo "$response"
    exit 1
  }

echo ""
echo "Success. API response:"
echo "$response" | python3 -m json.tool 2>/dev/null || echo "$response"
echo ""
echo "Verifying table count in public schema..."

verify=$(curl -sS \
  -X POST "https://api.supabase.com/v1/projects/${PROJECT_REF}/database/query" \
  -H "Authorization: Bearer ${SUPABASE_ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"query": "SELECT table_name FROM information_schema.tables WHERE table_schema = '"'"'public'"'"' ORDER BY table_name"}')

echo "$verify" | python3 -m json.tool 2>/dev/null || echo "$verify"
