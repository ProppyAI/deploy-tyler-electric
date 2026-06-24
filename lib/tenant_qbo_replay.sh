#!/usr/bin/env bash
# tenant_qbo_replay.sh — `harness tenant qbo-replay <slug> <outbox_row_id> [--apply]`
#
# Diagnostic tool that hits the tenant's /api/internal/qbo-replay route to
# (a) rebuild the QBO payload for a given qbo_outbox row and (b) optionally
# reset the row + retrigger processing against live QBO.
#
# Dry-run by default. --apply prompts for explicit "yes" confirmation
# before kicking off a real retry.
#
# Auth: INTERNAL_JOB_SECRET from ~/.harness/operator.env (Bearer token).
# Tenant URL: deploy-<slug>/harness.json -> deployment.url (or .deployment_url,
#             or .subdomain as bare hostname fallback: tylerelec.proppyai.io).
#
# Usage:
#   harness tenant qbo-replay tyler-electric 5050df2a-ffc1-48a3-8a11-43d0f022132b
#   harness tenant qbo-replay tyler-electric 5050df2a-ffc1-48a3-8a11-43d0f022132b --apply

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./operator-env.sh
source "$SCRIPT_DIR/operator-env.sh"
# shellcheck source=./tenant_common.sh
source "$SCRIPT_DIR/tenant_common.sh"

tenant_qbo_replay_usage() {
  cat >&2 <<USAGE
Usage:
  harness tenant qbo-replay <slug> <outbox_row_id> [--apply]

  Dry-run by default. --apply resets the row and triggers a live QBO retry.

Examples:
  harness tenant qbo-replay tyler-electric 5050df2a-ffc1-48a3-8a11-43d0f022132b
  harness tenant qbo-replay tyler-electric 5050df2a-ffc1-48a3-8a11-43d0f022132b --apply

Reads:
  - INTERNAL_JOB_SECRET     (from ~/.harness/operator.env)
  - deployment URL          (from deploy-<slug>/harness.json)
USAGE
}

_resolve_tenant_url() {
  local slug="$1"
  local repo
  repo="$(resolve_tenant_repo "$slug")" || return 1
  local json="$repo/harness.json"
  if [[ ! -f "$json" ]]; then
    echo "tenant_qbo_replay: $json not found" >&2
    return 1
  fi
  local url
  url="$(python3 -c '
import json, sys, re
d = json.load(open(sys.argv[1]))
dep = d.get("deployment", {})
explicit = dep.get("url") or dep.get("deployment_url")
if explicit:
    print(explicit)
else:
    # Fall back to subdomain (the canonical field in current tenant harness.json files,
    # e.g. tylerelec.proppyai.io). Validate as a dotted FQDN so a crafted value cannot
    # redirect the Bearer-token request to another host: this rejects userinfo "@",
    # path "/", port/scheme ":", whitespace, consecutive/trailing dots, AND bare labels
    # or IP literals (e.g. localhost, 169.254.169.254) that could reach internal/metadata
    # endpoints. Each label is alnum with optional internal hyphens; the TLD is alphabetic.
    sub = dep.get("subdomain") or ""
    if re.fullmatch(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}", sub):
        print(f"https://{sub}")
    else:
        print("")  # empty -> bash error path below
' "$json")"
  if [[ -z "$url" ]]; then
    echo "tenant_qbo_replay: could not resolve a valid HTTPS URL — set deployment.url, deployment.deployment_url, or a bare-hostname deployment.subdomain in $json" >&2
    return 1
  fi
  # strip trailing slash if present
  url="${url%/}"
  if [[ "$url" != https://* && "$url" != http://localhost* && "$url" != http://127.0.0.1* ]]; then
    echo "tenant_qbo_replay: resolved URL must use HTTPS (got ${url}) — check deployment.url/deployment_url/subdomain" >&2
    return 1
  fi
  echo "$url"
}

tenant_qbo_replay_main() {
  if [[ $# -lt 1 ]]; then
    tenant_qbo_replay_usage
    return 1
  fi
  case "$1" in
    --help|-h|help) tenant_qbo_replay_usage; return 0 ;;
  esac
  if [[ $# -lt 2 ]]; then
    tenant_qbo_replay_usage
    return 1
  fi

  local slug="$1"
  local row_id="$2"
  shift 2

  local apply="false"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --apply) apply="true"; shift ;;
      --help|-h) tenant_qbo_replay_usage; return 0 ;;
      *) echo "tenant_qbo_replay: unknown flag $1" >&2; tenant_qbo_replay_usage; return 1 ;;
    esac
  done

  # Basic UUID-ish validation (alnum + dashes)
  if [[ ! "$row_id" =~ ^[A-Za-z0-9-]+$ ]]; then
    echo "tenant_qbo_replay: outbox_row_id must be alphanumeric + dashes" >&2
    return 1
  fi

  require_operator_secret INTERNAL_JOB_SECRET \
    "Shared secret for HARNESS internal job routes (set in ~/.harness/operator.env)"

  local tenant_url
  tenant_url="$(_resolve_tenant_url "$slug")" || return 1

  if [[ "$apply" == "true" ]]; then
    echo "About to RETRY qbo_outbox row $row_id against $slug's live QBO." >&2
    read -r -p "Type 'yes' to proceed: " confirm
    if [[ "$confirm" != "yes" ]]; then
      echo "Aborted." >&2
      return 1
    fi
  fi

  local body
  body="$(python3 -c '
import json, sys
print(json.dumps({"outbox_row_id": sys.argv[1], "apply": sys.argv[2] == "true"}))
' "$row_id" "$apply")"

  local response
  response="$(curl -sS -X POST "${tenant_url}/api/internal/qbo-replay" \
    -H "Authorization: Bearer ${INTERNAL_JOB_SECRET}" \
    -H "Content-Type: application/json" \
    -d "$body")" || {
      echo "tenant_qbo_replay: curl failed" >&2
      return 1
    }

  if [[ -z "$response" ]]; then
    echo "tenant_qbo_replay: empty response from $tenant_url" >&2
    return 1
  fi

  if have_jq; then
    echo "$response" | jq '.'
  else
    python3 -m json.tool <<< "$response"
  fi
}

if [[ "${BASH_SOURCE[0]}" == "${0:-}" ]]; then
  tenant_qbo_replay_main "$@"
fi
