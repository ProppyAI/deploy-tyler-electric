#!/usr/bin/env bash
# tenant_qbo_read.sh — read-only QBO inspection.
#   harness tenant qbo-get   <slug> <entity> <id>
#   harness tenant qbo-query <slug> <entity> "<where-clause>"
#
# Hits the tenant's /api/internal/qbo-read route. Read-only: no confirmation,
# no writes. Entities: invoice estimate customer item account bill vendor payment.
# Auth: INTERNAL_JOB_SECRET (~/.harness/operator.env). Tenant URL from
# deploy-<slug>/harness.json (deployment.url / .deployment_url / .subdomain).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=./operator-env.sh
source "$SCRIPT_DIR/operator-env.sh"
# shellcheck source=./tenant_common.sh
source "$SCRIPT_DIR/tenant_common.sh"

tenant_qbo_read_usage() {
  cat >&2 <<USAGE
Usage:
  harness tenant qbo-get   <slug> <entity> <id>
  harness tenant qbo-query <slug> <entity> "<where-clause>"

  Read-only QBO inspection. Entities: invoice estimate customer item account
  bill vendor payment.

Examples:
  harness tenant qbo-get   tyler-electric invoice 975
  harness tenant qbo-query tyler-electric invoice "where DocNumber = 'TE-2026-002-I1'"

Reads:
  - INTERNAL_JOB_SECRET  (from ~/.harness/operator.env)
  - deployment URL       (from deploy-<slug>/harness.json)
USAGE
}

# Resolves a validated HTTPS URL for the given tenant slug.
# Duplicates the URL-resolution logic from tenant_qbo_replay.sh (inline-clone
# pattern; dedupe into tenant_common.sh is a noted follow-up).
_qbo_read_resolve_url() {
  local slug="$1"
  local repo
  repo="$(resolve_tenant_repo "$slug")" || return 1
  local json="$repo/harness.json"
  if [[ ! -f "$json" ]]; then
    echo "tenant_qbo_read: $json not found" >&2
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
    # redirect the Bearer-token request to another host.
    sub = dep.get("subdomain") or ""
    if re.fullmatch(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}", sub):
        print(f"https://{sub}")
    else:
        print("")
' "$json")"
  if [[ -z "$url" ]]; then
    echo "tenant_qbo_read: could not resolve a valid HTTPS URL — set deployment.url, deployment.deployment_url, or a bare-hostname deployment.subdomain in $json" >&2
    return 1
  fi
  # strip trailing slash if present
  url="${url%/}"
  # Allow https:// (any host) or an EXACT localhost/127.0.0.1 base (port-optional).
  # Exact-match guards against prefix-glob bypass — `http://localhost.evil.com`
  # must NOT be accepted as "localhost" and have the Bearer token sent to it.
  if [[ "$url" != https://* \
     && "$url" != "http://localhost" && "$url" != "http://localhost:"* \
     && "$url" != "http://127.0.0.1" && "$url" != "http://127.0.0.1:"* ]]; then
    echo "tenant_qbo_read: resolved URL must use HTTPS (got ${url}) — check deployment.url/deployment_url/subdomain" >&2
    return 1
  fi
  echo "$url"
}

# Internal dispatcher: $1=mode (get|query); "$@" = <slug> <entity> <id|where>.
_tenant_qbo_read_main() {
  local mode="$1"; shift

  if [[ $# -lt 1 ]]; then
    tenant_qbo_read_usage
    return 1
  fi
  case "$1" in
    --help|-h|help) tenant_qbo_read_usage; return 0 ;;
  esac
  if [[ $# -lt 3 ]]; then
    tenant_qbo_read_usage
    return 1
  fi

  local slug="$1" entity="$2" arg="$3"

  require_operator_secret INTERNAL_JOB_SECRET \
    "Shared secret for HARNESS internal job routes (set in ~/.harness/operator.env)"

  local tenant_url
  tenant_url="$(_qbo_read_resolve_url "$slug")" || return 1

  local body
  body="$(python3 -c '
import json, sys
mode, entity, arg = sys.argv[1], sys.argv[2], sys.argv[3]
payload = {"mode": mode, "entity": entity}
if mode == "get":
    payload["id"] = arg
else:
    payload["where"] = arg
print(json.dumps(payload))
' "$mode" "$entity" "$arg")"

  local response
  # --max-redirs 0: curl already does not follow redirects without -L, but make
  # it explicit so the Bearer token can never be forwarded to a redirect target.
  response="$(curl -sS --max-redirs 0 -X POST "${tenant_url}/api/internal/qbo-read" \
    -H "Authorization: Bearer ${INTERNAL_JOB_SECRET}" \
    -H "Content-Type: application/json" \
    -d "$body")" || { echo "tenant_qbo_read: curl failed" >&2; return 1; }

  if [[ -z "$response" ]]; then
    echo "tenant_qbo_read: empty response from $tenant_url" >&2
    return 1
  fi

  if have_jq; then
    echo "$response" | jq '.'
  else
    echo "$response" | python3 -m json.tool
  fi
}

tenant_qbo_get_main()   { _tenant_qbo_read_main "get"   "$@"; }
tenant_qbo_query_main() { _tenant_qbo_read_main "query" "$@"; }

if [[ "${BASH_SOURCE[0]}" == "${0:-}" ]]; then
  echo "tenant_qbo_read.sh: source + call tenant_qbo_get_main or tenant_qbo_query_main" >&2
  exit 1
fi
