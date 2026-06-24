#!/usr/bin/env bash
# tenant_common.sh — shared helpers for `harness tenant {sql,logs,outbox,…}`
# inspection subcommands.
#
# Each subcommand needs to resolve a tenant slug (e.g. `tyler-electric`) to
# the per-tenant inputs it operates on:
#
#   - supabase_project_ref → for Supabase Management API SQL queries
#   - client-app directory → for the locally-linked Netlify CLI invocation
#
# Plus operator credentials sourced from ~/.harness/operator.env. The
# operator never types secrets into the terminal; everything flows through
# this helper so additions are uniform.
#
# Why this file exists:
#   The first post-de-cron UAT (2026-05-21) burned ~30 minutes on operator
#   diagnostic plumbing — running curl directly against the Supabase
#   Management API, hand-correlating netlify logs by timestamp, etc. Those
#   surfaces all live HERE in HARNESS but had no ergonomic wrapper. See
#   memory project_harness_cli_tenant_inspection_gaps.md.
#
# Conventions:
#   - All functions assume `set -u`; never expand unset vars.
#   - Errors print to stderr with a `tenant_<verb>:` prefix + a one-line
#     hint at the fix.
#   - Tenant credentials are read from the deploy repo, NOT from operator.env
#     (operator.env is for cross-tenant operator-scope secrets like
#     SUPABASE_MANAGEMENT_PAT).

set -euo pipefail

# Resolve a tenant slug → repo path. Defaults to ~/Documents/deploy-<slug>
# but accepts HARNESS_DEPLOY_ROOT to override (CI / multi-machine setups).
resolve_tenant_repo() {
  local slug="$1"
  local root="${HARNESS_DEPLOY_ROOT:-$HOME/Documents}"
  local repo="$root/deploy-$slug"
  if [[ ! -d "$repo" ]]; then
    echo "tenant_common: tenant repo not found at $repo" >&2
    echo "  set HARNESS_DEPLOY_ROOT or ensure the deploy-$slug repo exists" >&2
    return 1
  fi
  echo "$repo"
}

# Resolve a tenant slug → Supabase project ref from the tenant's harness.json.
resolve_tenant_project_ref() {
  local slug="$1"
  local repo
  repo="$(resolve_tenant_repo "$slug")" || return 1
  local json="$repo/harness.json"
  if [[ ! -f "$json" ]]; then
    echo "tenant_common: $json not found" >&2
    return 1
  fi
  local ref
  ref="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("deployment",{}).get("supabase_project_ref",""))' "$json")"
  if [[ -z "$ref" ]]; then
    echo "tenant_common: deployment.supabase_project_ref empty in $json" >&2
    return 1
  fi
  echo "$ref"
}

# Resolve the HARNESS client-app directory (the locally-linked Netlify site).
# The path is fixed under HARNESS root since Tyler's site is locally-linked
# from HARNESS/client-app/.netlify/state.json (per reference_tyler_deploy_-
# architecture_2026_05_20).
resolve_client_app_dir() {
  local slug="$1"
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  local harness_root
  harness_root="$(cd "$script_dir/.." && pwd)"
  local client_app="$harness_root/client-app"
  if [[ ! -d "$client_app" ]]; then
    echo "tenant_common: client-app not found at $client_app" >&2
    return 1
  fi
  local state="$client_app/.netlify/state.json"
  if [[ ! -f "$state" ]]; then
    echo "tenant_common: $state not found — netlify CLI not linked" >&2
    echo "  cd $client_app && netlify link  # then retry" >&2
    return 1
  fi
  echo "$client_app"
  # NOTE: <slug> is accepted but currently unused — only one site is locally
  # linked at a time. When multi-site linking lands, this resolver picks the
  # right one. Keep the API stable now to avoid churn later.
  : "$slug"
}

# Format args for jq's --raw-output flag, falling back if jq is absent.
have_jq() {
  command -v jq >/dev/null 2>&1
}
