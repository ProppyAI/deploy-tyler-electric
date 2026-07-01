#!/usr/bin/env bash
# uat.sh — `harness uat record` and `harness uat record --show`.
#
# Appends per-PR UAT outcomes to .personal/uat/<YYYY-MM-DD>-pr-<#>.md
# (override via UAT_DIR env var for testing). Markdown-only; no GitHub
# API calls. See docs/superpowers/specs/2026-05-27-uat-record-tooling-design.md.
#
# Usage:
#   harness uat record --pr N --category X --driver agent|operator --observed "<verbatim>"
#   harness uat record --pr N --show

set -euo pipefail

uat_record_usage() {
  cat >&2 <<USAGE
Usage:
  harness uat record --pr <PR#> --category <name> --driver <agent|operator> --observed "<quote>"
  harness uat record --pr <PR#> --show

Record mode appends a step entry to .personal/uat/<YYYY-MM-DD>-pr-<PR#>.md
(override location via UAT_DIR env var). All four flags required.

Show mode prints the audit log(s) for the PR to stdout, most-recent-first.
Only --pr required.
USAGE
}

uat_record_main() {
  local pr="" category="" driver="" observed="" show=0
  local observed_set=0

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --pr)
        pr="${2:-}"; shift 2 || { uat_record_usage; return 2; }
        ;;
      --category)
        category="${2:-}"; shift 2 || { uat_record_usage; return 2; }
        ;;
      --driver)
        driver="${2:-}"; shift 2 || { uat_record_usage; return 2; }
        ;;
      --observed)
        observed="${2:-}"; observed_set=1; shift 2 || { uat_record_usage; return 2; }
        ;;
      --show)
        show=1; shift
        ;;
      -h|--help)
        uat_record_usage; return 0
        ;;
      *)
        printf "unknown flag: %s\n" "$1" >&2
        uat_record_usage
        return 2
        ;;
    esac
  done

  # --pr is required in both modes.
  if [[ -z "$pr" ]]; then
    printf "error: --pr is required\n" >&2
    uat_record_usage
    return 2
  fi
  if ! [[ "$pr" =~ ^[0-9]+$ ]]; then
    printf "error: --pr must be a positive integer (got: %s)\n" "$pr" >&2
    uat_record_usage
    return 2
  fi

  if [[ $show -eq 1 ]]; then
    local uat_dir="${UAT_DIR:-.personal/uat}"
    if [[ ! -d "$uat_dir" ]]; then
      printf "no UAT log for PR #%s (UAT_DIR not found: %s)\n" "$pr" "$uat_dir" >&2
      return 1
    fi

    # Collect all *-pr-<PR>.md files, sort descending by filename (which is
    # YYYY-MM-DD-pr-<#>.md — string sort is also chronological sort).
    local files=()
    while IFS= read -r f; do
      files+=("$f")
    done < <(find "$uat_dir" -maxdepth 1 -type f -name "*-pr-$pr.md" 2>/dev/null | sort -r)

    if [[ ${#files[@]} -eq 0 ]]; then
      printf "no UAT log for PR #%s in %s\n" "$pr" "$uat_dir" >&2
      return 1
    fi

    local first=1
    for f in "${files[@]}"; do
      if [[ $first -eq 0 ]]; then
        printf "\n---\n\n"
      fi
      cat "$f"
      first=0
    done
    return 0
  fi

  # Record mode — all four flags required.
  if [[ -z "$category" ]]; then
    printf "error: --category is required in record mode\n" >&2
    uat_record_usage
    return 2
  fi
  if [[ -z "$driver" ]]; then
    printf "error: --driver is required in record mode\n" >&2
    uat_record_usage
    return 2
  fi
  if [[ "$driver" != "agent" && "$driver" != "operator" ]]; then
    printf "error: --driver must be agent|operator (got: %s)\n" "$driver" >&2
    uat_record_usage
    return 2
  fi
  if [[ $observed_set -eq 0 || -z "$observed" ]]; then
    printf "error: --observed is required and must be non-empty\n" >&2
    uat_record_usage
    return 2
  fi

  # Write path.
  local uat_dir="${UAT_DIR:-.personal/uat}"
  mkdir -p "$uat_dir" || {
    printf "error: cannot create UAT dir: %s\n" "$uat_dir" >&2
    return 3
  }
  local today
  today="$(date -u +%Y-%m-%d)"
  local log_file="$uat_dir/$today-pr-$pr.md"
  local now
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  if [[ ! -f "$log_file" ]]; then
    # First write — emit header.
    local pr_url=""
    # Best-effort PR URL lookup; degrade silently if gh is absent or fails.
    if command -v gh >/dev/null 2>&1; then
      pr_url=$(gh pr view "$pr" --json url --jq .url 2>/dev/null || true)
    fi
    {
      printf "# UAT — PR #%s\n" "$pr"
      printf "First recorded: %s\n" "$now"
      if [[ -n "$pr_url" ]]; then
        printf "PR URL: %s\n" "$pr_url"
      fi
      printf "\n"
    } >> "$log_file"
  fi

  # Append step entry. observed: uses YAML block scalar (|) to preserve newlines.
  {
    printf "## Step recorded %s\n" "$now"
    printf -- "- category: %s\n" "$category"
    printf -- "- driver: %s\n" "$driver"
    printf -- "- observed: |\n"
    # Indent every line of $observed by 6 spaces under the pipe.
    printf "%s\n" "$observed" | sed 's/^/      /'
    printf "\n"
  } >> "$log_file"

  return 0
}
