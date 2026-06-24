#!/usr/bin/env bash
# tenant_telegram.sh — real Telegram UAT primitives (tg-login / tg-send / tg-read / tg-dialogs).
#
# Network code lives in lib/tg_client.py (Telethon). This wrapper bootstraps a
# dedicated venv, loads operator secrets, resolves the tenant bot @username from
# deploy-<slug>/harness.json, and shells out to the python client.
#
# Tests inject HARNESS_TG_CLIENT (a command that mimics tg_client.py) to stub
# the Telethon layer and skip venv bootstrap.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./operator-env.sh
source "$SCRIPT_DIR/operator-env.sh"
# shellcheck source=./tenant_common.sh
source "$SCRIPT_DIR/tenant_common.sh"

# Resolve the python client command into the caller-local TG_CMD array. Stub
# override wins (tests); otherwise bootstrap ~/.harness/.venv-tg and ensure
# telethon is installed. Using an array (vs an echoed string) keeps the python
# path + script path intact even when $HOME/$SCRIPT_DIR contains spaces — and
# lets call sites quote "${TG_CMD[@]}" instead of relying on word-splitting.
# Pin telethon to the 1.x line: Telethon 2.x is a breaking API change and an
# unbounded install is also a supply-chain footgun (this venv holds the session
# string = full account access).
_TG_TELETHON_SPEC='telethon>=1.36,<2'
_tg_python() {
  if [[ -n "${HARNESS_TG_CLIENT:-}" ]]; then
    # test stub / explicit override: split the command prefix into words.
    read -r -a TG_CMD <<< "$HARNESS_TG_CLIENT"
    return 0
  fi
  local venv="${HARNESS_TG_VENV:-$HOME/.harness/.venv-tg}"
  if [[ ! -x "$venv/bin/python3" ]]; then
    python3 -m venv "$venv" >&2 || { echo "tenant_telegram: venv create failed" >&2; return 1; }
  fi
  if ! "$venv/bin/python3" -c 'import telethon' 2>/dev/null; then
    echo "tenant_telegram: installing telethon into $venv ..." >&2
    # Some Python builds (e.g. Homebrew python@3.x) create venvs WITHOUT pip.
    # Bootstrap it via ensurepip, and always invoke pip as a module
    # ("python3 -m pip") rather than the bin/pip shim, which may be absent.
    if ! "$venv/bin/python3" -m pip --version >/dev/null 2>&1; then
      if ! "$venv/bin/python3" -m ensurepip --upgrade >&2; then
        echo "tenant_telegram: could not bootstrap pip in $venv (python3 -m ensurepip failed)" >&2
        return 1
      fi
    fi
    if ! "$venv/bin/python3" -m pip install -q "$_TG_TELETHON_SPEC" >&2; then
      echo "tenant_telegram: telethon install failed. Manually: $venv/bin/python3 -m pip install '$_TG_TELETHON_SPEC'" >&2
      return 1
    fi
  fi
  TG_CMD=( "$venv/bin/python3" "$SCRIPT_DIR/tg_client.py" )
}

# Resolve channels.telegram.bot_username from deploy-<slug>/harness.json.
_tg_bot_username() {
  local slug="$1" repo json u
  repo="$(resolve_tenant_repo "$slug")" || return 1
  json="$repo/harness.json"
  if [[ ! -f "$json" ]]; then
    echo "tenant_telegram: $json not found" >&2
    return 1
  fi
  u="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("channels",{}).get("telegram",{}).get("bot_username",""))' "$json")"
  if [[ -z "$u" ]]; then
    echo "tenant_telegram: channels.telegram.bot_username missing in $json" >&2
    echo "  add it, e.g.:  \"bot_username\": \"@tylerelec_bot\"" >&2
    return 1
  fi
  echo "$u"
}

tenant_tg_send_main() {
  local slug="${1:-}" text="${2:-}"
  shift 2 2>/dev/null || true
  local dry=0
  [[ "${1:-}" == "--dry-run" ]] && dry=1
  if [[ -z "$slug" || -z "$text" ]]; then
    echo "Usage: harness tenant tg-send <slug> \"<text>\" [--dry-run]" >&2
    return 2
  fi
  local bot
  bot="$(_tg_bot_username "$slug")" || return 1
  if [[ "$dry" == "1" ]]; then
    python3 -c 'import json,sys; print(json.dumps({"dry_run":True,"bot":sys.argv[1],"text":sys.argv[2]}))' "$bot" "$text"
    return 0
  fi
  require_operator_secret TELEGRAM_SESSION_STRING "run: harness tenant tg-login"
  local -a TG_CMD
  _tg_python || return 1
  "${TG_CMD[@]}" send "$bot" "$text"
}

tenant_tg_read_main() {
  local slug="${1:-}"
  shift 2>/dev/null || true
  local since=0 wait=540 quiet=3
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --since) since="${2:-}"; shift 2 2>/dev/null || shift 1 ;;
      --wait)  wait="${2:-}";  shift 2 2>/dev/null || shift 1 ;;
      --quiet) quiet="${2:-}"; shift 2 2>/dev/null || shift 1 ;;
      *) echo "tenant_telegram: unknown flag: $1" >&2; return 2 ;;
    esac
  done
  if [[ -z "$slug" ]]; then
    echo "Usage: harness tenant tg-read <slug> [--since ID] [--wait S] [--quiet S]" >&2
    return 2
  fi
  if [[ -z "$since" || -z "$wait" || -z "$quiet" ]]; then
    echo "tenant_telegram: --since/--wait/--quiet require a value" >&2
    return 2
  fi
  case "${since}${wait}${quiet}" in
    *[!0-9]*) echo "tenant_telegram: --since/--wait/--quiet must be integers" >&2; return 2 ;;
  esac
  local bot
  bot="$(_tg_bot_username "$slug")" || return 1
  require_operator_secret TELEGRAM_SESSION_STRING "run: harness tenant tg-login"
  local -a TG_CMD
  _tg_python || return 1
  "${TG_CMD[@]}" read "$bot" --since "$since" --wait "$wait" --quiet "$quiet"
}

tenant_tg_login_main() {
  local -a TG_CMD
  _tg_python || return 1
  # tg_client writes TELEGRAM_API_ID/API_HASH/SESSION_STRING into this file.
  "${TG_CMD[@]}" login "$HARNESS_OPERATOR_ENV"
}

tenant_tg_dialogs_main() {
  require_operator_secret TELEGRAM_SESSION_STRING "run: harness tenant tg-login"
  local -a TG_CMD
  _tg_python || return 1
  "${TG_CMD[@]}" dialogs
}
