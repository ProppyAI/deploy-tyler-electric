#!/usr/bin/env bash
# tenant_bid_replay.sh — exhaustive bid replay UAT driver.
#
#   harness tenant bid-replay <slug> <fixture.json> [--dry-run] [--cleanup] [--no-assert]
#
# Replays a captured, full-length bid conversation (fixture = an ordered list of
# transcribed user turns) against the DEPLOYED tenant agent path using the
# real-Telegram UAT harness: each turn is tg-send'd to the tenant bot and the
# bot's reply is tg-read back. After the last turn it asserts UP TO QBO that the
# bid persisted and produced exactly one correct estimate — the dead-end that
# Tyler's live incident exposed (no persisted draft -> HISTORY_LIMIT truncation
# wipes the line set -> infinite "confirm descriptions" loop, no create).
#
# This is the C4 deliverable of docs/superpowers/specs/2026-06-09-bid-deterministic-persistence-design.md.
# It is the post-merge/deploy gate: the reproduction is already evidenced by the
# live incident; this must PASS once the fix is deployed, then lives as a durable
# regression guard. Never targets a real customer — the fixture uses the
# placeholder "Validation Jones".
#
# Tests inject HARNESS_TG_CLIENT (a stub that mimics tg_client.py) and
# HARNESS_BID_REPLAY_SQL / HARNESS_BID_REPLAY_QBO_GET to stub the network layer.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=./operator-env.sh
source "$SCRIPT_DIR/operator-env.sh"
# shellcheck source=./tenant_common.sh
source "$SCRIPT_DIR/tenant_common.sh"
# shellcheck source=./tenant_telegram.sh
source "$SCRIPT_DIR/tenant_telegram.sh"
# shellcheck source=./tenant_qbo_read.sh
source "$SCRIPT_DIR/tenant_qbo_read.sh"
# shellcheck source=./tenant_sql.sh
source "$SCRIPT_DIR/tenant_sql.sh"

tenant_bid_replay_usage() {
  cat >&2 <<USAGE
Usage:
  harness tenant bid-replay <slug> <fixture.json> [--dry-run] [--cleanup] [--no-assert]

  Replays a captured bid conversation against the DEPLOYED tenant bot and
  asserts the bid persisted + produced exactly one correct QBO estimate.

  --dry-run     print each turn (and read params) without sending anything
  --no-assert   send the turns but skip the post-run QBO/DB assertions
  --cleanup     after asserting, delete the local test estimate + abandon the
                draft created by this run (QBO-side delete is manual — printed)

Examples:
  harness tenant bid-replay tyler-electric \\
    client-app/tests/uat/bid-long-replay-validation-jones.json
  harness tenant bid-replay tyler-electric <fixture> --dry-run

Reads:
  - TELEGRAM_SESSION_STRING, INTERNAL_JOB_SECRET, SUPABASE_MANAGEMENT_PAT
    (all from ~/.harness/operator.env)
USAGE
}

# Extract a top-level scalar / nested value from the fixture via python3.
_bid_fixture_get() {
  local fixture="$1" expr="$2"
  python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
keys = sys.argv[2].split(".")
v = d
for k in keys:
    if isinstance(v, dict):
        v = v.get(k)
    else:
        v = None
if v is None:
    print("")
elif isinstance(v, (dict, list)):
    # Emit valid JSON so callers can pass the value to json.loads / sys.argv
    print(json.dumps(v))
else:
    print(v)
' "$fixture" "$expr"
}

# Defensive JSON-rows parse shared by every SQL assertion. `harness tenant sql
# --json` returns either {"result":[...]} or a bare [...]; normalize to the row
# list and hand it to the python snippet in $1 (which reads rows from a global
# `rows`). Stubbable via HARNESS_BID_REPLAY_SQL for tests.
_bid_sql() {
  local slug="$1" query="$2"
  if [[ -n "${HARNESS_BID_REPLAY_SQL:-}" ]]; then
    "$HARNESS_BID_REPLAY_SQL" "$slug" "$query"
    return $?
  fi
  tenant_sql_main "$slug" "$query" --json
}

_bid_qbo_get() {
  local slug="$1" entity="$2" id="$3"
  if [[ -n "${HARNESS_BID_REPLAY_QBO_GET:-}" ]]; then
    "$HARNESS_BID_REPLAY_QBO_GET" "$slug" "$entity" "$id"
    return $?
  fi
  tenant_qbo_get_main "$slug" "$entity" "$id"
}

# Run the send/read loop for every fixture turn. Echoes a transcript to stderr
# and leaves the last-seen message id in the file $cursor_file so each tg-read
# only collects genuinely new bot replies.
#
# Per-turn bot-text assertions (A3 Task 10): a turn object may carry two OPTIONAL
# arrays — expect_reply_contains (every substring MUST appear in that turn's
# concatenated bot reply) and expect_reply_absent (no substring may appear).
# Matching is case-insensitive substring (not regex). These prove UX/robustness
# fixes that don't move document totals (e.g. the bot must NOT ask "is that 1
# unit?"). A violation increments BID_REPLAY_TEXT_FAILS (a GLOBAL — this function
# runs under $(...) to capture the cursor on stdout, so a normal `return` count
# would be lost; the caller folds the global into the run RESULT).
#
# Results travel via the global because the function is invoked in a
# command-substitution subshell ONLY when its stdout is captured; the caller
# instead reads BID_REPLAY_TEXT_FAILS directly after a NON-substituted call.
_bid_replay_send_turns() {
  local slug="$1" fixture="$2" wait_s="$3" quiet_s="$4"
  local n total
  total="$(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))["turns"]))' "$fixture")"
  local cursor=0
  BID_REPLAY_TEXT_FAILS=0
  for ((n = 0; n < total; n++)); do
    local text label
    text="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["turns"][int(sys.argv[2])]["text"])' "$fixture" "$n")"
    label="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["turns"][int(sys.argv[2])].get("label",""))' "$fixture" "$n")"
    printf '\n>>> TURN %s/%s [%s]\n%s\n' "$((n + 1))" "$total" "$label" "$text" >&2

    # Optional per-turn fault-injection SQL (A5a watchdog UAT). Runs before tg-send.
    local pre_sql
    pre_sql="$(python3 -c '
import json,sys
fx=json.load(open(sys.argv[1])); n=int(sys.argv[2])
t=next((t for t in fx.get("turns",[]) if t.get("n")==n),{})
sys.stdout.write(t.get("pre_sql") or "")
' "$fixture" "$n")"
    if [ -n "$pre_sql" ]; then
      echo "bid-replay: turn $n pre_sql -> fault-injection SQL" >&2
      _bid_sql "$slug" "$pre_sql" >/dev/null || { echo "bid-replay: pre_sql failed on turn $n" >&2; return 1; }
    fi

    local send_out sent_id
    send_out="$(tenant_tg_send_main "$slug" "$text")" || {
      echo "bid-replay: tg-send failed on turn $n" >&2; return 1; }
    sent_id="$(printf '%s' "$send_out" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("sent_id",0))')"
    if [[ "$sent_id" =~ ^[0-9]+$ ]] && (( sent_id > cursor )); then cursor="$sent_id"; fi

    local read_out
    read_out="$(tenant_tg_read_main "$slug" --since "$cursor" --wait "$wait_s" --quiet "$quiet_s")" || {
      echo "bid-replay: tg-read failed on turn $n" >&2; return 1; }
    # Print bot replies + advance cursor to the newest message id seen. The
    # python snippet emits two lines on stdout: line 1 = new cursor, line 2 =
    # the concatenated reply text for this turn (newlines flattened to spaces)
    # so the bash side can run case-insensitive substring assertions on it.
    local read_parsed reply_text
    read_parsed="$(printf '%s' "$read_out" | python3 -c '
import json, sys
data = json.load(sys.stdin)
cur = int(sys.argv[1])
texts = []
for m in data:
    t = (m.get("text","") or "")
    sys.stderr.write("    <<< BOT: " + t[:600].replace("\n"," ") + "\n")
    texts.append(t)
    if int(m.get("id", 0)) > cur:
        cur = int(m["id"])
print(cur)
print(" ".join(texts).replace("\n", " "))
' "$cursor")"
    cursor="$(printf '%s' "$read_parsed" | sed -n '1p')"
    reply_text="$(printf '%s' "$read_parsed" | sed -n '2,$p')"

    _bid_assert_turn_text "$fixture" "$n" "$label" "$reply_text"
  done
  printf '%s' "$cursor"
}

