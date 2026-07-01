#!/usr/bin/env bash
# tenant_permit_replay.sh — Validation-Jones permit-fill replay UAT driver.
#
#   harness permit-uat run <fixture.json> [--dry-run] [--no-assert] [--pr N]
#   harness permit-uat help
#
# Replays a captured permit-fill conversation (fixture = an ordered list of
# transcribed user turns) against the DEPLOYED tenant agent path using the
# real-Telegram UAT harness, then asserts UP TO the system of record
# (browser_sessions) that the fill subsystem:
#   - filled the resolvable fields from the right SOURCE (profile / app_data),
#   - left genuinely-unknown fields BLANK (never hard-failed the cascade),
#   - parked the outstanding DOCUMENTS in missing_inputs (values=ask, docs=park),
#   - and NEVER auto-submitted (status NEVER reaches 'submitted_by_human').
#
# This is the Phase-H deliverable of
# docs/superpowers/plans/2026-06-23-permit-fill-everything.md and mirrors
# lib/tenant_bid_replay.sh (fixture-load python helper, tg-send/tg-read usage,
# tenant SQL, harness uat record). Never targets a real customer — the fixture
# uses the placeholder "Validation Jones".
#
# IMPORTANT (target_override): start_permits_for_job only AUTO-STARTS a session
# when handed a target_override that is a known portal key (redondo-iworq /
# sce-powerclerk) OR when only ONE portal is configured. TARGET_CONFIG currently
# has TWO portals, so WITHOUT an override the tool just builds the plan and asks
# which portal. The fixture therefore declares "target_override":"redondo-iworq"
# and the start turn names the portal key explicitly so the kickoff auto-starts.
# The SQL seed + the browser_sessions assertions below are what actually gate
# the UAT.
#
# Tests inject HARNESS_PERMIT_REPLAY_SQL to stub the network/DB layer; the live
# Telegram path is only exercised post-deploy by a human-in-the-loop.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=./operator-env.sh
source "$SCRIPT_DIR/operator-env.sh"
# shellcheck source=./tenant_common.sh
source "$SCRIPT_DIR/tenant_common.sh"
# shellcheck source=./tenant_telegram.sh
source "$SCRIPT_DIR/tenant_telegram.sh"
# shellcheck source=./tenant_sql.sh
source "$SCRIPT_DIR/tenant_sql.sh"
# shellcheck source=./uat.sh
source "$SCRIPT_DIR/uat.sh"

tenant_permit_replay_usage() {
  cat >&2 <<USAGE
Usage:
  harness permit-uat run <fixture.json> [--dry-run] [--no-assert] [--pr N]
  harness permit-uat help

  Replays a captured permit-fill conversation against the DEPLOYED tenant bot
  and asserts the fill persisted to browser_sessions with the right field
  sources, the outstanding documents parked in missing_inputs, and that the
  form was NEVER auto-submitted (values=ask, documents=park, never-submit).

  --dry-run     print the fixture turns + expectations without sending anything
  --no-assert   send the turns but skip the post-run browser_sessions assertions
  --pr N        after the run, record the outcome via 'harness uat record'
                (category=permit-fill, driver=agent)

Examples:
  harness permit-uat run client-app/tests/uat/permit-fill-validation-jones.json
  harness permit-uat run <fixture> --dry-run
  harness permit-uat run <fixture> --pr 217

Reads:
  - TELEGRAM_SESSION_STRING, SUPABASE_MANAGEMENT_PAT (from ~/.harness/operator.env)
USAGE
}

# Extract a top-level scalar / nested value from the fixture via python3.
# Mirrors _bid_fixture_get: lists/dicts emitted as JSON; scalars as-is.
_permit_fixture_get() {
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
    print(json.dumps(v))
else:
    print(v)
' "$fixture" "$expr"
}

