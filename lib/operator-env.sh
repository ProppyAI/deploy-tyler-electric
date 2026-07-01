#!/usr/bin/env bash
# operator-env.sh — single source of truth for loading operator-level secrets.
#
# Sources ~/.harness/operator.env into the current shell (or from the path
# named by HARNESS_OPERATOR_ENV if set), then applies back-compat var aliases
# so older scripts that reference legacy names keep working.
#
# Bash python-side loaders (lib/tenant_probe.py:load_env) already auto-read
# this file. This shim does the same for shell scripts so neither side asks
# the operator to `export VAR=...` for routine operations.
#
# DESIGN POINT: The operator MUST never paste a secret value into the agent
# conversation. Secrets live in ~/.harness/operator.env (chmod 600) and
# scripts source this helper to pick them up. If a needed key is missing,
# the script fails LOUDLY with a message telling the operator to edit the
# file — no export-into-terminal workarounds.
#
# Usage (from a HARNESS canonical script):
#     SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#     # shellcheck source=../lib/operator-env.sh
#     source "$SCRIPT_DIR/../lib/operator-env.sh"
#     require_operator_secret SUPABASE_ACCESS_TOKEN \
#       "Supabase PAT from https://supabase.com/dashboard/account/tokens"

set -u

_HARNESS_OPERATOR_ENV_DEFAULT="$HOME/.harness/operator.env"
HARNESS_OPERATOR_ENV="${HARNESS_OPERATOR_ENV:-$_HARNESS_OPERATOR_ENV_DEFAULT}"

# Source the operator env file if present. Quoted-value awareness mirrors
# the python parser in lib/tenant_probe.py:_parse_env_file (matched single
# or double quotes are stripped; unquoted ' #...' is a comment).
if [[ -f "$HARNESS_OPERATOR_ENV" ]]; then
  # Validate perms — operator.env holds PATs, MUST be chmod 600 or stricter.
  # If wider, refuse to source and tell the operator to fix.
  _perms=$(stat -f '%Lp' "$HARNESS_OPERATOR_ENV" 2>/dev/null || stat -c '%a' "$HARNESS_OPERATOR_ENV" 2>/dev/null || echo "?")
  if [[ "$_perms" != "600" && "$_perms" != "400" && "$_perms" != "?" ]]; then
    echo "ERROR: $HARNESS_OPERATOR_ENV has loose permissions ($_perms); refuse to source." >&2
    echo "       chmod 600 $HARNESS_OPERATOR_ENV  # then retry" >&2
    return 1 2>/dev/null || exit 1
  fi
  while IFS= read -r _line || [[ -n "$_line" ]]; do
    # Skip blanks and comments
    _trimmed="${_line#"${_line%%[![:space:]]*}"}"
    [[ -z "$_trimmed" || "$_trimmed" == \#* ]] && continue
    # Must contain =
    [[ "$_trimmed" != *=* ]] && continue
    _key="${_trimmed%%=*}"
    _val="${_trimmed#*=}"
    # Strip matched surrounding quotes
    if [[ "$_val" == \"*\" || "$_val" == \'*\' ]]; then
      _val="${_val:1:${#_val}-2}"
    fi
    # Strip inline ' #...' comment on unquoted values
    if [[ "$_val" != \"* && "$_val" != \'* && "$_val" == *" #"* ]]; then
      _val="${_val%% #*}"
    fi
    # Reject non-identifier keys — blocks accidental shadowing of dangerous
    # env names (BASH_ENV, PROMPT_COMMAND, LD_PRELOAD, etc. via malformed
    # lines or whitespace-prefixed keys) and surfaces typos at load time.
    if [[ ! "$_key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
      echo "harness operator-env: WARNING: skipping line with invalid identifier: '$_key'" >&2
      continue
    fi
    # Export so child processes inherit
    export "$_key=$_val"
  done < "$HARNESS_OPERATOR_ENV"
  unset _line _trimmed _key _val _perms
fi

# --- Back-compat aliases ----------------------------------------------------
# Some scripts predate operator.env's canonical key names. Alias forward and
# backward so either name works in any script.
#
# Supabase Management API PAT (sbp_*):
#   canonical: SUPABASE_MANAGEMENT_PAT (matches lib/probes/auth_redirect_allowlist.py)
#   legacy:    SUPABASE_ACCESS_TOKEN   (matches deploy-tyler-electric scripts pre-Phase-3)
if [[ -n "${SUPABASE_MANAGEMENT_PAT:-}" && -z "${SUPABASE_ACCESS_TOKEN:-}" ]]; then
  export SUPABASE_ACCESS_TOKEN="$SUPABASE_MANAGEMENT_PAT"
fi
if [[ -n "${SUPABASE_ACCESS_TOKEN:-}" && -z "${SUPABASE_MANAGEMENT_PAT:-}" ]]; then
  export SUPABASE_MANAGEMENT_PAT="$SUPABASE_ACCESS_TOKEN"
fi

# --- Public API -------------------------------------------------------------

# require_operator_secret VAR_NAME [help_text]
#   Returns 0 if $VAR_NAME is set and non-empty. Otherwise prints a clear
#   error pointing at ~/.harness/operator.env and exits 1.
#   NEVER prints the value.
require_operator_secret() {
  local var="$1"
  local help="${2:-}"
  if [[ -z "${!var:-}" ]]; then
    echo "ERROR: operator secret \$$var is not set." >&2
    echo "" >&2
    echo "Add it to $HARNESS_OPERATOR_ENV (chmod 600), e.g.:" >&2
    echo "  echo '${var}=<value>' >> $HARNESS_OPERATOR_ENV" >&2
    echo "  chmod 600 $HARNESS_OPERATOR_ENV" >&2
    if [[ -n "$help" ]]; then
      echo "" >&2
      echo "  ($help)" >&2
    fi
    echo "" >&2
    echo "Do NOT export the value into your terminal — operator.env is the persistent home." >&2
    exit 1
  fi
}

# optional_operator_secret VAR_NAME
#   Soft-fail variant of require_operator_secret. Returns 0 regardless of
#   whether $VAR_NAME is set. When absent, prints a one-line stderr note
#   (without the help text — keep noise low for routine SKIP paths) and
#   leaves the variable unset. Callers MUST handle the empty case
#   (typically by emitting a structured SKIP and proceeding).
#
#   Use cases:
#     - Reviewer subagents that wrap optional external tools (gstack:codex
#       needs OPENAI_API_KEY; absent → SKIP, not fail).
#     - Diagnostic scripts that prefer richer output when a key is present
#       but degrade gracefully when not.
#
#   NEVER prints the value.
optional_operator_secret() {
  local var="$1"
  if [[ -z "${!var:-}" ]]; then
    echo "harness operator-env: optional secret \$$var not set; caller should SKIP." >&2
    return 0
  fi
  return 0
}

# operator_env_path
#   Print the resolved path to operator.env (for debugging / scripts).
operator_env_path() {
  echo "$HARNESS_OPERATOR_ENV"
}