# Per-turn bot-text assertion helper. Reads expect_reply_contains /
# expect_reply_absent (both optional substring arrays) for turn $idx and checks
# them case-insensitively against $reply_text. Increments the GLOBAL
# BID_REPLAY_TEXT_FAILS on any violation. bash-3.2 safe: no mapfile, no
# associative arrays, no ${var,,}; lowercasing via tr, substring match via a
# case glob, array population via while-read of a NUL-delimited python emit.
_bid_assert_turn_text() {
  local fixture="$1" idx="$2" label="$3" reply_text="$4"
  local reply_lc
  reply_lc="$(printf '%s' "$reply_text" | tr '[:upper:]' '[:lower:]')"
  local turn_no=$((idx + 1))

  # expect_reply_contains — each substring MUST be present.
  local sub
  while IFS= read -r -d '' sub; do
    [[ -z "$sub" ]] && continue
    local sub_lc
    sub_lc="$(printf '%s' "$sub" | tr '[:upper:]' '[:lower:]')"
    case "$reply_lc" in
      *"$sub_lc"*)
        echo "  [PASS] turn ${turn_no} [${label}]: reply contains \"${sub}\"" >&2 ;;
      *)
        echo "  [FAIL] turn ${turn_no} [${label}]: reply missing \"${sub}\"" >&2
        BID_REPLAY_TEXT_FAILS=$((BID_REPLAY_TEXT_FAILS + 1)) ;;
    esac
  done < <(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
t = d["turns"][int(sys.argv[2])]
for s in (t.get("expect_reply_contains") or []):
    sys.stdout.write(str(s) + "\0")
' "$fixture" "$idx")

  # expect_reply_absent — no substring may be present.
  while IFS= read -r -d '' sub; do
    [[ -z "$sub" ]] && continue
    local sub_lc
    sub_lc="$(printf '%s' "$sub" | tr '[:upper:]' '[:lower:]')"
    case "$reply_lc" in
      *"$sub_lc"*)
        echo "  [FAIL] turn ${turn_no} [${label}]: reply must NOT contain \"${sub}\"" >&2
        BID_REPLAY_TEXT_FAILS=$((BID_REPLAY_TEXT_FAILS + 1)) ;;
      *)
        echo "  [PASS] turn ${turn_no} [${label}]: reply absent \"${sub}\"" >&2 ;;
    esac
  done < <(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
t = d["turns"][int(sys.argv[2])]
for s in (t.get("expect_reply_absent") or []):
    sys.stdout.write(str(s) + "\0")
' "$fixture" "$idx")
}

# QBO read-back assertion for a single document.
# Usage: _bid_assert_qbo_doc <slug> <entity: estimate|invoice> <qbo_id> <exp_total> [<fails_var>]
# Writes [PASS]/[FAIL] lines to stderr; increments the caller's $fails counter
# by echoing a delta ("0" or "N") to stdout so the caller can add it.
# This avoids subshell-variable-loss while keeping the function self-contained.
_bid_assert_qbo_doc() {
  local slug="$1" entity="$2" qbo_id="$3" exp_total="$4"
  local local_fails=0
  if ! [[ "$qbo_id" =~ ^[0-9]+$ ]]; then
    echo "  [FAIL] ${entity} qbo_id is not numeric ('$qbo_id') — cannot read back from QBO" >&2
    echo "1"; return 0
  fi
  local qbo_json
  qbo_json="$(_bid_qbo_get "$slug" "$entity" "$qbo_id" 2>/dev/null || echo '{}')"
  # Determine the entity key name in the QBO response ("Estimate" or "Invoice").
  local entity_key
  if [[ "$entity" == "estimate" ]]; then
    entity_key="Estimate"
  else
    entity_key="Invoice"
  fi
  local qbo_check
  qbo_check="$(printf '%s' "$qbo_json" | python3 -c '
import json, sys
exp_total  = float(sys.argv[1])
entity_key = sys.argv[2]
try:
    d = json.load(sys.stdin)
except Exception:
    print("PARSE_ERR|NONE|PARSE_ERR|qbo response did not parse as JSON"); sys.exit(0)
doc = d.get("result", d)
if isinstance(doc, dict) and entity_key in doc:
    doc = doc[entity_key]
total = doc.get("TotalAmt")
lines = [l for l in doc.get("Line", []) if l.get("DetailType") == "SalesItemLineDetail"]
nonnon = [l for l in lines
          if (l.get("SalesItemLineDetail", {}).get("TaxCodeRef", {}) or {}).get("value") != "NON"]
ok_total = total is not None and abs(float(total) - exp_total) < 0.005
print(("OK" if ok_total else "BAD") + "|" + str(total) + "|" + str(len(nonnon)) + "|")
' "$exp_total" "$entity_key" 2>/dev/null || echo "ERR|?|?|")"
  local qstat qtotal qnonnon _rest
  IFS='|' read -r qstat qtotal qnonnon _rest <<< "$qbo_check"
  if [[ "$qstat" == "OK" ]]; then
    echo "  [PASS] QBO ${entity} ${qbo_id} TotalAmt=${qtotal} == quote" >&2
  else
    echo "  [FAIL] QBO ${entity} ${qbo_id} TotalAmt=${qtotal} (expected ${exp_total})" >&2
    local_fails=$((local_fails + 1))
  fi
  if [[ "$qnonnon" == "0" ]]; then
    echo "  [PASS] every QBO line TaxCodeRef=NON (bid = no tax)" >&2
  else
    echo "  [FAIL] $qnonnon QBO line(s) NOT marked NON — bid lines must be non-taxable" >&2
    local_fails=$((local_fails + 1))
  fi
  echo "$local_fails"
}

