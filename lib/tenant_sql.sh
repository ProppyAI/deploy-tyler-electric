#!/usr/bin/env bash
# tenant_sql.sh — `harness tenant sql <slug> "<query>"` — ad-hoc SQL
# against a tenant's Supabase project via the Management API.
#
# Reads SUPABASE_MANAGEMENT_PAT from ~/.harness/operator.env. Reads
# supabase_project_ref from the tenant's harness.json. Never echoes the
# PAT into stdout/stderr.
#
# Default output: pretty-printed JSON (pass --table for column display,
# --csv for CSV). Read-only by convention — anything non-SELECT should
# go through the tenant's own write path (the agentic bot / app code),
# not this CLI. The Management API will accept writes if the PAT has
# permission, but the operator shouldn't be making schema changes this
# way (the runbook + migration apply-migrations.sh exist for that).
#
# Usage:
#   harness tenant sql tyler-electric "SELECT id, name FROM clients LIMIT 5"
#   harness tenant sql tyler-electric "SELECT * FROM qbo_outbox WHERE status='failed'" --table

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./operator-env.sh
source "$SCRIPT_DIR/operator-env.sh"
# shellcheck source=./tenant_common.sh
source "$SCRIPT_DIR/tenant_common.sh"

tenant_sql_usage() {
  cat >&2 <<USAGE
Usage:
  harness tenant sql <slug> "<query>" [--json|--table|--csv]

Examples:
  harness tenant sql tyler-electric "SELECT status, count(*) FROM qbo_outbox GROUP BY status"
  harness tenant sql tyler-electric "SELECT * FROM clients WHERE qbo_customer_id IS NULL" --table

Reads:
  - SUPABASE_MANAGEMENT_PAT  (from ~/.harness/operator.env)
  - supabase_project_ref     (from deploy-<slug>/harness.json)
USAGE
}

tenant_sql_main() {
  if [[ $# -lt 1 ]]; then
    tenant_sql_usage
    return 1
  fi
  case "$1" in
    --help|-h|help) tenant_sql_usage; return 0 ;;
  esac
  if [[ $# -lt 2 ]]; then
    tenant_sql_usage
    return 1
  fi
  local slug="$1"
  local query="$2"
  shift 2
  local format="json"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --json)  format="json"; shift ;;
      --table) format="table"; shift ;;
      --csv)   format="csv"; shift ;;
      --help|-h) tenant_sql_usage; return 0 ;;
      *)       echo "tenant_sql: unknown flag $1" >&2; tenant_sql_usage; return 1 ;;
    esac
  done

  require_operator_secret SUPABASE_MANAGEMENT_PAT \
    "Supabase PAT with project-level access (https://supabase.com/dashboard/account/tokens)"

  local project_ref
  project_ref="$(resolve_tenant_project_ref "$slug")" || return 1

  local body
  body="$(python3 -c 'import json,sys; print(json.dumps({"query": sys.argv[1]}))' "$query")"

  local response
  response="$(curl -sS -X POST \
    "https://api.supabase.com/v1/projects/$project_ref/database/query" \
    -H "Authorization: Bearer $SUPABASE_MANAGEMENT_PAT" \
    -H "Content-Type: application/json" \
    -d "$body")"

  # Detect Supabase error envelope: {"message": "..."} on non-array response.
  # The success shape is a JSON array (possibly empty).
  if [[ -z "$response" ]]; then
    echo "tenant_sql: empty response from Supabase" >&2
    return 1
  fi
  local first_char="${response:0:1}"
  if [[ "$first_char" == "{" ]]; then
    echo "tenant_sql: Supabase returned an error envelope:" >&2
    echo "$response" >&2
    return 1
  fi

  case "$format" in
    json)
      if have_jq; then
        echo "$response" | jq '.'
      else
        echo "$response"
      fi
      ;;
    table)
      python3 "$SCRIPT_DIR/tenant_format_table.py" <<< "$response"
      ;;
    csv)
      python3 "$SCRIPT_DIR/tenant_format_csv.py" <<< "$response"
      ;;
  esac
}

# If executed directly (not sourced), dispatch immediately.
if [[ "${BASH_SOURCE[0]}" == "${0:-}" ]]; then
  tenant_sql_main "$@"
fi
