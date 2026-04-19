#!/usr/bin/env bash
# Push env vars + deploy this tenant to Netlify. Direct replacement for
# setup-vercel.sh after the 2026-04-19 Vercel breach (see
# memory/project_vercel_breach_2026_04_19.md).
#
# Reads .env.production, generates any GENERATED-marked secrets, and runs
# `netlify env:set` for each key. Idempotent — existing env vars are
# overwritten.
#
# Prereqs:
#   - netlify CLI installed (npm i -g netlify-cli)
#   - netlify login (one-time, browser)
#   - netlify link inside HARNESS/client-app to this tenant's site
#   - .env.production populated from .env.production.template (with ROTATED
#     values, not the pre-breach Vercel values)
#
# Usage:
#   bash scripts/setup-netlify.sh
#
# Secret-marking notes:
#   Netlify CLI v18+ supports --secret flag on `env:set` to flag a variable
#   as secret. Secret vars are masked in the UI and excluded from build logs.
#   NEXT_PUBLIC_* are intentionally NOT marked secret — they ship to the
#   client bundle anyway and marking them secret breaks nothing but is
#   semantically wrong. Everything else gets --secret.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env.production"
CLIENT_APP_DIR="${CLIENT_APP_DIR:-$REPO_ROOT/../HARNESS/client-app}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Copy .env.production.template and fill in values (ROTATED, post-breach)."
  exit 1
fi

if [[ ! -d "$CLIENT_APP_DIR" ]]; then
  echo "ERROR: client-app dir not found at $CLIENT_APP_DIR"
  exit 1
fi

if ! command -v netlify &>/dev/null; then
  echo "ERROR: netlify CLI not installed. Run: npm i -g netlify-cli"
  exit 1
fi

if [[ ! -f "$CLIENT_APP_DIR/.netlify/state.json" ]]; then
  echo "ERROR: $CLIENT_APP_DIR is not linked to a Netlify site."
  echo "Run: cd $CLIENT_APP_DIR && netlify init   (or netlify link)"
  echo "Pick the 'tyler-electric' site, or create it if it doesn't exist."
  exit 1
fi

# Keys whose values are NOT secrets — they're config or ship to the client bundle.
# Everything else gets --secret.
NON_SECRET_KEYS=(
  NEXT_PUBLIC_SUPABASE_URL
  NEXT_PUBLIC_SUPABASE_ANON_KEY
  NEXT_PUBLIC_APP_URL
  NEXT_PUBLIC_CLIENT_NAME
  CLIENT_SLUG
  QBO_CLIENT_ID
  QBO_REDIRECT_URI
  QBO_ENVIRONMENT
  QBO_SERVICES_ITEM_ID
  VISION_MODEL
  VENDOR_MATCH_MODEL
  MEDIA_CONFIDENCE_THRESHOLD
  MEDIA_MAX_BYTES
  TELEGRAM_ALLOWED_USER_IDS
)

is_non_secret() {
  local k="$1"
  for n in "${NON_SECRET_KEYS[@]}"; do
    [[ "$k" == "$n" ]] && return 0
  done
  return 1
}

# Generate any GENERATED secrets in-place
echo "-- Generating missing secrets --"
python3 - "$ENV_FILE" <<'PY'
import sys, re, secrets
path = sys.argv[1]
with open(path) as f:
    lines = f.readlines()
changed = False
for i, line in enumerate(lines):
    m = re.match(r'^([A-Z_]+)=GENERATED\s*$', line)
    if m:
        key = m.group(1)
        lines[i] = f"{key}={secrets.token_hex(32)}\n"
        print(f"  generated {key}")
        changed = True
if changed:
    with open(path, "w") as f:
        f.writelines(lines)
else:
    print("  (none — all secrets already set)")
PY

echo ""
echo "-- Pushing env vars to Netlify production context --"
cd "$CLIENT_APP_DIR"

while IFS= read -r line <&3; do
  [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue

  key="${line%%=*}"
  value="${line#*=}"

  if [[ -z "$value" || "$value" == "GENERATED" ]]; then
    echo "  skip $key (no value)"
    continue
  fi

  # Netlify env:set with --context production ties the var to prod deploys only.
  # --force overwrites without prompting.
  if is_non_secret "$key"; then
    if ! netlify env:set "$key" "$value" --context production --force </dev/null >/dev/null 2>&1; then
      echo "  FAILED (plain) $key"
      continue
    fi
    echo "  set (plain) $key"
  else
    if ! netlify env:set "$key" "$value" --context production --force --secret </dev/null >/dev/null 2>&1; then
      echo "  FAILED (secret) $key"
      continue
    fi
    echo "  set (secret) $key"
  fi
done 3< "$ENV_FILE"

echo ""
echo "-- Triggering production deploy --"
netlify deploy --build --prod

echo ""
echo "Next steps:"
echo "  1. Verify tyler-electric site is serving by visiting the Netlify URL Netlify prints above."
echo "  2. Add tylerelec.proppyai.io as a custom domain in the Netlify dashboard."
echo "  3. Swap Squarespace DNS: tylerelec CNAME to <site>.netlify.app (or A record to apex-loadbalancer.netlify.com)."
echo "  4. After DNS propagates, re-run scripts/configure-supabase-auth.sh (Supabase allowlist unchanged but worth re-verifying)."
echo "  5. Re-run scripts/register-telegram-webhook.sh (Telegram setWebhook with new secret if TELEGRAM_WEBHOOK_SECRET rotated)."
echo "  6. Delete the old Vercel project: vercel project rm tyler-electric"