# Assert multi-doc expectations from the fixture.
# Reads expected_estimates and expected_invoices arrays; for each entry asserts
# exactly one matching row + correct total + QBO read-back.
# Prints PASS/FAIL lines to stderr. Results travel via GLOBALS — the caller
# must invoke this function DIRECTLY (not via $(...)): a command-substitution
# subshell would silently drop both BID_REPLAY_MULTI_FAILS and the
# BID_REPLAY_MULTI_CLEANUP stash that --cleanup depends on.
#   BID_REPLAY_MULTI_FAILS   — number of failed assertions (int)
#   BID_REPLAY_MULTI_CLEANUP — tab-separated cleanup entries (see stash below)
# bash-3.2 safe: uses while-read loop driven by python3, no mapfile/declare -A.
_bid_assert_multi_docs() {
  local slug="$1" fixture="$2" start_ts="$3"
  local total_fails=0
  BID_REPLAY_MULTI_FAILS=0

  # Author-error guard: a key that is PRESENT but holds an empty array would
  # suppress the legacy single-estimate block while asserting nothing — a
  # silent no-op PASS. Hard-fail instead.
  local empty_keys
  empty_keys="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
exp = d.get("expect", {})
bad = [k for k in ("expected_estimates", "expected_invoices")
       if k in exp and isinstance(exp[k], list) and len(exp[k]) == 0]
print(",".join(bad))
' "$fixture" 2>/dev/null || true)"
  if [[ -n "$empty_keys" ]]; then
    echo "  [FAIL] expected_estimates/expected_invoices present but empty — author error (${empty_keys})" >&2
    total_fails=$((total_fails + 1))
  fi

  # Emit one TSV line per entry: type TAB doc_match TAB exp_total TAB local_status TAB email_status
  # python3 reads both arrays and concatenates. local_status/email_status are empty when absent.
  local entries
  entries="$(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
exp = d.get("expect", {})
for e in exp.get("expected_estimates", []):
    print("estimate\t" + str(e["doc_match"]) + "\t" + str(e["grand_total"]) + "\t" + str(e.get("local_status","")) + "\t" + str(e.get("email_status","")))
for e in exp.get("expected_invoices", []):
    print("invoice\t"  + str(e["doc_match"]) + "\t" + str(e["amount"]) + "\t" + str(e.get("local_status","")) + "\t" + str(e.get("email_status","")))
' "$fixture" 2>/dev/null || true)"

  [[ -z "$entries" ]] && { BID_REPLAY_MULTI_FAILS="$total_fails"; return 0; }

  while IFS= read -r entry_line; do
    [[ -z "$entry_line" ]] && continue
    # Split on tab: field1=entity type, field2=doc_match pattern, field3=expected total,
    # field4=expected local_status (empty when not asserted), field5=expected email_status
    local etype doc_match exp_total exp_local_status exp_email_status
    etype="$(             printf '%s' "$entry_line" | cut -f1)"
    doc_match="$(         printf '%s' "$entry_line" | cut -f2)"
    exp_total="$(         printf '%s' "$entry_line" | cut -f3)"
    exp_local_status="$(  printf '%s' "$entry_line" | cut -f4)"
    exp_email_status="$(  printf '%s' "$entry_line" | cut -f5)"

    # Validate doc_match: allow SQL LIKE chars (% _ letters digits dash dot space)
    # plus common punctuation used in doc numbers. Reject shell-injection chars.
    # NOTE: the backslash-space in the bracket expression is a literal SPACE.
    # Do NOT widen it to [[:space:]] — that would admit tab/newline, which
    # would reach (and corrupt) the tab-delimited TSV entry pipeline above.
    local _dm_pat='^[A-Za-z0-9%_.\ /-]+$'
    if ! [[ "$doc_match" =~ $_dm_pat ]]; then
      echo "  [FAIL] ${etype} doc_match has illegal chars: '${doc_match}'" >&2
      total_fails=$((total_fails + 1))
      continue
    fi

    # Choose the right table + columns.
    local table total_col qbo_col sql_query
    if [[ "$etype" == "estimate" ]]; then
      table="estimates"
      total_col="grand_total"
      qbo_col="qbo_estimate_id"
      sql_query="SELECT id, doc_number, status, grand_total, qbo_estimate_id FROM estimates WHERE doc_number LIKE '${doc_match}' AND created_at > '${start_ts}' ORDER BY created_at DESC"
    else
      table="invoices"
      total_col="amount"
      qbo_col="qbo_invoice_id"
      sql_query="SELECT id, doc_number, status, amount, qbo_invoice_id FROM invoices WHERE doc_number LIKE '${doc_match}' AND created_at > '${start_ts}' ORDER BY created_at DESC"
    fi

    local row_json
    row_json="$(_bid_sql "$slug" "$sql_query")"
    local row_count loc_total loc_status qbo_id doc_number local_id
    row_count="$(printf '%s' "$row_json" | python3 -c '
import json,sys
d=json.load(sys.stdin); r=d.get("result",d) if isinstance(d,dict) else d
print(len(r))' 2>/dev/null || echo "-1")"
    loc_total="$( printf '%s' "$row_json" | python3 -c '
import json,sys,os
d=json.load(sys.stdin); r=d.get("result",d) if isinstance(d,dict) else d
col=sys.argv[1]
print(r[0].get(col,"") if r else "")' "$total_col" 2>/dev/null || echo "")"
    loc_status="$(printf '%s' "$row_json" | python3 -c '
import json,sys
d=json.load(sys.stdin); r=d.get("result",d) if isinstance(d,dict) else d
print((r[0].get("status") or "") if r else "")' 2>/dev/null || echo "")"
    qbo_id="$(    printf '%s' "$row_json" | python3 -c '
import json,sys
d=json.load(sys.stdin); r=d.get("result",d) if isinstance(d,dict) else d
col=sys.argv[1]
print(r[0].get(col,"") if r else "")' "$qbo_col"    2>/dev/null || echo "")"
    doc_number="$(printf '%s' "$row_json" | python3 -c '
import json,sys
d=json.load(sys.stdin); r=d.get("result",d) if isinstance(d,dict) else d
print(r[0].get("doc_number","") if r else "")' 2>/dev/null || echo "")"
    local_id="$(  printf '%s' "$row_json" | python3 -c '
import json,sys
d=json.load(sys.stdin); r=d.get("result",d) if isinstance(d,dict) else d
print(r[0].get("id","") if r else "")' 2>/dev/null || echo "")"

    # Assert exactly 1 match.
    if [[ "$row_count" == "1" ]]; then
      # Assert total matches.
      if python3 -c 'import sys; sys.exit(0 if abs(float(sys.argv[1] or 0) - float(sys.argv[2])) < 0.005 else 1)' \
          "${loc_total:-0}" "$exp_total" 2>/dev/null; then
        echo "  [PASS] ${etype} ${doc_match}: 1 match, ${total_col}=${loc_total}" >&2
      else
        echo "  [FAIL] ${etype} ${doc_match}: ${total_col}=${loc_total} (expected ${exp_total})" >&2
        total_fails=$((total_fails + 1))
      fi
      # Local status assertion (only when expected non-empty).
      if [[ -n "$exp_local_status" ]]; then
        if [[ "$loc_status" == "$exp_local_status" ]]; then
          echo "  [PASS] ${etype} ${doc_match}: local status = ${loc_status}" >&2
        else
          echo "  [FAIL] ${etype} ${doc_match}: local status = ${loc_status} (expected ${exp_local_status})" >&2
          total_fails=$((total_fails + 1))
        fi
      fi
      # QBO read-back.
      if [[ -n "$qbo_id" && "$qbo_id" != "None" ]]; then
        local qbo_delta
        qbo_delta="$(_bid_assert_qbo_doc "$slug" "$etype" "$qbo_id" "$exp_total")"
        total_fails=$((total_fails + qbo_delta))
        # QBO EmailStatus assertion (only when expected non-empty). The outer
        # guard is non-empty/non-None, NOT numeric, so add an explicit numeric
        # guard here to match the total path's protection (_bid_assert_qbo_doc
        # checks numeric internally) — never fire a real HTTP read with a bad id.
        if [[ -n "$exp_email_status" && "$qbo_id" =~ ^[0-9]+$ ]]; then
          local _eq_json _es
          _eq_json="$(_bid_qbo_get "$slug" "$etype" "$qbo_id" 2>/dev/null || echo '{}')"
          _es="$(printf '%s' "$_eq_json" | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: print(""); sys.exit(0)
doc=d.get("result",d)
for k in ("Estimate","Invoice"):
    if isinstance(doc,dict) and k in doc: doc=doc[k]; break
print(doc.get("EmailStatus","") if isinstance(doc,dict) else "")' 2>/dev/null || echo "")"
          if [[ "$_es" == "$exp_email_status" ]]; then
            echo "  [PASS] ${etype} ${doc_match}: QBO EmailStatus = ${_es}" >&2
          else
            echo "  [FAIL] ${etype} ${doc_match}: QBO EmailStatus = ${_es} (expected ${exp_email_status})" >&2
            total_fails=$((total_fails + 1))
          fi
        fi
      else
        echo "  [FAIL] ${etype} ${doc_match} (${doc_number:-?}) has no ${qbo_col} — never synced to QBO" >&2
        total_fails=$((total_fails + 1))
      fi
      # Stash for cleanup: one entry per doc, fields joined by the unit
      # separator US ($'\x1f') — a control char that cannot appear in a
      # UUID, entity type, doc_number, or QBO id, so a literal ':' (or any
      # other punctuation) inside doc_number cannot corrupt the cut parse.
      # Entries themselves remain tab-separated.
      local _us=$'\x1f'
      BID_REPLAY_MULTI_CLEANUP="${BID_REPLAY_MULTI_CLEANUP:-}${local_id}${_us}${etype}${_us}${doc_number}${_us}${qbo_id}	"
    else
      echo "  [FAIL] ${etype} ${doc_match}: ${row_count} match(es) (expected exactly 1)" >&2
      total_fails=$((total_fails + 1))
    fi
  done <<< "$entries"

  BID_REPLAY_MULTI_FAILS="$total_fails"
}

