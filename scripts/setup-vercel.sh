#!/usr/bin/env bash
# Push env vars + set up domain on Vercel for this tenant deployment.
# Reads .env.production, generates any GENERATED-marked secrets, and runs
# `vercel env add` for each key. Idempotent — existing env vars are replaced.
#
# Prereqs:
#   - vercel CLI installed (npm i -g vercel)
#   - vercel login (one-time, browser)
#   - vercel link inside HARNESS/client-app (one-time, pointing at Tyler's project)
#   - .env.production populated from .env.production.template
#
# Usage:
#   bash scripts/setup-vercel.sh
#
# Assumes the Vercel project is already linked in HARNESS/client-app via
# `cd HARNESS/client-app && vercel link` pointing at this tenant's project.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env.production"
CLIENT_APP_DIR="${CLIENT_APP_DIR:-$REPO_ROOT/../HARNESS/client-app}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE not found. Copy .env.production.template and fill in values."
  exit 1
fi

if [[ ! -d "$CLIENT_APP_DIR" ]]; then
  echo "ERROR: client-app dir not found at $CLIENT_APP_DIR"
  echo "Set CLIENT_APP_DIR to override."
  exit 1
fi

if ! command -v vercel &>/dev/null; then
  echo "ERROR: vercel CLI not installed. Run: npm i -g vercel"
  exit 1
fi

if [[ ! -f "$CLIENT_APP_DIR/.vercel/project.json" ]]; then
  echo "ERROR: $CLIENT_APP_DIR is not linked to a Vercel project."
  echo "Run: cd $CLIENT_APP_DIR && vercel link"
  echo "Link it to the 'tyler-electric' project (create if needed)."
  exit 1
fi

# Generate any GENERATED secrets in-place
echo "-- Generating missing secrets --"
python3 - "$ENV_FILE" <<'PY'
import os, re, secrets, sys
path = sys.argv[1]
with open(path) as f:
    lines = f.readlines()
changed = False
for i, line in enumerate(lines):
    m = re.match(r'^([A-Z_]+)=GENERATED\s*$', line)
    if m:
        key = m.group(1)
        value = secrets.token_hex(32)
        lines[i] = f"{key}={value}\n"
        print(f"  generated {key}")
        changed = True
if changed:
    with open(path, "w") as f:
        f.writelines(lines)
else:
    print("  (none — all secrets already set)")
PY

echo ""
echo "-- Pushing env vars to Vercel (production + preview) --"

# Vercel CLI behavior: `vercel env add KEY production` prompts for the value on
# stdin. We use `--force` so we can pipe the value in and overwrite any existing.
# `vercel env rm KEY production --yes` first to avoid duplicate-key errors on
# re-runs.

cd "$CLIENT_APP_DIR"

# Skip blank-value lines and comments
while IFS= read -r line; do
  # strip comments and blanks
  [[ -z "$line" || "$line" =~ ^\s*# ]] && continue

  key="${line%%=*}"
  value="${line#*=}"

  # skip lines that still have placeholder values (empty or GENERATED leftover)
  if [[ -z "$value" || "$value" == "GENERATED" ]]; then
    echo "  skip $key (no value)"
    continue
  fi

  # remove existing, ignore failures if key didn't exist
  vercel env rm "$key" production --yes >/dev/null 2>&1 || true
  vercel env rm "$key" preview --yes    >/dev/null 2>&1 || true

  # add to both production and preview
  printf '%s' "$value" | vercel env add "$key" production >/dev/null 2>&1
  printf '%s' "$value" | vercel env add "$key" preview    >/dev/null 2>&1

  echo "  set  $key"
done < "$ENV_FILE"

echo ""
echo "-- Done. Triggering production deploy --"
vercel --prod

echo ""
echo "Next steps:"
echo "  1. Add the 'tylerelec' CNAME on Squarespace pointing at cname.vercel-dns.com"
echo "  2. In Vercel dashboard → Project → Settings → Domains → add tylerelec.proppyai.io"
echo "  3. Once DNS propagates (~2-5 min), Tyler can hit https://tylerelec.proppyai.io"
