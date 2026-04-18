#!/usr/bin/env bash
# Register this tenant's Telegram bot webhook.
# Hits the client-app's /api/telegram/setup endpoint, which is gated by
# TELEGRAM_SETUP_SECRET and internally calls Telegram setWebhook with the
# per-tenant bot token + webhook secret already in Vercel env.
#
# Reads NEXT_PUBLIC_APP_URL + TELEGRAM_SETUP_SECRET from .env.production.
# No bot token leaves the Vercel environment — we just authorize the
# deployed endpoint to do it server-side.
#
# Usage:
#   bash scripts/register-telegram-webhook.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env.production"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found."
  exit 1
fi

APP_URL=$(grep -E '^NEXT_PUBLIC_APP_URL=' "$ENV_FILE" | head -1 | cut -d= -f2-)
SETUP_SECRET=$(grep -E '^TELEGRAM_SETUP_SECRET=' "$ENV_FILE" | head -1 | cut -d= -f2-)

if [[ -z "$APP_URL" || -z "$SETUP_SECRET" || "$SETUP_SECRET" == "GENERATED" ]]; then
  echo "ERROR: NEXT_PUBLIC_APP_URL or TELEGRAM_SETUP_SECRET missing/placeholder."
  echo "Has scripts/setup-vercel.sh been run? That fills in TELEGRAM_SETUP_SECRET."
  exit 1
fi

echo "Registering Telegram webhook at ${APP_URL}/api/telegram/webhook ..."

response=$(curl -sS \
  --fail-with-body \
  -X POST "${APP_URL}/api/telegram/setup" \
  -H "Authorization: Bearer ${SETUP_SECRET}" \
  -H "Content-Type: application/json" \
  2>&1) || {
    echo ""
    echo "ERROR: Webhook registration failed."
    echo "$response"
    exit 1
  }

echo ""
echo "Success. Response:"
echo "$response" | python3 -m json.tool 2>/dev/null || echo "$response"
echo ""
echo "Verify via Telegram getWebhookInfo:"
echo "  curl -s \"https://api.telegram.org/bot\$TELEGRAM_BOT_TOKEN/getWebhookInfo\" | python3 -m json.tool"
echo "(bot token is only in Vercel env; run this with the token if you need the verification)"