# Service-desk assertions (expect.service_desk). Results travel via GLOBALS
# (direct call, no subshell — same rationale as _bid_assert_multi_docs):
#   BID_REPLAY_SD_FAILS   — failed assertion count (int)
#   BID_REPLAY_SD_CLEANUP — newline-separated service_desk ids for --cleanup
# Fixture schema:
#   "service_desk": {
#     "total_count": 1,                                          (optional)
#     "expected_tickets": [{"request_match": "supply house", "count": 1}],
#     "absent_tickets":   [{"request_match": "gps tracking"}]
#   }
# All counts are scoped to category='feature-request' AND created_at>start_ts.
_bid_assert_service_desk() {
  local slug="$1" fixture="$2" start_ts="$3"
  BID_REPLAY_SD_FAILS=0
  BID_REPLAY_SD_CLEANUP=""
  local has_sd
  has_sd="$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
print("1" if isinstance(d.get("expect",{}).get("service_desk"),dict) else "0")' "$fixture" 2>/dev/null || echo 0)"
  [[ "$has_sd" == "1" ]] || return 0

  local _us=$'\x1f'
  local spec
  spec="$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1])); sd=d.get("expect",{}).get("service_desk",{})
US="\x1f"; out=[]
tc=sd.get("total_count")
if tc is not None: out.append("total"+US+US+str(tc))
for e in sd.get("expected_tickets",[]) or []:
    out.append("expect"+US+str(e.get("request_match","")).lower()+US+str(e.get("count",1)))
for e in sd.get("absent_tickets",[]) or []:
    out.append("absent"+US+str(e.get("request_match","")).lower()+US+"0")
print("\n".join(out))' "$fixture" 2>/dev/null)"

  # Silent-skip guard: has_sd=1 means the fixture DECLARED a service_desk
  # block — an empty spec (python parse failure or empty block) would skip
  # the loop and read as PASS while asserting NOTHING. Fail loudly instead.
  if [[ -z "$spec" ]]; then
    echo "  [FAIL] service_desk: expect.service_desk present but produced no assertions (parse failure or empty block)" >&2
    BID_REPLAY_SD_FAILS=$((BID_REPLAY_SD_FAILS + 1))
    return 0
  fi

  # Pattern guard mirrors the customer_name_match guard: lowercase alnum,
  # space, and . , % - only — no quotes/semicolons/backslashes (SQL safety).
  local _pat_guard='^[a-z0-9 .,%-]+$'
  local line typ pat cnt n_json n
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    typ="$(printf '%s' "$line" | cut -d"$_us" -f1)"
    pat="$(printf '%s' "$line" | cut -d"$_us" -f2)"
    cnt="$(printf '%s' "$line" | cut -d"$_us" -f3)"
    if ! [[ "$cnt" =~ ^[0-9]+$ ]]; then
      echo "  [FAIL] service_desk ${typ}: count is not an integer ('$cnt')" >&2
      BID_REPLAY_SD_FAILS=$((BID_REPLAY_SD_FAILS + 1)); continue
    fi
    if [[ "$typ" == "total" ]]; then
      n_json="$(_bid_sql "$slug" "SELECT count(*) AS n FROM service_desk WHERE category='feature-request' AND created_at > '${start_ts}'")"
    else
      if ! [[ "$pat" =~ $_pat_guard ]]; then
        echo "  [FAIL] service_desk ${typ}: request_match has illegal chars: '$pat'" >&2
        BID_REPLAY_SD_FAILS=$((BID_REPLAY_SD_FAILS + 1)); continue
      fi
      n_json="$(_bid_sql "$slug" "SELECT count(*) AS n FROM service_desk WHERE category='feature-request' AND created_at > '${start_ts}' AND request ILIKE '%${pat}%'")"
    fi
    n="$(printf '%s' "$n_json" | python3 -c '
import json,sys
d=json.load(sys.stdin); r=d.get("result",d) if isinstance(d,dict) else d
print(r[0].get("n","") if r else "ERR")' 2>/dev/null || echo ERR)"
    if [[ "$n" == "$cnt" ]]; then
      echo "  [PASS] service_desk ${typ} '${pat}' = $n" >&2
    else
      echo "  [FAIL] service_desk ${typ} '${pat}' = $n (expected $cnt)" >&2
      BID_REPLAY_SD_FAILS=$((BID_REPLAY_SD_FAILS + 1))
    fi
  done <<< "$spec"

  # Stash run-window ticket ids for --cleanup. Includes action-failed rows so
  # a PRE-FIX gate run (today's unilateral escalate_to_team path) also cleans.
  local ids_json
  ids_json="$(_bid_sql "$slug" "SELECT id FROM service_desk WHERE created_at > '${start_ts}' AND channel = 'telegram' AND category IN ('feature-request','action-failed')")"
  BID_REPLAY_SD_CLEANUP="$(printf '%s' "$ids_json" | python3 -c '
import json,sys
d=json.load(sys.stdin); r=d.get("result",d) if isinstance(d,dict) else d
print("\n".join(str(x.get("id","")) for x in (r or [])))' 2>/dev/null || true)"
  return 0
}

# Customer assertions (expect.customers). Direct call, global for the fail
# count (same rationale as _bid_assert_service_desk — a command-substitution
# subshell would drop the count). For each expected entry: name count (global,
# NOT start_ts-scoped — the hardening customer is a permanent reusable
# invariant, same rationale as the Validation-Jones check), optional
# phone/email exact match on the newest matching row, and qbo_synced →
# qbo_customer_id non-empty AND a QBO read-back DisplayName match (verify up to
# the system of record).
#   BID_REPLAY_CUST_FAILS — failed assertion count (int)
_bid_assert_customers() {
  local slug="$1" fixture="$2"
  BID_REPLAY_CUST_FAILS=0
  local has_c
  has_c="$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
print("1" if isinstance(d.get("expect",{}).get("customers"),dict) else "0")' "$fixture" 2>/dev/null || echo 0)"
  [[ "$has_c" == "1" ]] || return 0

  local _us=$'\x1f'
  local spec
  spec="$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1])); c=d.get("expect",{}).get("customers",{})
US="\x1f"; out=[]
for e in c.get("expected",[]) or []:
    out.append(US.join([
        str(e.get("name_match","")),
        str(e.get("count","")),
        str(e.get("phone","")),
        str(e.get("email","")),
        "1" if e.get("qbo_synced") else "0",
    ]))
print("\n".join(out))' "$fixture" 2>/dev/null)"
  if [[ -z "$spec" ]]; then
    echo "  [FAIL] customers: expect.customers present but produced no assertions (parse failure or empty block)" >&2
    BID_REPLAY_CUST_FAILS=$((BID_REPLAY_CUST_FAILS + 1))
    return 0
  fi

  # Guards (SQL-interpolation safety): name uses the lowercase-name regex
  # charset; phone/email a conservative literal set. Anything else → FAIL,
  # never a silent skip.
  # NOTE: name_match is interpolated into a Postgres `~` regex — an overly broad
  # pattern (metachars like .|()) widens the match and weakens the "exactly one"
  # count invariant; fixtures should use a literal name.
  local _name_guard='^[a-z0-9 .?*+^$()|-]+$'
  local _val_guard='^[a-z0-9@+._ -]+$'
  local line nm cnt phone email qsync
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    nm="$(   printf '%s' "$line" | cut -d"$_us" -f1)"
    cnt="$(  printf '%s' "$line" | cut -d"$_us" -f2)"
    phone="$(printf '%s' "$line" | cut -d"$_us" -f3)"
    email="$(printf '%s' "$line" | cut -d"$_us" -f4)"
    qsync="$(printf '%s' "$line" | cut -d"$_us" -f5)"
    if ! [[ "$nm" =~ $_name_guard ]]; then
      echo "  [FAIL] customers: name_match has illegal chars: '$nm'" >&2
      BID_REPLAY_CUST_FAILS=$((BID_REPLAY_CUST_FAILS + 1)); continue
    fi

    # count
    if [[ -n "$cnt" ]]; then
      if ! [[ "$cnt" =~ ^[0-9]+$ ]]; then
        echo "  [FAIL] customers '${nm}': count is not an integer ('$cnt')" >&2
        BID_REPLAY_CUST_FAILS=$((BID_REPLAY_CUST_FAILS + 1))
      else
        local cj cn
        cj="$(_bid_sql "$slug" "SELECT count(*) AS n FROM clients WHERE lower(name) ~ '${nm}'")"
        cn="$(printf '%s' "$cj" | python3 -c '
import json,sys
d=json.load(sys.stdin); r=d.get("result",d) if isinstance(d,dict) else d
print(r[0].get("n","") if r else "ERR")' 2>/dev/null || echo ERR)"
        if [[ "$cn" == "$cnt" ]]; then
          echo "  [PASS] customers '${nm}' count = $cn (expected $cnt)" >&2
        else
          echo "  [FAIL] customers '${nm}' count = $cn (expected $cnt)" >&2
          BID_REPLAY_CUST_FAILS=$((BID_REPLAY_CUST_FAILS + 1))
        fi
      fi
    fi

    # newest matching row's fields (one fetch, reused for phone/email/qbo).
    # Only fetched when at least one field that consumes it is requested — an
    # entry that sets only name_match/count needs no row fetch.
    if [[ -n "$phone" || -n "$email" || "$qsync" == "1" ]]; then
      local rj
      rj="$(_bid_sql "$slug" "SELECT phone, email, qbo_customer_id FROM clients WHERE lower(name) ~ '${nm}' ORDER BY created_at DESC LIMIT 1")"

      # phone
      if [[ -n "$phone" ]]; then
        if ! [[ "$phone" =~ $_val_guard ]]; then
          echo "  [FAIL] customers '${nm}': phone has illegal chars" >&2
          BID_REPLAY_CUST_FAILS=$((BID_REPLAY_CUST_FAILS + 1))
        else
          local gotp
          gotp="$(printf '%s' "$rj" | python3 -c '
import json,sys
d=json.load(sys.stdin); r=d.get("result",d) if isinstance(d,dict) else d
print((r[0].get("phone") or "") if r else "")' 2>/dev/null || echo "")"
          if [[ "$gotp" == "$phone" ]]; then
            echo "  [PASS] customers '${nm}' phone = $gotp" >&2
          else
            echo "  [FAIL] customers '${nm}' phone = $gotp (expected $phone)" >&2
            BID_REPLAY_CUST_FAILS=$((BID_REPLAY_CUST_FAILS + 1))
          fi
        fi
      fi

      # email
      if [[ -n "$email" ]]; then
        if ! [[ "$email" =~ $_val_guard ]]; then
          echo "  [FAIL] customers '${nm}': email has illegal chars" >&2
          BID_REPLAY_CUST_FAILS=$((BID_REPLAY_CUST_FAILS + 1))
        else
          local gote
          gote="$(printf '%s' "$rj" | python3 -c '
import json,sys
d=json.load(sys.stdin); r=d.get("result",d) if isinstance(d,dict) else d
print((r[0].get("email") or "") if r else "")' 2>/dev/null || echo "")"
          if [[ "$gote" == "$email" ]]; then
            echo "  [PASS] customers '${nm}' email = $gote" >&2
          else
            echo "  [FAIL] customers '${nm}' email = $gote (expected $email)" >&2
            BID_REPLAY_CUST_FAILS=$((BID_REPLAY_CUST_FAILS + 1))
          fi
        fi
      fi

      # qbo_synced: id non-empty + QBO read-back DisplayName match
      if [[ "$qsync" == "1" ]]; then
        local qid
        qid="$(printf '%s' "$rj" | python3 -c '
import json,sys
d=json.load(sys.stdin); r=d.get("result",d) if isinstance(d,dict) else d
print((r[0].get("qbo_customer_id") or "") if r else "")' 2>/dev/null || echo "")"
        if [[ -z "$qid" || "$qid" == "None" ]]; then
          echo "  [FAIL] customers '${nm}' has no qbo_customer_id — never synced to QBO" >&2
          BID_REPLAY_CUST_FAILS=$((BID_REPLAY_CUST_FAILS + 1))
        else
          local qj match
          qj="$(_bid_qbo_get "$slug" "customer" "$qid" 2>/dev/null || echo '{}')"
          match="$(printf '%s' "$qj" | python3 -c '
import json,sys,re
nm=sys.argv[1]
try: d=json.load(sys.stdin)
except Exception: print("ERR"); sys.exit(0)
doc=d.get("result",d)
if isinstance(doc,dict) and "Customer" in doc: doc=doc["Customer"]
disp=(doc.get("DisplayName") or "").lower()
print("OK" if re.search(nm, disp) else "BAD")' "$nm" 2>/dev/null || echo ERR)"
          if [[ "$match" == "OK" ]]; then
            echo "  [PASS] customers '${nm}' QBO customer $qid DisplayName matches" >&2
          else
            echo "  [FAIL] customers '${nm}' QBO customer $qid DisplayName mismatch (got status $match)" >&2
            BID_REPLAY_CUST_FAILS=$((BID_REPLAY_CUST_FAILS + 1))
          fi
        fi
      fi
    fi
  done <<< "$spec"
  return 0
}

