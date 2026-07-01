#!/usr/bin/env bash
# tenant_outbox.sh — `harness tenant outbox <slug>` — canonical view of the
# qbo_outbox table for the named tenant.
#
# This is the single most-used diagnostic during agentic-bot UAT. Calls into
# tenant_sql.sh's transport (same SUPABASE_MANAGEMENT_PAT auth, same SQL
# endpoint) but with a curated query + filter flags.
#
# Usage:
#   harness tenant outbox tyler-electric                  # recent 20 rows, grouped by status
#   harness tenant outbox tyler-electric --failed
#   harness tenant outbox tyler-electric --pending
#   harness tenant outbox tyler-electric --since 1h
#   harness tenant outbox tyler-electric --entity customer
#   harness tenant outbox tyler-electric --failed --since 30m --table

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./operator-env.sh
source "$SCRIPT_DIR/operator-env.sh"
# shellcheck source=./tenant_common.sh
source "$SCRIPT_DIR/tenant_common.sh"

tenant_outbox_usage() {
  cat >&2 <<USAGE
Usage:
  harness tenant outbox <slug> [--failed|--pending|--all] [--since DURATION]
                              [--entity NAME] [--limit N] [--json|--table|--csv]

Defaults to --table, since 24h, limit 20, all statuses.

Examples:
  harness tenant outbox tyler-electric
  harness tenant outbox tyler-electric --failed
  harness tenant outbox tyler-electric --pending --since 30m
  harness tenant outbox tyler-electric --entity estimate --failed
  harness tenant outbox tyler-electric --all --since 24h --json
USAGE
}

# Convert a duration string (e.g. 30m, 2h, 24h) into a SQL INTERVAL literal.
# Returns the interval text on stdout. Defaults to 24h if invalid.
sql_interval_from_duration() {
  local d="$1"
  local num="${d//[!0-9]/}"
  local unit="${d//[0-9]/}"
  if [[ -z "$num" || -z "$unit" ]]; then
    echo "24 hours"
    return
  fi
  case "$unit" in
    s) echo "$num seconds" ;;
    m) echo "$num minutes" ;;
    h) echo "$num hours" ;;
    d) echo "$num days" ;;
    *) echo "24 hours" ;;
  esac
}

tenant_outbox_main() {
  if [[ $# -lt 1 ]]; then
    tenant_outbox_usage
    return 1
  fi
  case "$1" in
    --help|-h|help) tenant_outbox_usage; return 0 ;;
  esac
  local slug="$1"
  shift
  local status_filter=""   # "failed" | "pending" | "" (all)
  local since="24h"
  local entity=""
  local limit=20
  local format="table"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --failed)   status_filter="failed"; shift ;;
      --pending)  status_filter="pending"; shift ;;
      --all)      status_filter=""; shift ;;
      --since)    since="$2"; shift 2 ;;
      --entity)   entity="$2"; shift 2 ;;
      --limit)    limit="$2"; shift 2 ;;
      --json)     format="json"; shift ;;
      --table)    format="table"; shift ;;
      --csv)      format="csv"; shift ;;
      --help|-h)  tenant_outbox_usage; return 0 ;;
      *)          echo "tenant_outbox: unknown flag $1" >&2; tenant_outbox_usage; return 1 ;;
    esac
  done

  # Validate inputs that flow into the SQL string BEFORE touching credentials
  # or resolving the tenant project ref. This way malformed input fails fast
  # and never reaches operator.env / harness.json lookups. Status comes from a
  # restricted set of flags above so it's safe. Entity is operator-supplied;
  # restrict to alnum/underscore to prevent injection.
  if [[ -n "$entity" && ! "$entity" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    echo "tenant_outbox: --entity must match ^[A-Za-z_][A-Za-z0-9_]*\$" >&2
    return 1
  fi
  if [[ ! "$limit" =~ ^[0-9]+$ ]] || (( limit < 1 || limit > 1000 )); then
    echo "tenant_outbox: --limit must be a positive integer ≤ 1000" >&2
    return 1
  fi

  require_operator_secret SUPABASE_MANAGEMENT_PAT \
    "Supabase PAT with project-level access (https://supabase.com/dashboard/account/tokens)"

  local project_ref
  project_ref="$(resolve_tenant_project_ref "$slug")" || return 1

  local interval
  interval="$(sql_interval_from_duration "$since")"

  # Build the SQL with simple string composition. All variable values are
  # validated above; the SQL endpoint runs each request in its own
  # transaction so a single bad query can't poison state.
  local where_clauses="created_at > NOW() - INTERVAL '$interval'"
  if [[ -n "$status_filter" ]]; then
    where_clauses="$where_clauses AND status = '$status_filter'"
  fi
  if [[ -n "$entity" ]]; then
    where_clauses="$where_clauses AND entity = '$entity'"
  fi

  local query="
SELECT
  id,
  entity,
  op,
  status,
  attempts,
  CASE WHEN last_error IS NULL THEN NULL ELSE LEFT(last_error, 100) END AS last_error,
  qbo_entity_id,
  claimed_at,
  created_at
FROM qbo_outbox
WHERE $where_clauses
ORDER BY created_at DESC
LIMIT $limit"

  local body
  body="$(python3 -c 'import json,sys; print(json.dumps({"query": sys.argv[1]}))' "$query")"

  local response
  response="$(curl -sS -X POST \
    "https://api.supabase.com/v1/projects/$project_ref/database/query" \
    -H "Authorization: Bearer $SUPABASE_MANAGEMENT_PAT" \
    -H "Content-Type: application/json" \
    -d "$body")"

  if [[ -z "$response" ]]; then
    echo "tenant_outbox: empty response from Supabase" >&2
    return 1
  fi
  local first_char="${response:0:1}"
  if [[ "$first_char" == "{" ]]; then
    echo "tenant_outbox: Supabase returned an error envelope:" >&2
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

if [[ "${BASH_SOURCE[0]}" == "${0:-}" ]]; then
  tenant_outbox_main "$@"
fi
