#!/usr/bin/env bash
# issue-bot-readonly-jwt.sh — Issues a JWT signed with role: bot_readonly
# for the agent's read-only Supabase client.
#
# Background: client-app's safety.ts:22-24 reads SUPABASE_BOT_READONLY_KEY
# from env and throws when absent. The `bot_readonly` Postgres role is
# created in migration 00013_agent_loop_tables.sql with SELECT-only grants
# and a 10s statement_timeout. A JWT signed with `role: bot_readonly`
# tells PostgREST/Supabase to authenticate the request as that role.
#
# Without this JWT, the agent's `find_relevant_context` and `query_database`
# tools fail every call, and per current agent-loop code, the agent retries
# infra-error tool calls until the Anthropic request times out. PR #125
# documented "Issuer script: TBD" in .env.local.example; this is that
# script.
#
# REQUIRED ENV (auto-sourced from ~/.harness/operator.env):
#   SUPABASE_JWT_SECRET    — the tenant's Supabase JWT secret. Fetch from
#                            Supabase Studio → Settings → API → JWT Secret.
#                            Per-tenant secret; for a multi-tenant setup,
#                            switch operator.env between tenants by symlinking
#                            or rewriting the file before invoking. Never pass
#                            the secret as an inline env prefix on the command
#                            line — it leaks into shell history and `ps` output.
#
# OPTIONAL ENV:
#   ROLE                   — JWT role claim (default: bot_readonly). The script
#                            is named and intended for the bot_readonly role;
#                            override only when you know why (e.g. anon for
#                            test fixtures). NEVER set to service_role or
#                            postgres — those bypass all RLS.
#   EXPIRY_SECONDS         — JWT expiry, seconds from now (default: 315360000
#                            = ~10 years, matching Supabase legacy key
#                            convention; rotate at least annually or on any
#                            operator off-boarding by reissuing + redeploying)
#   ISS                    — JWT issuer claim (default: supabase)
#
# Outputs the signed JWT to stdout. Exits non-zero on missing secret or
# signing failure. NEVER echoes the secret value itself — but the JWT
# OUTPUT is a bearer credential for the bot_readonly role and must be
# treated with the same confidentiality as SUPABASE_JWT_SECRET. Never
# paste the JWT into chat, Slack, a PR body, or commit it to a repo.
#
# Usage (single tenant — Tyler):
#   cd HARNESS/client-app
#   JWT=$(bash ../scripts/issue-bot-readonly-jwt.sh)
#   netlify env:set SUPABASE_BOT_READONLY_KEY "$JWT" --secret
#   netlify deploy --build --prod
#
# The --secret flag stores the value in Netlify's Sensitive tier (hidden
# from the dashboard and `netlify env:get`). Without it any team member
# with site access can read the JWT verbatim.
#
# Usage (verify a JWT after setting — masks the secret half):
#   bash scripts/issue-bot-readonly-jwt.sh | cut -c1-40
#
# Rotation: re-run this script + netlify env:set + redeploy. The new JWT
# supersedes the old one server-side as soon as the deploy promotes.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=../lib/operator-env.sh
source "$HARNESS_ROOT/lib/operator-env.sh"

require_operator_secret SUPABASE_JWT_SECRET \
  "Supabase JWT secret from Studio → Settings → API → JWT Secret. Per-tenant."

ROLE="${ROLE:-bot_readonly}"
EXPIRY_SECONDS="${EXPIRY_SECONDS:-315360000}"
ISS="${ISS:-supabase}"

# Inline Python signer. Stdlib-only — no third-party JWT lib required (which
# matters because HARNESS/scripts/ are operator-laptop scripts that must
# run on a fresh macOS without `pip install`).
ROLE="$ROLE" EXPIRY_SECONDS="$EXPIRY_SECONDS" ISS="$ISS" python3 <<'PY'
import base64, hmac, hashlib, json, os, sys, time

secret = os.environ.get('SUPABASE_JWT_SECRET', '').encode('utf-8')
if not secret:
    print('issue-bot-readonly-jwt: SUPABASE_JWT_SECRET empty after env load', file=sys.stderr)
    sys.exit(1)

role = os.environ['ROLE']
# Allowlist: bot_readonly (default, this script's purpose), anon (test fixtures),
# authenticated (future user-scoped tools). service_role / postgres are
# intentionally excluded — they bypass RLS and require a separate, explicitly
# named script per principle of least surprise.
ALLOWED_ROLES = {'bot_readonly', 'anon', 'authenticated'}
if role not in ALLOWED_ROLES:
    print(f'issue-bot-readonly-jwt: ROLE must be one of {sorted(ALLOWED_ROLES)}, got {role!r}', file=sys.stderr)
    sys.exit(1)
iss = os.environ['ISS']
try:
    expiry_seconds = int(os.environ['EXPIRY_SECONDS'])
except ValueError:
    print('issue-bot-readonly-jwt: EXPIRY_SECONDS must be an integer', file=sys.stderr)
    sys.exit(1)
if expiry_seconds <= 0:
    print('issue-bot-readonly-jwt: EXPIRY_SECONDS must be > 0', file=sys.stderr)
    sys.exit(1)
# Cap at 20 years (2x the documented default). An operator could otherwise
# pass EXPIRY_SECONDS=99999999999 and get a 3000-year JWT.
MAX_EXPIRY_SECONDS = 20 * 365 * 24 * 3600
if expiry_seconds > MAX_EXPIRY_SECONDS:
    print(f'issue-bot-readonly-jwt: EXPIRY_SECONDS exceeds 20-year ceiling ({MAX_EXPIRY_SECONDS})', file=sys.stderr)
    sys.exit(1)

now = int(time.time())
exp = now + expiry_seconds

header = {'alg': 'HS256', 'typ': 'JWT'}
payload = {'role': role, 'iss': iss, 'iat': now, 'exp': exp}

def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode('ascii')

h_b64 = b64url(json.dumps(header, separators=(',', ':')).encode('utf-8'))
p_b64 = b64url(json.dumps(payload, separators=(',', ':')).encode('utf-8'))
signing_input = f'{h_b64}.{p_b64}'.encode('ascii')
sig = hmac.new(secret, signing_input, hashlib.sha256).digest()
print(f'{h_b64}.{p_b64}.{b64url(sig)}')
PY