# Post-run assertions: verify up to QBO. Returns 0 = PASS, 1 = FAIL.
_bid_replay_assert() {
  local slug="$1" fixture="$2" start_ts="$3"
  local exp_count exp_total name_match
  exp_count="$(_bid_fixture_get "$fixture" "expect.bid_line_count")"
  exp_total="$(_bid_fixture_get "$fixture" "expect.bid_total")"
  name_match="$(_bid_fixture_get "$fixture" "expect.customer_name_match")"
  [[ -z "$name_match" ]] && name_match="validat.?on jones"
  # Finding 1 / G7: guard name_match against SQL injection / quote-break.
  # Allow: a-z 0-9 a single literal space . ? * + ^ $ ( ) | -
  # (valid POSIX regex metacharacters for a lowercase name pattern).
  # Reject everything else — quotes, semicolons, backslashes, newlines, CR, FF, VT.
  # Using a single literal space (not [:space:]) deliberately excludes control chars.
  # Pattern stored in a variable to avoid bash ERE inline-bracket quoting pitfalls.
  local _name_match_pat='^[a-z0-9.?*+^$()| -]+$'
  [[ "$name_match" =~ $_name_match_pat ]] || {
    echo "bid-replay: customer_name_match has illegal chars: '$name_match'" >&2
    return 1
  }

  # Detect multi-doc mode early so legacy single-estimate block can be skipped.
  local has_multi_docs
  has_multi_docs="$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
exp=d.get("expect",{})
print("1" if ("expected_estimates" in exp or "expected_invoices" in exp or "service_desk" in exp or "customers" in exp) else "0")
' "$fixture" 2>/dev/null || echo "0")"

  # A5b: does the fixture declare a single-estimate expectation? (bid_total or an
  # explicit customer_name_match). If NOT — and not multi-doc — skip the legacy
  # single-estimate assertions so estimate-less fixtures (expect:{}, e.g. the
  # hung-turn watchdog UAT) don't false-fail.
  local declares_estimate
  declares_estimate="$(python3 -c '
import json,sys
exp=json.load(open(sys.argv[1])).get("expect",{})
print("1" if ("bid_total" in exp or "customer_name_match" in exp) else "0")
' "$fixture" 2>/dev/null || echo "0")"

  local fails=0
  # Fold in any per-turn bot-text assertion failures recorded during the send
  # phase (BID_REPLAY_TEXT_FAILS, set by _bid_assert_turn_text). A text-assertion
  # failure must fail the gate exactly like a doc-total failure.
  if [[ "${BID_REPLAY_TEXT_FAILS:-0}" =~ ^[0-9]+$ ]] && [[ "${BID_REPLAY_TEXT_FAILS:-0}" != "0" ]]; then
    echo "  [INFO] ${BID_REPLAY_TEXT_FAILS} per-turn bot-text assertion(s) failed during the conversation (counted in RESULT)" >&2
    fails=$((fails + BID_REPLAY_TEXT_FAILS))
  fi
  # Safe defaults for cleanup stash (populated by legacy block when active).
  local est_summary='{"count":0,"rows":[]}'
  local qbo_id="" doc_number=""
  echo "" >&2
  echo "=== ASSERTIONS (verify up to QBO) — start_ts=$start_ts ===" >&2

  # 1. Bid draft persisted with all N lines (the line set that fails to persist
  #    pre-fix). lines is jsonb holding a double-encoded JSON string -> extract
  #    the inner text with #>>'{}' then cast to jsonb for array length.
  local draft_json draft_count
  if [[ -z "$exp_count" ]]; then
    echo "  [SKIP] bid_drafts check — expect.bid_line_count not set in fixture" >&2
  else
    draft_json="$(_bid_sql "$slug" "
      SELECT id, status,
             jsonb_array_length((lines #>> '{}')::jsonb) AS line_count
      FROM bid_drafts
      WHERE created_at > '${start_ts}'
      ORDER BY created_at DESC
      LIMIT 1")"
    draft_count="$(printf '%s' "$draft_json" | python3 -c '
import json,sys
d=json.load(sys.stdin); r=d.get("result",d) if isinstance(d,dict) else d
print(r[0].get("line_count","") if r else "NONE")' 2>/dev/null || echo "ERR")"
    if [[ "$draft_count" == "$exp_count" ]]; then
      echo "  [PASS] bid_drafts persisted $draft_count lines (expected $exp_count)" >&2
    else
      echo "  [FAIL] bid_drafts line count = '$draft_count' (expected $exp_count) — the dead-end (no/short persisted draft)" >&2
      fails=$((fails + 1))
    fi
  fi

  # 2. Single-estimate assertions: skipped when expected_estimates/expected_invoices
  #    are present (multi-doc mode handles those via _bid_assert_multi_docs below).
  if [[ "$has_multi_docs" != "1" && "$declares_estimate" == "1" ]]; then
  # Exactly ONE estimate for Validation Jones since start; capture its qbo id + total.
  local est_json
  est_json="$(_bid_sql "$slug" "
    SELECT e.id, e.doc_number, e.status, e.grand_total, e.qbo_estimate_id
    FROM estimates e
    JOIN clients c ON c.id = e.client_id
    WHERE e.created_at > '${start_ts}'
      AND lower(c.name) ~ '${name_match}'
    ORDER BY e.created_at DESC")"
  est_summary="$(printf '%s' "$est_json" | python3 -c '
import json,sys
d=json.load(sys.stdin); r=d.get("result",d) if isinstance(d,dict) else d
print(json.dumps({"count":len(r),"rows":r}))' 2>/dev/null || echo '{"count":-1,"rows":[]}')"
  local est_count
  est_count="$(printf '%s' "$est_summary" | python3 -c 'import json,sys;print(json.load(sys.stdin)["count"])')"
  if [[ "$est_count" == "1" ]]; then
    echo "  [PASS] exactly one Validation-Jones estimate created" >&2
  else
    echo "  [FAIL] Validation-Jones estimates created = $est_count (expected 1)" >&2
    fails=$((fails + 1))
  fi

  # local total + not-sent + qbo id, from the newest estimate row.
  local loc_total est_status
  loc_total="$(printf '%s' "$est_summary" | python3 -c 'import json,sys
r=json.load(sys.stdin)["rows"];print(r[0].get("grand_total","") if r else "")')"
  est_status="$(printf '%s' "$est_summary" | python3 -c 'import json,sys
r=json.load(sys.stdin)["rows"];print(r[0].get("status","") if r else "")')"
  qbo_id="$(printf '%s' "$est_summary" | python3 -c 'import json,sys
r=json.load(sys.stdin)["rows"];print(r[0].get("qbo_estimate_id","") if r else "")')"
  doc_number="$(printf '%s' "$est_summary" | python3 -c 'import json,sys
r=json.load(sys.stdin)["rows"];print(r[0].get("doc_number","") if r else "")')"

  # local grand_total matches the quote
  # Finding 2: pass DB + fixture values as argv rather than interpolating into
  # the Python source string to prevent code injection via crafted values.
  if python3 -c 'import sys; sys.exit(0 if abs(float(sys.argv[1] or 0) - float(sys.argv[2])) < 0.005 else 1)' "${loc_total:-0}" "$exp_total" 2>/dev/null; then
    echo "  [PASS] local grand_total = $loc_total (expected $exp_total)" >&2
  else
    echo "  [FAIL] local grand_total = '$loc_total' (expected $exp_total)" >&2
    fails=$((fails + 1))
  fi

  # not sent
  if [[ "$est_status" == "sent" ]]; then
    echo "  [FAIL] estimate status='sent' — fixture says DO NOT SEND" >&2
    fails=$((fails + 1))
  else
    echo "  [PASS] estimate not sent (status='${est_status:-?}')" >&2
  fi

  # 3. QBO read-back: TotalAmt == quote, every line TaxCodeRef = NON,
  #    per-line Amount/Qty/UnitPrice/ItemRef correct (spec §C4 L80).
  # Finding 4b: guard qbo_id — must be numeric before calling qbo-get.
  if [[ -n "$qbo_id" && "$qbo_id" != "None" ]]; then
    if ! [[ "$qbo_id" =~ ^[0-9]+$ ]]; then
      echo "  [FAIL] qbo_estimate_id is not numeric ('$qbo_id') — cannot read back from QBO" >&2
      fails=$((fails + 1))
    else
      local qbo_json
      qbo_json="$(_bid_qbo_get "$slug" estimate "$qbo_id" 2>/dev/null || echo '{}')"
      # Load expected per-line data from fixture (may be empty array → skip per-line check).
      local exp_lines_json
      exp_lines_json="$(_bid_fixture_get "$fixture" "expect.qbo_lines")"
      [[ -z "$exp_lines_json" ]] && exp_lines_json="[]"
      local qbo_check
      qbo_check="$(printf '%s' "$qbo_json" | python3 -c '
import json, sys
exp_total = float(sys.argv[1])
exp_lines = json.loads(sys.argv[2])   # list of {description,qty,unit_price,amount,item_ref}
# G2 fix: parse errors must NOT emit a silent-PASS sentinel.  Emit a sentinel
# that forces BOTH the NON-taxable check (qnonnon != "0") and the per-line
# check (qline_fails non-empty) to FAIL so no assertion silently passes on a
# malformed QBO response.
try:
    d = json.load(sys.stdin)
except Exception:
    print("PARSE_ERR|NONE|PARSE_ERR|qbo response did not parse as JSON"); sys.exit(0)
est = d.get("result", d)
if isinstance(est, dict) and "Estimate" in est:
    est = est["Estimate"]
total = est.get("TotalAmt")
lines = [l for l in est.get("Line", []) if l.get("DetailType") == "SalesItemLineDetail"]
nonnon = [l for l in lines
          if (l.get("SalesItemLineDetail", {}).get("TaxCodeRef", {}) or {}).get("value") != "NON"]
ok_total = total is not None and abs(float(total) - exp_total) < 0.005

# Per-line assertions (spec §C4 L80): compare each expected entry against the
# matching QBO line by position (QBO returns lines in order).
#
# ItemRef assertion mode (G1 fix):
#   - If "item_ref" is the sentinel "*present*" (or "item_ref_present": true),
#     the assertion checks that ItemRef.value is a non-empty string — the line
#     is catalog-linked, not orphaned — without pinning to a tenant-specific UUID.
#   - If "item_ref" is a concrete string (e.g. "abc-123"), exact match is used.
#   - If "item_ref" is absent, the ItemRef dimension is skipped for that line.
#
# Failure messages use \x01 as the delimiter (G3 fix) so a literal ~ or any
# other punctuation in a description cannot produce phantom FAIL entries.
ITEM_REF_PRESENT_SENTINEL = "*present*"
line_fails = []
if exp_lines:
    if len(lines) != len(exp_lines):
        line_fails.append("line count: got %d expected %d" % (len(lines), len(exp_lines)))
    for i, (ql, el) in enumerate(zip(lines, exp_lines)):
        det = ql.get("SalesItemLineDetail", {})
        desc = el.get("description", el.get("product", "line %d" % i))
        got_qty        = det.get("Qty")
        got_unit_price = det.get("UnitPrice")
        got_item_ref   = (det.get("ItemRef") or {}).get("value")
        got_amount     = ql.get("Amount")
        # Scalar numeric/string fields
        for field, got, want in [
            ("Qty",       got_qty,        el.get("qty")),
            ("UnitPrice", got_unit_price, el.get("unit_price")),
            ("Amount",    got_amount,     el.get("amount")),
        ]:
            if want is None:
                continue   # fixture did not specify this field — skip
            try:
                ok = abs(float(got) - float(want)) < 0.005
            except (TypeError, ValueError):
                ok = str(got) == str(want)
            if not ok:
                line_fails.append("%s %s: got %s expected %s" % (desc, field, got, want))
        # ItemRef: presence check or exact match (G1)
        item_ref_want = el.get("item_ref")
        item_ref_present = el.get("item_ref_present", False)
        if item_ref_want == ITEM_REF_PRESENT_SENTINEL or item_ref_present:
            if not (isinstance(got_item_ref, str) and got_item_ref.strip()):
                line_fails.append("%s ItemRef: expected present+non-empty, got %r" % (desc, got_item_ref))
        elif item_ref_want is not None:
            if str(got_item_ref) != str(item_ref_want):
                line_fails.append("%s ItemRef: got %s expected %s" % (desc, got_item_ref, item_ref_want))

print(("OK" if ok_total else "BAD") + "|" + str(total) + "|" + str(len(nonnon)) + "|" + "\x01".join(line_fails))
' "$exp_total" "$exp_lines_json" 2>/dev/null || echo "ERR|?|?|")"
      local qstat qtotal qnonnon qline_fails
      IFS='|' read -r qstat qtotal qnonnon qline_fails <<< "$qbo_check"
      if [[ "$qstat" == "OK" ]]; then
        echo "  [PASS] QBO estimate $qbo_id TotalAmt=$qtotal == quote" >&2
      else
        echo "  [FAIL] QBO estimate $qbo_id TotalAmt=$qtotal (expected $exp_total)" >&2
        fails=$((fails + 1))
      fi
      if [[ "$qnonnon" == "0" ]]; then
        echo "  [PASS] every QBO line TaxCodeRef=NON (bid = no tax)" >&2
      else
        echo "  [FAIL] $qnonnon QBO line(s) NOT marked NON — bid lines must be non-taxable" >&2
        fails=$((fails + 1))
      fi
      # Per-line assertion results (G3 fix: split on \x01, not ~, to avoid
      # phantom FAIL lines when a description contains a literal tilde).
      if [[ -n "$qline_fails" ]]; then
        local lf
        while IFS= read -r lf; do
          [[ -n "$lf" ]] && { echo "  [FAIL] per-line mismatch: $lf" >&2; fails=$((fails + 1)); }
        done <<< "$(printf '%s' "$qline_fails" | tr '\001' '\n')"
      else
        # Only print the per-line PASS when the fixture actually supplied qbo_lines
        if [[ "$exp_lines_json" != "[]" ]]; then
          echo "  [PASS] all QBO per-line Amount/Qty/UnitPrice/ItemRef match fixture" >&2
        fi
      fi
    fi
  else
    echo "  [FAIL] estimate has no qbo_estimate_id — never synced to QBO" >&2
    fails=$((fails + 1))
  fi
  else
    echo "  [SKIP] estimate assertions — fixture declares no expected estimate (expect.bid_total / customer_name_match absent)" >&2
  fi  # end has_multi_docs != "1" / declares_estimate guard

  # 4. Exactly ONE Validation-Jones customer TOTAL (the respelling must not dup).
  #    Deliberately NOT scoped to created_at > start_ts: the placeholder customer
  #    usually pre-exists (created by an earlier UAT), so a correct run creates
  #    ZERO new rows and a start_ts-scoped count false-FAILs (observed on the
  #    2026-06-10 messy-fixture pre-fix run: the bot correctly reused the
  #    existing customer and count-since-start was 0). Counting ALL matching
  #    rows with expected==1 asserts the dedup invariant in both scenarios AND
  #    stays re-runnable — a dup row persists and keeps failing until cleaned up.
  local cust_json cust_count
  cust_json="$(_bid_sql "$slug" "
    SELECT count(*) AS n FROM clients
    WHERE lower(name) ~ '${name_match}'")"
  cust_count="$(printf '%s' "$cust_json" | python3 -c '
import json,sys
d=json.load(sys.stdin); r=d.get("result",d) if isinstance(d,dict) else d
print(r[0].get("n","") if r else "ERR")' 2>/dev/null || echo "ERR")"
  if [[ "$cust_count" == "1" ]]; then
    echo "  [PASS] exactly one Validation-Jones customer (C3 dedup held)" >&2
  else
    echo "  [FAIL] Validation-Jones customers = $cust_count (expected 1) — respelling duped" >&2
    fails=$((fails + 1))
  fi

  # G5: assert doc_number starts with estimate_doc_prefix when set in fixture.
  # Use a literal substring comparison rather than a glob to prevent a fixture
  # value like "*" from matching anything (false PASS).
  local doc_prefix
  doc_prefix="$(_bid_fixture_get "$fixture" "expect.estimate_doc_prefix")"
  if [[ -n "$doc_prefix" ]]; then
    if [[ "${doc_number:0:${#doc_prefix}}" == "$doc_prefix" ]]; then
      echo "  [PASS] estimate doc_number '$doc_number' starts with prefix '$doc_prefix'" >&2
    else
      echo "  [FAIL] estimate doc_number '$doc_number' does not start with expected prefix '$doc_prefix'" >&2
      fails=$((fails + 1))
    fi
  fi

  # 5. Multi-doc assertions (expected_estimates / expected_invoices).
  # When these keys are present in the fixture, assert each doc by LIKE pattern.
  # Absent keys → this block is a no-op (legacy behavior unchanged).
  # has_multi_docs was computed at the top of this function.
  # DIRECT call (no $(...)): results return via the BID_REPLAY_MULTI_FAILS /
  # BID_REPLAY_MULTI_CLEANUP globals — a command-substitution subshell would
  # silently drop the cleanup stash that --cleanup depends on.
  BID_REPLAY_MULTI_CLEANUP=""
  BID_REPLAY_MULTI_FAILS=0
  if [[ "$has_multi_docs" == "1" ]]; then
    _bid_assert_multi_docs "$slug" "$fixture" "$start_ts"
    fails=$((fails + BID_REPLAY_MULTI_FAILS))
  fi

  # 6. Service-desk assertions (expect.service_desk). Direct call — results
  # via BID_REPLAY_SD_FAILS / BID_REPLAY_SD_CLEANUP globals.
  BID_REPLAY_SD_FAILS=0
  BID_REPLAY_SD_CLEANUP=""
  _bid_assert_service_desk "$slug" "$fixture" "$start_ts"
  fails=$((fails + BID_REPLAY_SD_FAILS))

  # 7. Customer assertions (expect.customers). Direct call — result via
  # BID_REPLAY_CUST_FAILS global.
  BID_REPLAY_CUST_FAILS=0
  _bid_assert_customers "$slug" "$fixture"
  fails=$((fails + BID_REPLAY_CUST_FAILS))

  echo "" >&2
  # Stash identifiers for optional cleanup.
  BID_REPLAY_LOCAL_EST_ID="$(printf '%s' "$est_summary" | python3 -c 'import json,sys
r=json.load(sys.stdin)["rows"];print(r[0].get("id","") if r else "")')"
  BID_REPLAY_QBO_EST_ID="$qbo_id"
  BID_REPLAY_DOC_NUMBER="$doc_number"

  if [[ "$fails" == "0" ]]; then
    echo "=== RESULT: PASS — bid persisted, one correct non-taxable estimate, not sent, no dup customer ===" >&2
    return 0
  fi
  echo "=== RESULT: FAIL ($fails assertion(s)) ===" >&2
  return 1
}

tenant_bid_replay_main() {
  local slug="" fixture="" dry=0 cleanup=0 assert=1
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run)   dry=1; shift ;;
      --cleanup)   cleanup=1; shift ;;
      --no-assert) assert=0; shift ;;
      --help|-h|help) tenant_bid_replay_usage; return 0 ;;
      -*)          echo "bid-replay: unknown flag: $1" >&2; return 2 ;;
      *)
        if [[ -z "$slug" ]]; then slug="$1"
        elif [[ -z "$fixture" ]]; then fixture="$1"
        else echo "bid-replay: unexpected arg: $1" >&2; return 2
        fi
        shift ;;
    esac
  done

  if [[ -z "$slug" || -z "$fixture" ]]; then
    tenant_bid_replay_usage
    return 2
  fi
  if [[ ! -f "$fixture" ]]; then
    echo "bid-replay: fixture not found: $fixture" >&2
    return 1
  fi
  if ! python3 -c 'import json,sys;json.load(open(sys.argv[1]))' "$fixture" 2>/dev/null; then
    echo "bid-replay: fixture is not valid JSON: $fixture" >&2
    return 1
  fi

  local wait_s quiet_s
  wait_s="$(_bid_fixture_get "$fixture" "read.wait")";   [[ -z "$wait_s"  ]] && wait_s=240
  quiet_s="$(_bid_fixture_get "$fixture" "read.quiet")"; [[ -z "$quiet_s" ]] && quiet_s=6
  # G6: fail fast on non-integer values so the harness doesn't silently pass
  # "abc" to sleep or tg-read --wait (which would produce confusing errors).
  [[ "$wait_s"  =~ ^[0-9]+$ ]] || { echo "bid-replay: read.wait must be a non-negative integer (got '$wait_s')"   >&2; return 1; }
  [[ "$quiet_s" =~ ^[0-9]+$ ]] || { echo "bid-replay: read.quiet must be a non-negative integer (got '$quiet_s')" >&2; return 1; }

  if [[ "$dry" == "1" ]]; then
    echo "=== DRY RUN — $fixture (wait=${wait_s}s quiet=${quiet_s}s) ===" >&2
    python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
