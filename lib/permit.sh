# lib/permit.sh — `harness permit …` operator control surface for the permit KB.
# Thin: reads/writes via tenant SQL; queues crawl runs the cron picks up. The
# heavy crawl+extract+diff runs server-side (see crawl-runner.ts). NEVER submits.
set -euo pipefail

: "${SCRIPT_DIR:=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)}"

PERMIT_TENANT="${PERMIT_TENANT:-tyler-electric}"

_permit_sql() {
  local query="$1"; shift || true
  if [[ "${PERMIT_DRY_RUN:-0}" == "1" ]]; then printf '%s\n' "$query"; return 0; fi
  # shellcheck source=./tenant_sql.sh
  source "$SCRIPT_DIR/lib/tenant_sql.sh"
  tenant_sql_main "$PERMIT_TENANT" "$query" "$@"
}

# SQL-escape a single-quoted literal (double any embedded quote). bash 3.2 safe.
_permit_lit() { local sq="'"; printf "%s" "${1//$sq/$sq$sq}"; }

permit_usage() {
  cat <<'EOF'
Usage:
  harness permit enumerate [authority]        Queue a breadth crawl that refreshes the backlog
  harness permit list [--authority A] [--status S] [--low-confidence]
  harness permit crawl <target_id>            Queue a depth crawl of one target (you pick)
  harness permit show <target_id>             Print the full extracted model for a target
  harness permit diff <target_id>             Print the latest drift for a target
  harness permit verify <target_id>           Mark low-confidence rows confirmed
EOF
}

permit_main() {
  local sub="${1:-}"; shift || true
  case "$sub" in
    enumerate)
      local authority="${1:-}"; local lit; lit="$(_permit_lit "$authority")"
      _permit_sql "INSERT INTO crawl_runs (mode, authority_slug, status) VALUES ('enumerate', NULLIF('$lit',''), 'queued') RETURNING id;"
      ;;
    list)
      local authority="" status="" lowconf=0
      while [[ $# -gt 0 ]]; do case "$1" in
        --authority) authority="$2"; shift 2;;
        --status) status="$2"; shift 2;;
        --low-confidence) lowconf=1; shift;;
        *) echo "permit list: unknown flag $1" >&2; return 2;;
      esac; done
      local where="WHERE 1=1"
      [[ -n "$authority" ]] && where="$where AND a.slug = '$(_permit_lit "$authority")'"
      [[ -n "$status" ]] && where="$where AND ct.status = '$(_permit_lit "$status")'"
      [[ "$lowconf" == "1" ]] && where="$where AND (EXISTS (SELECT 1 FROM permit_fields pf WHERE pf.snapshot_id=(SELECT id FROM crawl_snapshots WHERE target_id=ct.id ORDER BY captured_at DESC LIMIT 1) AND pf.confidence='inferred') OR EXISTS (SELECT 1 FROM permit_requirements pr WHERE pr.snapshot_id=(SELECT id FROM crawl_snapshots WHERE target_id=ct.id ORDER BY captured_at DESC LIMIT 1) AND pr.confidence='inferred'))"
      _permit_sql "SELECT ct.id, a.slug AS authority, s.slug AS system, ct.permit_category, ct.status, ct.priority, ct.last_crawled_at FROM crawl_targets ct JOIN permit_systems s ON s.id = ct.system_id JOIN permit_authorities a ON a.id = s.authority_id $where ORDER BY ct.priority, a.slug;" --table
      ;;
    crawl)
      local target="${1:-}"; [[ -n "$target" ]] || { echo "permit crawl: target_id required" >&2; return 2; }
      _permit_sql "WITH sel AS (UPDATE crawl_targets SET status='selected', updated_at=now() WHERE id='$(_permit_lit "$target")' RETURNING id) INSERT INTO crawl_runs (mode, target_id, status) SELECT 'depth', id, 'queued' FROM sel RETURNING id;"
      ;;
    show)
      local target="${1:-}"; [[ -n "$target" ]] || { echo "permit show: target_id required" >&2; return 2; }
      local t; t="$(_permit_lit "$target")"
      _permit_sql "SELECT 'form' AS kind, name AS detail, NULL::text AS extra FROM permit_forms WHERE target_id='$t' AND snapshot_id=(SELECT id FROM crawl_snapshots WHERE target_id='$t' ORDER BY captured_at DESC LIMIT 1) UNION ALL SELECT 'requirement', kind, confidence FROM permit_requirements WHERE target_id='$t' AND snapshot_id=(SELECT id FROM crawl_snapshots WHERE target_id='$t' ORDER BY captured_at DESC LIMIT 1) ORDER BY 1;" --table
      ;;
    diff)
      local target="${1:-}"; [[ -n "$target" ]] || { echo "permit diff: target_id required" >&2; return 2; }
      _permit_sql "SELECT created_at, has_material, changes FROM crawl_diffs WHERE target_id='$(_permit_lit "$target")' ORDER BY created_at DESC LIMIT 1;" --json
      ;;
    verify)
      local target="${1:-}"; [[ -n "$target" ]] || { echo "permit verify: target_id required" >&2; return 2; }
      local t; t="$(_permit_lit "$target")"
      # Promote BOTH the deterministic-but-inferred fields AND the LLM-inferred
      # requirements (classify-requirements writes requirements at confidence
      # 'inferred' and points the operator here); `list --low-confidence` flags
      # both, so verify must clear both or requirements stay inferred forever.
      _permit_sql "UPDATE permit_fields SET confidence='high' WHERE confidence='inferred' AND snapshot_id=(SELECT id FROM crawl_snapshots WHERE target_id='$t' ORDER BY captured_at DESC LIMIT 1) RETURNING id;"
      _permit_sql "UPDATE permit_requirements SET confidence='high' WHERE confidence='inferred' AND snapshot_id=(SELECT id FROM crawl_snapshots WHERE target_id='$t' ORDER BY captured_at DESC LIMIT 1) RETURNING id;"
      ;;
    ""|-h|--help|help) permit_usage ;;
    *) echo "harness permit: unknown subcommand '$sub'" >&2; permit_usage >&2; return 2 ;;
  esac
}
