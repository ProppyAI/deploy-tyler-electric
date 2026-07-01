#!/usr/bin/env bash
# tenant_logs.sh — `harness tenant logs <slug>` — wraps `netlify logs`
# against the tenant's locally-linked Netlify site.
#
# Tyler's Netlify site is locally-linked from HARNESS/client-app/.netlify/
# state.json. When the operator runs this, we cd into client-app/ so the
# Netlify CLI picks up the right siteId. Default window is 30 minutes; an
# operator triaging an incident usually wants the past 1-2 hours.
#
# Usage:
#   harness tenant logs tyler-electric                              # last 30m
#   harness tenant logs tyler-electric --since 1h
#   harness tenant logs tyler-electric --since 30m --function qbo-outbox-process
#   harness tenant logs tyler-electric --filter "QBO|400"
#   harness tenant logs tyler-electric --around 2026-05-21T22:00:26Z

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./tenant_common.sh
source "$SCRIPT_DIR/tenant_common.sh"

tenant_logs_usage() {
  cat >&2 <<USAGE
Usage:
  harness tenant logs <slug> [--since DURATION] [--function NAME] [--filter REGEX] [--around TIMESTAMP]

Examples:
  harness tenant logs tyler-electric
  harness tenant logs tyler-electric --since 1h
  harness tenant logs tyler-electric --function qbo-outbox-process --since 30m
  harness tenant logs tyler-electric --filter "QBO|400" --since 2h
  harness tenant logs tyler-electric --around 2026-05-21T22:00:26Z

Wraps \`netlify logs --source functions\` against the locally-linked site
at HARNESS/client-app/.netlify/state.json.

--since DURATION   Netlify duration string: 30s, 5m, 1h, etc. Default: 30m.
--function NAME    Restrict to a single function (e.g. qbo-outbox-process).
--filter REGEX     Pipe through 'grep -E' after collection.
--around TS        ISO-8601 timestamp; shows logs within ±2 minutes of TS.
                   Mutually exclusive with --since; --filter still applies.
USAGE
}

tenant_logs_main() {
  if [[ $# -lt 1 ]]; then
    tenant_logs_usage
    return 1
  fi
  case "$1" in
    --help|-h|help) tenant_logs_usage; return 0 ;;
  esac
  local slug="$1"
  shift
  local since="30m"
  local function_name=""
  local filter=""
  local around=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --since)    since="$2"; shift 2 ;;
      --function) function_name="$2"; shift 2 ;;
      --filter)   filter="$2"; shift 2 ;;
      --around)   around="$2"; shift 2 ;;
      --help|-h)  tenant_logs_usage; return 0 ;;
      *)          echo "tenant_logs: unknown flag $1" >&2; tenant_logs_usage; return 1 ;;
    esac
  done

  local client_app
  client_app="$(resolve_client_app_dir "$slug")" || return 1

  # --around overrides --since with a wider 2h pull, then we filter by ts window.
  local effective_since="$since"
  if [[ -n "$around" ]]; then
    effective_since="2h"
  fi

  local args=(logs --source functions --since "$effective_since")
  if [[ -n "$function_name" ]]; then
    args+=(--function "$function_name")
  fi

  local raw
  if ! raw="$(cd "$client_app" && netlify "${args[@]}" 2>&1)"; then
    echo "tenant_logs: netlify logs failed" >&2
    echo "$raw" >&2
    return 1
  fi

  # Optional --around filter: keep lines whose timestamp falls within ±2min of TS.
  if [[ -n "$around" ]]; then
    raw="$(python3 "$SCRIPT_DIR/tenant_logs_filter.py" "$around" <<< "$raw")"
  fi

  # Optional --filter regex.
  if [[ -n "$filter" ]]; then
    # Use grep -E with -- for safety against regexes starting with '-'.
    raw="$(echo "$raw" | grep -E -- "$filter" || true)"
  fi

  echo "$raw"
}

if [[ "${BASH_SOURCE[0]}" == "${0:-}" ]]; then
  tenant_logs_main "$@"
fi
