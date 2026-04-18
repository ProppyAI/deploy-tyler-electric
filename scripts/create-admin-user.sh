#!/usr/bin/env bash
# Create an admin user in this tenant's Supabase via Auth Admin API.
# Reads SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY from .env.production.
# Generates a random temp password if one isn't passed as $2.
#
# Usage:
#   bash scripts/create-admin-user.sh <email>
#   bash scripts/create-admin-user.sh <email> <password>
#
# On success, prints the email + password to stdout (terminal only — not in git,
# not in commits). Share with the user via phone/paper/password manager, NOT chat.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env.production"

if [[ -z "${1:-}" ]]; then
  echo "Usage: $0 <email> [password]"
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found."
  exit 1
fi

# Pull Supabase creds from .env.production without sourcing the whole file
SUPABASE_URL=$(grep -E '^NEXT_PUBLIC_SUPABASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2-)
SERVICE_ROLE_KEY=$(grep -E '^SUPABASE_SERVICE_ROLE_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2-)

if [[ -z "$SUPABASE_URL" || -z "$SERVICE_ROLE_KEY" ]]; then
  echo "ERROR: NEXT_PUBLIC_SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing/blank in $ENV_FILE"
  exit 1
fi

export EMAIL="$1"
export PASSWORD="${2:-$(openssl rand -base64 18 | tr -d '=+/' | head -c 20)}"

echo "Creating admin user at $SUPABASE_URL ..."

response=$(curl -sS \
  --fail-with-body \
  -X POST "$SUPABASE_URL/auth/v1/admin/users" \
  -H "apikey: $SERVICE_ROLE_KEY" \
  -H "Authorization: Bearer $SERVICE_ROLE_KEY" \
  -H "Content-Type: application/json" \
  --data @<(python3 -c "
import json, os
print(json.dumps({
    'email': os.environ['EMAIL'],
    'password': os.environ['PASSWORD'],
    'email_confirm': True,
    'app_metadata': {'role': 'admin'}
}))
" ) 2>&1) || {
    echo ""
    echo "ERROR: Auth Admin API call failed."
    echo "$response"
    exit 1
  }

user_id=$(echo "$response" | python3 -c "import json, sys; d=json.load(sys.stdin); print(d.get('id','?'))" 2>/dev/null || echo "?")

echo ""
echo "======================================"
echo "  ADMIN USER CREATED"
echo "======================================"
echo "  Email:    $EMAIL"
echo "  Password: $PASSWORD"
echo "  User ID:  $user_id"
echo "  Role:     admin"
echo ""
echo "  Share these credentials with the user"
echo "  via phone/paper/password manager — NOT chat."
echo "======================================"
