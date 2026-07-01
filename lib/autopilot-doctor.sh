#!/usr/bin/env bash
# lib/autopilot-doctor.sh — pure forensic decision helpers for `harness autopilot
# doctor` / `watch`. NO process I/O here (the command layer in bin/harness gathers
# ps/lsof/lock/log and passes facts in) so these are fully unit-testable.
# bash 3.2 compatible.

# Convert a `ps -o time=` field to integer seconds. Handles the real shapes:
#   macOS:    "MM:SS.cc" / "HH:MM:SS.cc"   (fractional seconds — dropped)
#   Linux/CI: "[[DD-]HH:]MM:SS"            (optional DD- day prefix — folded in)
# Non-numeric / unexpected input -> 0.
_autopilot_cputime_secs() {
  local t="${1:-}" d=0 h=0 m=0 s=0
  t="${t%.*}"                       # strip fractional seconds suffix (.cc), if any
  case "$t" in
    *-*) d="${t%%-*}"; t="${t#*-}" ;;   # split optional leading day prefix "DD-"
  esac
  case "$t" in
    *:*:*) IFS=: read -r h m s <<<"$t" ;;
    *:*)   IFS=: read -r m s <<<"$t" ;;
    *)     printf '0\n'; return 0 ;;
  esac
  case "$d$h$m$s" in *[!0-9]*|"") printf '0\n'; return 0 ;; esac
  printf '%s\n' "$(( 10#$d * 86400 + 10#$h * 3600 + 10#$m * 60 + 10#$s ))"
}

# Stalled if CPU-time advanced by less than MIN_DELTA seconds over the window.
_autopilot_is_stalled() {
  local prev="${1:-0}" cur="${2:-0}" min="${3:-1}"
  if (( cur - prev < min )); then printf 'stalled\n'; else printf 'progressing\n'; fi
}

# Verdict from facts: lock present?(0/1) pid alive?(0/1) stalled-flag(stalled|progressing)
_autopilot_classify_verdict() {
  local lock="${1:-0}" alive="${2:-0}" stalled="${3:-progressing}"
  if [[ "$lock" != "1" && "$alive" != "1" ]]; then printf 'NONE\n'; return 0; fi
  if [[ "$alive" != "1" ]]; then printf 'DEAD\n'; return 0; fi   # lock held, no live pid
  if [[ "$stalled" == "stalled" ]]; then printf 'STALLED\n'; else printf 'PASS\n'; fi
}

# Is this command line a reap-eligible autopilot/headless-review process?
# Matches the HARNESS_AUTOPILOT marker, or a real dispatched /pr-review --auto-merge
# loop where /pr-review is the actual prompt (immediately after `-p`, optionally
# quoted) — NOT a bare interactive `claude`, an unrelated `claude -p`, or a command
# line that merely mentions the tokens inside a prose prompt.
_autopilot_proc_is_autopilot() {
  local cmd="${1:-}"
  case "$cmd" in
    *HARNESS_AUTOPILOT=1*)                 printf 'yes\n' ;;
    *-p\ /pr-review*--auto-merge*)         printf 'yes\n' ;;
    *-p\ \"/pr-review*--auto-merge*)       printf 'yes\n' ;;
    *-p\ \'/pr-review*--auto-merge*)       printf 'yes\n' ;;
    *)                                     printf 'no\n'  ;;
  esac
}
