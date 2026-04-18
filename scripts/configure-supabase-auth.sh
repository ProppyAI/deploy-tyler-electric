#!/usr/bin/env bash
# Configure this tenant's Supabase Auth URL allowlist via Management API.
# Sets site_url and uri_allow_list so magic-link redirects are accepted.
# Reads the tenant's subdomain from harness.json; no secrets inline.
#
# Requires:
#   SUPABASE_ACCESS_TOKEN — Supabase PAT (https://supabase.com/dashboard/account/tokens)
#
# Usage:
#   export SUPABASE_ACCESS_TOKEN=sbp_...
#   bash scripts/configure-supabase-auth.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ -z "${SUPABASE_ACCESS_TOKEN:-}" ]]; then
  echo "ERROR: SUPABASE_ACCESS_TOKEN is required."
  echo "Generate at https://supabase.com/dashboard/account/tokens"
  exit 1
fi

PROJECT_REF=$(python3 -c "import json; print(json.load(open('$REPO_ROOT/harness.json'))['deployment']['supabase_project_ref'])")
SUBDOMAIN=$(python3 -c "import json; print(json.load(open('$REPO_ROOT/harness.json'))['deployment']['subdomain'])")
SITE_URL="https://${SUBDOMAIN}"

# Allowlist covers the callback route on both the custom domain and the
# vercel.app alias (useful for preview/debug). Wildcards allowed per
# Supabase docs.
URI_ALLOW_LIST="${SITE_URL}/auth/callback,${SITE_URL}/**,https://*.vercel.app/auth/callback"

echo "Project:           $PROJECT_REF"
echo "Site URL:          $SITE_URL"
echo "URI allow list:    $URI_ALLOW_LIST"
echo ""
echo "Updating auth config..."

response=$(curl -sS \
  --fail-with-body \
  -X PATCH "https://api.supabase.com/v1/projects/${PROJECT_REF}/config/auth" \
  -H "Authorization: Bearer ${SUPABASE_ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  --data @<(python3 -c "
import json, os
print(json.dumps({
    'site_url': os.environ['SITE_URL'],
    'uri_allow_list': os.environ['URI_ALLOW_LIST'],
    'mailer_autoconfirm': False,
}))
") 2>&1) || {
    echo ""
    echo "ERROR: Auth config update failed."
    echo "$response"
    exit 1
  }

export SITE_URL URI_ALLOW_LIST

echo "Success. Response:"
echo "$response" | python3 -m json.tool 2>/dev/null | head -20 || echo "$response"