print("slug:", d.get("slug"))
for t in d["turns"]:
    print("  TURN %2d [%s]: %s" % (t["n"], t.get("label",""), t["text"][:120].replace(chr(10)," ")))
print("expect:", json.dumps(d.get("expect",{})))
' "$fixture" >&2
    return 0
  fi

  require_operator_secret TELEGRAM_SESSION_STRING "run: harness tenant tg-login"

  # Freeze the window the assertions look at BEFORE the first send, so only this
  # run's draft/estimate/customer rows are considered.
  local start_ts
  start_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "=== bid-replay: $slug — $(basename "$fixture") — start_ts=$start_ts ===" >&2

  # ── Send-safety preflight ───────────────────────────────────────────────
  # A2: fixtures that trigger a real QBO email (send_estimate) carry an
  # expect.send_safety block naming the ONLY recipient we will email. Verify
  # the live clients.email matches BEFORE sending any turn — a real email must
  # never reach a non-operator address. Abort the whole run on mismatch.
  local ss_json ss_name ss_email ss_rc=0
  ss_json="$(python3 -c '
import json,sys
d=json.load(open(sys.argv[1])); ss=d.get("expect",{}).get("send_safety")
if isinstance(ss,dict):
    print((ss.get("recipient_name_match","") or "") + "\x1f" + (ss.get("recipient_email","") or ""))