# SQL shim shared by seed + every assertion. `harness tenant sql --json` returns
# either {"result":[...]} or a bare [...]; the python snippets normalize. The
# whole layer is stubbable via HARNESS_PERMIT_REPLAY_SQL for tests.
_permit_sql() {
  local slug="$1" query="$2"
  if [[ -n "${HARNESS_PERMIT_REPLAY_SQL:-}" ]]; then
    "$HARNESS_PERMIT_REPLAY_SQL" "$slug" "$query"
    return $?
  fi
  tenant_sql_main "$slug" "$query" --json
}

# Seed/reset the Jones job + a known permit_profile so the resolver cascade has
# deterministic inputs (state_license from profile, owner name + valuation from
# app_data). Idempotent: upserts the client/profile, and recreates the job so a
# re-run starts from a clean slate. Returns the seeded job_id on stdout.
_permit_seed() {
  local slug="$1" fixture="$2"
  local category address valuation
  category="$(_permit_fixture_get "$fixture" "job.category")"
  address="$(_permit_fixture_get "$fixture" "job.address")"
  valuation="$(_permit_fixture_get "$fixture" "job.valuation")"
  [[ -z "$category" ]] && category="electrical-service-upgrade"
  [[ -z "$valuation" ]] && valuation="0"
  # SECURITY: slug/category/address/valuation are UNTRUSTED fixture-derived strings
  # interpolated into live-prod Supabase SQL. Validate every one against a strict
  # charset before it reaches a SQL string (mirrors the valuation/address guards
  # that were already present) — never trust a fixture value verbatim.
  # Numeric guard on valuation before it reaches SQL.
  [[ "$valuation" =~ ^[0-9]+$ ]] || { echo "permit-uat: job.valuation must be an integer (got '$valuation')" >&2; return 1; }
  # Conservative address guard (interpolated into a JSON string literal in SQL).
  local _addr_pat='^[A-Za-z0-9 ,./#-]+$'
  [[ "$address" =~ $_addr_pat ]] || { echo "permit-uat: job.address has illegal chars" >&2; return 1; }
  # slug feeds permit_profile rows + clients/jobs lookups — restrict to the tenant
  # slug charset (lowercase alphanumerics + hyphen).
  [[ "$slug" =~ ^[a-z0-9-]+$ ]] || { echo "permit-uat: slug must match ^[a-z0-9-]+\$ (got '$slug')" >&2; return 1; }
  # category becomes the jobs.type literal — restrict to a safe category charset.
  [[ "$category" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "permit-uat: job.category has illegal chars (got '$category')" >&2; return 1; }

  echo "permit-uat: seeding Validation-Jones client + job + permit_profile ..." >&2

  # 1. Upsert the placeholder client.
  _permit_sql "$slug" "
    INSERT INTO clients (name, address)
    SELECT 'Validation Jones', '${address}'
    WHERE NOT EXISTS (SELECT 1 FROM clients WHERE lower(name) = 'validation jones')" >/dev/null

  # 2. Known permit_profile constants (the 'profile' resolver source). Upsert via
  #    ON CONFLICT (tenant_slug,key) so a re-run keeps the canonical values.
  _permit_sql "$slug" "
    INSERT INTO permit_profile (tenant_slug, key, value) VALUES
      ('${slug}', 'state_license', '1234567'),
      ('${slug}', 'business_license', 'BL-0001'),
      ('${slug}', 'contractor_phone', '3105550100')
    ON CONFLICT (tenant_slug, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()" >/dev/null

  # 3. Recreate the Jones job (clean slate). Delete any prior Jones job first so
  #    the assertion window has exactly one. address stored as a JSON string.
  _permit_sql "$slug" "
    DELETE FROM jobs WHERE client_id IN
      (SELECT id FROM clients WHERE lower(name) = 'validation jones')" >/dev/null
  _permit_sql "$slug" "
    INSERT INTO jobs (client_id, description, address, type, service_size_amps)
    SELECT c.id, 'Validation-Jones permit-fill UAT', to_jsonb('${address}'::text),
           '${category}', 200
    FROM clients c WHERE lower(c.name) = 'validation jones'
    LIMIT 1" >/dev/null

  # Return the seeded job_id.
  local jid_json jid
  jid_json="$(_permit_sql "$slug" "
    SELECT j.id FROM jobs j JOIN clients c ON c.id = j.client_id
    WHERE lower(c.name) = 'validation jones' ORDER BY j.created_at DESC LIMIT 1")"
  jid="$(printf '%s' "$jid_json" | python3 -c '
import json,sys
d=json.load(sys.stdin); r=d.get("result",d) if isinstance(d,dict) else d
print(r[0].get("id","") if r else "")' 2>/dev/null || echo "")"
  if [[ -z "$jid" ]]; then
    echo "permit-uat: seed failed — could not resolve Jones job_id" >&2
    return 1
  fi
  echo "permit-uat: seeded job_id=$jid" >&2
  printf '%s' "$jid"
}

# Send each fixture turn via tg-send and read replies via tg-read. Mirrors the
# bid-replay send loop (cursor advances on each new bot message id). Echoes a
# transcript to stderr; leaves the cursor on stdout.
_permit_replay_send_turns() {
  local slug="$1" fixture="$2" wait_s="$3" quiet_s="$4"
  local total
  total="$(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))["turns"]))' "$fixture")"
  local cursor=0 n
  for ((n = 0; n < total; n++)); do
    local text label
    text="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["turns"][int(sys.argv[2])]["text"])' "$fixture" "$n")"
    label="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["turns"][int(sys.argv[2])].get("label",""))' "$fixture" "$n")"
    printf '\n>>> TURN %s/%s [%s]\n%s\n' "$((n + 1))" "$total" "$label" "$text" >&2

    local send_out sent_id
    send_out="$(tenant_tg_send_main "$slug" "$text")" || {
      echo "permit-uat: tg-send failed on turn $n" >&2; return 1; }
    sent_id="$(printf '%s' "$send_out" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("sent_id",0))')"
    if [[ "$sent_id" =~ ^[0-9]+$ ]] && (( sent_id > cursor )); then cursor="$sent_id"; fi

    local read_out
    read_out="$(tenant_tg_read_main "$slug" --since "$cursor" --wait "$wait_s" --quiet "$quiet_s")" || {
      echo "permit-uat: tg-read failed on turn $n" >&2; return 1; }
    cursor="$(printf '%s' "$read_out" | python3 -c '
import json, sys
data = json.load(sys.stdin)
cur = int(sys.argv[1])
for m in data:
    t = (m.get("text","") or "")
    sys.stderr.write("    <<< BOT: " + t[:600].replace("\n"," ") + "\n")
    if int(m.get("id", 0)) > cur:
        cur = int(m["id"])
print(cur)
' "$cursor")"
  done
  printf '%s' "$cursor"
}

# Post-run assertions: verify up to browser_sessions for the seeded job.
# Returns 0 = PASS, 1 = FAIL. Asserts:
#   - each expect.fields entry: a field whose persisted label contains
#     label_match, with the expected value_match / source / status==blank,
#   - documents_outstanding: every doc key present in missing_inputs,
#   - never_submitted: status is never 'submitted_by_human'.
_permit_replay_assert() {
  local slug="$1" fixture="$2" job_id="$3"
  local fails=0
  echo "" >&2
  echo "=== ASSERTIONS (verify up to browser_sessions) — job_id=$job_id ===" >&2

  # job_id must be a UUID before it reaches SQL.
  local uuid_re='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
  if ! [[ "$job_id" =~ $uuid_re ]]; then
    echo "  [FAIL] job_id is not a valid UUID ('$job_id')" >&2
    echo "=== RESULT: FAIL (1 assertion) ===" >&2
    return 1
  fi

  # Fetch the newest browser_session for this job (field_results + missing_inputs
  # + status). status_seen is the full status history check for never-submitted.
  local sess_json
  sess_json="$(_permit_sql "$slug" "
    SELECT field_results, status, missing_inputs
    FROM browser_sessions
    WHERE job_id = '${job_id}'
    ORDER BY created_at DESC
    LIMIT 1")"

  local row_count
  row_count="$(printf '%s' "$sess_json" | python3 -c '
import json,sys
d=json.load(sys.stdin); r=d.get("result",d) if isinstance(d,dict) else d
print(len(r))' 2>/dev/null || echo "-1")"
  if [[ "$row_count" != "1" ]]; then
    echo "  [FAIL] browser_sessions rows for job = $row_count (expected exactly 1 — did start_permits_for_job auto-start with target_override?)" >&2
    echo "=== RESULT: FAIL ===" >&2
    return 1
  fi

  # 1. Per-field assertions. The persisted field_results shape carries a label
  #    (or fieldKey) + intended/readback value + source/status. We match each
  #    expectation against the array flexibly: find the entry whose label/field
  #    contains label_match (case-insensitive), then check value/source/status
  #    where the expectation supplies them. python3 owns the matching so a
  #    portal-specific shape (field vs label, intended vs value) all resolve.
  # Display block: emit human-readable PASS/FAIL lines to stderr (stdout count
  # discarded here; the authoritative count is computed by the block below).
  printf '%s' "$sess_json" | python3 -c '
import json, sys
fixture = sys.argv[1]
fx = json.load(open(fixture))
exp_fields = fx.get("expect", {}).get("fields", []) or []
try:
    d = json.load(sys.stdin)
except Exception:
    print("PARSE_ERR"); sys.exit(0)
rows = d.get("result", d) if isinstance(d, dict) else d
fr = rows[0].get("field_results") if rows else None
if isinstance(fr, str):
    try: fr = json.loads(fr)
    except Exception: fr = []
fr = fr or []

def label_of(e):
    return str(e.get("label") or e.get("fieldKey") or e.get("field") or "").lower()
def value_of(e):
    for k in ("readback", "value", "intended", "intended_value"):
        v = e.get(k)
        if v is not None: return str(v)
    return ""
def source_of(e):
    return str(e.get("source") or "")
def status_of(e):
    # explicit status, else derive: empty value == blank
    s = e.get("status")
    if s: return str(s)
    return "blank" if value_of(e) in ("", "None", "null") else "filled"

fails = 0
for spec in exp_fields:
    lm = str(spec.get("label_match", "")).lower()
    match = next((e for e in fr if lm and lm in label_of(e)), None)
    if match is None:
        sys.stderr.write("  [FAIL] no field matching label \"%s\"\n" % lm); fails += 1; continue
    if "value_match" in spec:
        want = str(spec["value_match"]).lower()
        got = value_of(match).lower()
        if want in got:
            sys.stderr.write("  [PASS] field \"%s\" value contains \"%s\"\n" % (lm, want))
        else:
            sys.stderr.write("  [FAIL] field \"%s\" value=\"%s\" missing \"%s\"\n" % (lm, value_of(match), want)); fails += 1
    if "source" in spec:
        if source_of(match) == spec["source"]:
            sys.stderr.write("  [PASS] field \"%s\" source=%s\n" % (lm, spec["source"]))
        else:
            sys.stderr.write("  [FAIL] field \"%s\" source=%s (expected %s)\n" % (lm, source_of(match), spec["source"])); fails += 1
    if spec.get("status") == "blank":
        if status_of(match) == "blank":
            sys.stderr.write("  [PASS] field \"%s\" left blank (cascade did not hard-fail)\n" % lm)
        else:
            sys.stderr.write("  [FAIL] field \"%s\" status=%s (expected blank)\n" % (lm, status_of(match))); fails += 1
print(fails)
' "$fixture" 1>/dev/null || true
  # The display block emits PASS/FAIL lines on stderr (shown). The block below
  # re-runs to capture the authoritative fail count cleanly to fold into $fails.
  local field_fail_count
  field_fail_count="$(printf '%s' "$sess_json" | python3 -c '
import json, sys
fx = json.load(open(sys.argv[1]))
exp_fields = fx.get("expect", {}).get("fields", []) or []
try:
    d = json.load(sys.stdin)
except Exception:
    print("99"); sys.exit(0)
rows = d.get("result", d) if isinstance(d, dict) else d
fr = rows[0].get("field_results") if rows else None
if isinstance(fr, str):
    try: fr = json.loads(fr)
    except Exception: fr = []
fr = fr or []
def label_of(e): return str(e.get("label") or e.get("fieldKey") or e.get("field") or "").lower()
def value_of(e):
    for k in ("readback","value","intended","intended_value"):
        v = e.get(k)
        if v is not None: return str(v)
    return ""
def source_of(e): return str(e.get("source") or "")
def status_of(e):
    s = e.get("status")
    if s: return str(s)
    return "blank" if value_of(e) in ("","None","null") else "filled"
fails = 0
for spec in exp_fields:
    lm = str(spec.get("label_match","")).lower()
    m = next((e for e in fr if lm and lm in label_of(e)), None)
    if m is None: fails += 1; continue
    if "value_match" in spec and str(spec["value_match"]).lower() not in value_of(m).lower(): fails += 1
    if "source" in spec and source_of(m) != spec["source"]: fails += 1
    if spec.get("status") == "blank" and status_of(m) != "blank": fails += 1
print(fails)
' "$fixture" 2>/dev/null || echo "99")"
  [[ "$field_fail_count" =~ ^[0-9]+$ ]] || field_fail_count=99
  fails=$((fails + field_fail_count))

  # 2. documents_outstanding: each declared doc key must appear in missing_inputs.
  local doc_fails
  doc_fails="$(printf '%s' "$sess_json" | python3 -c '
import json, sys
fx = json.load(open(sys.argv[1]))
docs = fx.get("expect", {}).get("documents_outstanding", []) or []
try:
    d = json.load(sys.stdin)
except Exception:
    print("99"); sys.exit(0)
rows = d.get("result", d) if isinstance(d, dict) else d
mi = rows[0].get("missing_inputs") if rows else None
if isinstance(mi, str):
    try: mi = json.loads(mi)
    except Exception: mi = []
mi = mi or []
keys = set()
for m in mi:
    if isinstance(m, dict):
        keys.add(str(m.get("key","")))
        keys.add(str(m.get("label","")))
    else:
        keys.add(str(m))
fails = 0
for doc in docs:
    if doc in keys:
        sys.stderr.write("  [PASS] document parked in missing_inputs: %s\n" % doc)
    else:
        sys.stderr.write("  [FAIL] document NOT parked in missing_inputs: %s\n" % doc); fails += 1
print(fails)
' "$fixture" 2>&1 1>/dev/null; printf '')"
  echo "$doc_fails" >&2
  local doc_fail_count
  doc_fail_count="$(printf '%s' "$sess_json" | python3 -c '
import json, sys
fx = json.load(open(sys.argv[1]))
docs = fx.get("expect", {}).get("documents_outstanding", []) or []
try:
    d = json.load(sys.stdin)
except Exception:
    print("99"); sys.exit(0)
rows = d.get("result", d) if isinstance(d, dict) else d
mi = rows[0].get("missing_inputs") if rows else None
if isinstance(mi, str):
    try: mi = json.loads(mi)
    except Exception: mi = []
mi = mi or []
keys = set()
for m in mi:
    if isinstance(m, dict):
        keys.add(str(m.get("key",""))); keys.add(str(m.get("label","")))
    else:
        keys.add(str(m))
print(sum(1 for doc in docs if doc not in keys))
' "$fixture" 2>/dev/null || echo "99")"
  [[ "$doc_fail_count" =~ ^[0-9]+$ ]] || doc_fail_count=99
  fails=$((fails + doc_fail_count))

  # 3. never_submitted: the latest status must NOT be 'submitted_by_human'. The
  #    fill subsystem can ONLY reach that state via human live-takeover, so its
  #    presence after an agent-only replay is a hard failure of the never-submit
  #    invariant. (Belt-and-suspenders: also scan ALL sessions for the job.)
  local want_never
  want_never="$(_permit_fixture_get "$fixture" "expect.never_submitted")"
  if [[ "$want_never" == "True" || "$want_never" == "true" ]]; then
    local submitted_json submitted_n
    submitted_json="$(_permit_sql "$slug" "
      SELECT count(*) AS n FROM browser_sessions
      WHERE job_id = '${job_id}' AND status = 'submitted_by_human'")"
    submitted_n="$(printf '%s' "$submitted_json" | python3 -c '
import json,sys
d=json.load(sys.stdin); r=d.get("result",d) if isinstance(d,dict) else d
print(r[0].get("n","") if r else "ERR")' 2>/dev/null || echo "ERR")"
    if [[ "$submitted_n" == "0" ]]; then
      echo "  [PASS] never auto-submitted (no session reached submitted_by_human)" >&2
    else
      echo "  [FAIL] $submitted_n session(s) status='submitted_by_human' — never-submit invariant violated" >&2
      fails=$((fails + 1))
    fi
  fi

  echo "" >&2
  if [[ "$fails" == "0" ]]; then
    echo "=== RESULT: PASS — fill resolved from the right sources, docs parked, never submitted ===" >&2
    return 0
  fi
  echo "=== RESULT: FAIL ($fails assertion(s)) ===" >&2
  return 1
}

tenant_permit_replay_run() {
  local fixture="" dry=0 assert=1 pr=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run)   dry=1; shift ;;
      --no-assert) assert=0; shift ;;
      --pr)        pr="${2:-}"; shift 2 2>/dev/null || shift 1 ;;
      --help|-h|help) tenant_permit_replay_usage; return 0 ;;
      -*)          echo "permit-uat: unknown flag: $1" >&2; return 2 ;;
      *)
        if [[ -z "$fixture" ]]; then fixture="$1"
        else echo "permit-uat: unexpected arg: $1" >&2; return 2
        fi
        shift ;;
    esac
  done

  if [[ -z "$fixture" ]]; then
    tenant_permit_replay_usage
    return 2
  fi
  if [[ ! -f "$fixture" ]]; then
    echo "permit-uat: fixture not found: $fixture" >&2
    return 1
  fi
  if ! python3 -c 'import json,sys;json.load(open(sys.argv[1]))' "$fixture" 2>/dev/null; then
    echo "permit-uat: fixture is not valid JSON: $fixture" >&2
    return 1
  fi

  local slug
  slug="$(_permit_fixture_get "$fixture" "slug")"
  [[ -z "$slug" ]] && { echo "permit-uat: fixture missing top-level 'slug'" >&2; return 1; }

  # IMPORTANT: the fixture MUST carry a target_override that is a known portal
  # key, else start_permits_for_job will ask which portal instead of
  # auto-starting (TARGET_CONFIG has two portals). Validate it up front.
  local target_override
  target_override="$(_permit_fixture_get "$fixture" "target_override")"
  case "$target_override" in
    redondo-iworq|sce-powerclerk) : ;;
    *) echo "permit-uat: fixture target_override must be a known portal key (redondo-iworq|sce-powerclerk); got '$target_override'" >&2
       echo "           without it the single-portal kickoff will not auto-start." >&2
       return 1 ;;
  esac

  local wait_s quiet_s
  wait_s="$(_permit_fixture_get "$fixture" "read.wait")";   [[ -z "$wait_s"  ]] && wait_s=240
  quiet_s="$(_permit_fixture_get "$fixture" "read.quiet")"; [[ -z "$quiet_s" ]] && quiet_s=6
  [[ "$wait_s"  =~ ^[0-9]+$ ]] || { echo "permit-uat: read.wait must be a non-negative integer (got '$wait_s')"   >&2; return 1; }
  [[ "$quiet_s" =~ ^[0-9]+$ ]] || { echo "permit-uat: read.quiet must be a non-negative integer (got '$quiet_s')" >&2; return 1; }

  if [[ "$dry" == "1" ]]; then
    echo "=== DRY RUN — $fixture (wait=${wait_s}s quiet=${quiet_s}s) ===" >&2
    python3 -c '