elif ss is not None:
    # send_safety present but NOT a JSON object → fail closed, never silently skip the gate
    print("__SS_INVALID__")
' "$fixture" 2>/dev/null)" || ss_rc=$?
  # Fail closed: a python3 failure here must abort, not silently skip the safety gate.
  if [[ "$ss_rc" -ne 0 ]]; then
    echo "  [ABORT] send_safety: could not read send_safety block from fixture (python3 exit $ss_rc) — refusing to send" >&2
    return 1
  fi
  if [[ "$ss_json" == "__SS_INVALID__" ]]; then
    echo "  [ABORT] send_safety: present but not a JSON object — refusing to send" >&2
    return 1
  fi
  if [[ -n "$ss_json" ]]; then
    local _us=$'\x1f'
    ss_name="$(printf '%s' "$ss_json" | cut -d"$_us" -f1)"
    ss_email="$(printf '%s' "$ss_json" | cut -d"$_us" -f2)"
    local _nm_pat='^[a-z0-9 .?*+^$()|-]+$' _em_pat='^[a-z0-9@+._ -]+$'
    if [[ -z "$ss_name" || -z "$ss_email" ]]; then
      echo "  [ABORT] send_safety: present but missing recipient_name_match/recipient_email" >&2
      return 1
    fi
    if ! [[ "$ss_name" =~ $_nm_pat ]] || ! [[ "$ss_email" =~ $_em_pat ]]; then
      echo "  [ABORT] send_safety: recipient_name_match/recipient_email has illegal chars" >&2
      return 1
    fi
    local got_email_json got_email
    got_email_json="$(_bid_sql "$slug" "SELECT email FROM clients WHERE lower(name) ~ '${ss_name}' ORDER BY created_at DESC LIMIT 1")" \
      || { echo "  [ABORT] send_safety: DB query failed for recipient lookup — refusing to send" >&2; return 1; }
    got_email="$(printf '%s' "$got_email_json" | python3 -c '