import json,sys
d=json.load(open(sys.argv[1]))
print("slug:", d.get("slug"))
print("target_override:", d.get("target_override"))
for t in d["turns"]:
    print("  TURN %2d [%s]: %s" % (t["n"], t.get("label",""), t["text"][:120].replace(chr(10)," ")))
print("expect:", json.dumps(d.get("expect",{})))
' "$fixture" >&2
    return 0
  fi

  require_operator_secret TELEGRAM_SESSION_STRING "run: harness tenant tg-login"

  echo "=== permit-uat: $slug — $(basename "$fixture") — target=$target_override ===" >&2

  # Seed the Jones job + permit_profile and capture the job_id the assertions
  # scope to. The agent's start_permits_for_job will create the browser_session
  # for this job; the seed gives the resolver deterministic profile/app_data.
  local job_id
  job_id="$(_permit_seed "$slug" "$fixture")" || return 1

  # Replay the turns against the deployed bot.
  _permit_replay_send_turns "$slug" "$fixture" "$wait_s" "$quiet_s" >/dev/null || return 1

  if [[ "$assert" == "0" ]]; then
    echo "permit-uat: turns sent; --no-assert set, skipping verification." >&2
    return 0
  fi

  # Give the fill engine + park step time to settle before reading back.
  local settle
  settle="$(_permit_fixture_get "$fixture" "final_settle_seconds")"; [[ -z "$settle" ]] && settle=120
  [[ "$settle" =~ ^[0-9]+$ ]] || { echo "permit-uat: final_settle_seconds must be a non-negative integer (got '$settle')" >&2; return 1; }
  echo "permit-uat: settling ${settle}s for fill + park ..." >&2
  sleep "$settle"

  local rc=0
  _permit_replay_assert "$slug" "$fixture" "$job_id" || rc=1

  # Optional: record the outcome to the UAT audit log when --pr was supplied.
  if [[ -n "$pr" ]]; then
    if [[ "$rc" == "0" ]]; then
      uat_record_main --pr "$pr" --category permit-fill --driver agent \
        --observed "permit-uat PASS: Validation-Jones fill resolved from profile/app_data, ${target_override} session parked outstanding docs, never auto-submitted (job ${job_id})." || true
    else
      uat_record_main --pr "$pr" --category permit-fill --driver agent \
        --observed "permit-uat FAIL: assertions failed against browser_sessions for job ${job_id} (${target_override}). See run transcript." || true
    fi
  fi

  return "$rc"
}

tenant_permit_replay_main() {
  local subcmd="${1:-help}"
  shift 2>/dev/null || true
  case "$subcmd" in
    run)
      tenant_permit_replay_run "$@"
      ;;
    help|-h|--help|"")
      tenant_permit_replay_usage
      return 0
      ;;
    *)
      echo "permit-uat: unknown subcommand: $subcmd" >&2
      tenant_permit_replay_usage
      return 2
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "${0:-}" ]]; then
  echo "tenant_permit_replay.sh: source + call tenant_permit_replay_main" >&2
  exit 1
fi