import json,sys
d=json.load(sys.stdin); r=d.get("result",d) if isinstance(d,dict) else d
print((r[0].get("email") or "") if r else "")' 2>/dev/null || echo "")"
    if [[ "$got_email" == "$ss_email" ]]; then
      echo "  [PASS] send_safety: recipient '${ss_name}' email = ${got_email}" >&2
    else
      echo "  [ABORT] send_safety: recipient '${ss_name}' email is '${got_email}', expected '${ss_email}' — refusing to send" >&2
      return 1
    fi
  fi

  # Per-turn bot-text assertion failures accumulate in BID_REPLAY_TEXT_FAILS
  # (a global, since send-turns runs under $(...) to capture the cursor on
  # stdout — a `return` count would be lost). We call it WITHOUT command
  # substitution here (stdout → /dev/null) so the global survives into this
  # scope, then fold it into the run result below.
  BID_REPLAY_TEXT_FAILS=0
  _bid_replay_send_turns "$slug" "$fixture" "$wait_s" "$quiet_s" >/dev/null || return 1

  if [[ "$assert" == "0" ]]; then
    if [[ "${BID_REPLAY_TEXT_FAILS:-0}" != "0" ]]; then
      echo "bid-replay: turns sent; --no-assert set, but ${BID_REPLAY_TEXT_FAILS} per-turn bot-text assertion(s) FAILED." >&2
      return 1
    fi
    echo "bid-replay: turns sent; --no-assert set, skipping verification." >&2
    return 0
  fi

  # Give the final create + QBO outbox drain time to settle before reading back.
  local settle
  settle="$(_bid_fixture_get "$fixture" "final_settle_seconds")"; [[ -z "$settle" ]] && settle=120
  # Finding 4a: guard against non-integer settle value (e.g. crafted fixture or empty)
  [[ "$settle" =~ ^[0-9]+$ ]] || {
    echo "bid-replay: final_settle_seconds must be a non-negative integer (got '$settle')" >&2
    return 1
  }
  echo "bid-replay: settling ${settle}s for create + QBO sync ..." >&2
  sleep "$settle"

  local rc=0
  BID_REPLAY_LOCAL_EST_ID=""; BID_REPLAY_QBO_EST_ID=""; BID_REPLAY_DOC_NUMBER=""
  BID_REPLAY_MULTI_CLEANUP=""
  BID_REPLAY_SD_CLEANUP=""
  _bid_replay_assert "$slug" "$fixture" "$start_ts" || rc=1

  if [[ "$cleanup" == "1" ]]; then
    local uuid_re='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'

    # Multi-doc cleanup: iterate the tab-separated stash built by _bid_assert_multi_docs.
    # Each entry is "local_id<US>entity<US>doc_number<US>qbo_id" where US is the
    # unit separator $'\x1f' — it cannot appear in a UUID/entity/doc value, so a
    # literal ':' inside doc_number cannot corrupt the parse. bash-3.2 safe loop.
    if [[ -n "${BID_REPLAY_MULTI_CLEANUP:-}" ]]; then
      echo "=== CLEANUP — multi-doc local rows ===" >&2
      local _us=$'\x1f'
      # Replace tabs with newlines so we can iterate one entry per line.
      local cleanup_entry
      while IFS= read -r cleanup_entry; do
        [[ -z "$cleanup_entry" ]] && continue
        local cid ctype cdoc cqbo
        cid="$(   printf '%s' "$cleanup_entry" | cut -d"$_us" -f1)"
        ctype="$( printf '%s' "$cleanup_entry" | cut -d"$_us" -f2)"
        cdoc="$(  printf '%s' "$cleanup_entry" | cut -d"$_us" -f3)"
        cqbo="$(  printf '%s' "$cleanup_entry" | cut -d"$_us" -f4)"
        if ! [[ "$cid" =~ $uuid_re ]]; then
          echo "  cleanup skipped — '${cid}' is not a valid UUID (${ctype} ${cdoc})" >&2
          continue
        fi
        # Item 5: capture each DELETE's exit — only claim success when both
        # statements succeeded; otherwise flag for manual cleanup.
        local del_ok=1
        if [[ "$ctype" == "estimate" ]]; then
          _bid_sql "$slug" "DELETE FROM estimate_line_items WHERE estimate_id = '${cid}'" >/dev/null 2>&1 || del_ok=0
          _bid_sql "$slug" "DELETE FROM estimates WHERE id = '${cid}'" >/dev/null 2>&1 || del_ok=0
        else
          _bid_sql "$slug" "DELETE FROM invoice_line_items WHERE invoice_id = '${cid}'" >/dev/null 2>&1 || del_ok=0
          _bid_sql "$slug" "DELETE FROM invoices WHERE id = '${cid}'" >/dev/null 2>&1 || del_ok=0
        fi
        if [[ "$del_ok" == "1" ]]; then
          echo "  ${ctype} ${cdoc} (${cid}) deleted." >&2
        else
          echo "  [WARN] delete failed for ${cid} (${ctype} ${cdoc}) — manual cleanup" >&2
        fi
        if [[ -n "$cqbo" && "$cqbo" != "None" ]]; then
          echo "  MANUAL: delete QBO ${ctype} ${cqbo} (${cdoc}) in QuickBooks — the bot has no delete tool." >&2
        fi
      done <<< "$(printf '%s' "${BID_REPLAY_MULTI_CLEANUP}" | tr '\t' '\n')"
      _bid_sql "$slug" "UPDATE bid_drafts SET status='abandoned' WHERE status IN ('collecting','ready','promoted') AND created_at > '${start_ts}'" >/dev/null 2>&1 || true
      echo "  run drafts abandoned." >&2
    fi

    # Legacy single-estimate cleanup (when multi-doc stash is empty but a single est id exists).
    if [[ -z "${BID_REPLAY_MULTI_CLEANUP:-}" && -n "${BID_REPLAY_LOCAL_EST_ID:-}" ]]; then
      # Finding 3: validate the DB-derived id is a UUID before interpolating into DELETE.
      # Skip cleanup (with a warning) rather than running a malformed query.
      if ! [[ "${BID_REPLAY_LOCAL_EST_ID}" =~ $uuid_re ]]; then
        echo "  bid-replay: cleanup skipped — BID_REPLAY_LOCAL_EST_ID '${BID_REPLAY_LOCAL_EST_ID}' is not a valid UUID" >&2
      else
        echo "=== CLEANUP — local rows for estimate ${BID_REPLAY_LOCAL_EST_ID} (${BID_REPLAY_DOC_NUMBER}) ===" >&2
        _bid_sql "$slug" "DELETE FROM estimate_line_items WHERE estimate_id = '${BID_REPLAY_LOCAL_EST_ID}'" >/dev/null 2>&1 || true
        _bid_sql "$slug" "DELETE FROM estimates WHERE id = '${BID_REPLAY_LOCAL_EST_ID}'" >/dev/null 2>&1 || true
        _bid_sql "$slug" "UPDATE bid_drafts SET status='abandoned' WHERE status IN ('collecting','ready','promoted') AND created_at > '${start_ts}'" >/dev/null 2>&1 || true
        echo "  local estimate + line items deleted; run drafts abandoned." >&2
        if [[ -n "${BID_REPLAY_QBO_EST_ID:-}" && "${BID_REPLAY_QBO_EST_ID}" != "None" ]]; then
          echo "  MANUAL: delete QBO estimate ${BID_REPLAY_QBO_EST_ID} (${BID_REPLAY_DOC_NUMBER}) in QuickBooks — the bot has no delete tool." >&2
        fi
      fi  # end UUID guard
    fi  # end legacy single-estimate cleanup

    # Service-desk run-window cleanup (uuid-guarded, bash-3.2-safe loop).
    if [[ -n "${BID_REPLAY_SD_CLEANUP:-}" ]]; then
      echo "=== CLEANUP — service_desk run-window tickets ===" >&2
      local sd_id
      while IFS= read -r sd_id; do
        [[ -z "$sd_id" ]] && continue
        if ! [[ "$sd_id" =~ $uuid_re ]]; then
          echo "  cleanup skipped — '${sd_id}' is not a valid UUID" >&2
          continue
        fi
        if _bid_sql "$slug" "DELETE FROM service_desk WHERE id = '${sd_id}'" >/dev/null 2>&1; then
          echo "  service_desk ${sd_id} deleted." >&2
        else
          echo "  [WARN] delete failed for service_desk ${sd_id} — manual cleanup" >&2
        fi
      done <<< "${BID_REPLAY_SD_CLEANUP}"
    fi
  fi  # end cleanup block

  return "$rc"
}

if [[ "${BASH_SOURCE[0]}" == "${0:-}" ]]; then
  echo "tenant_bid_replay.sh: source + call tenant_bid_replay_main" >&2
  exit 1
fi
