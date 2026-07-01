#!/usr/bin/env bash
# lib/worktree-guard.sh — pure decision function for the worktree-edit guard.
# No file I/O, no JSON. Inspects real git state so it can be unit-tested with a
# real repo + worktree. macOS bash 3.2-safe.
#
# worktree_guard_decide <tool_name> <target> <cwd>
#   tool_name : "Edit" | "Write" | "Bash" | (other -> ALLOW)
#   target    : file path (Edit/Write) or full command string (Bash)
#   cwd       : the hook's working directory (used for Bash)
# Prints exactly one verdict on the first line:
#   ALLOW    — permitted
#   OVERRIDE — would block, but HARNESS_ALLOW_SHARED_EDIT=1 (caller allows + logs)
#   BLOCK    — refused; subsequent lines are the human-readable reason

# Returns 0 if the command string is a git *mutation*. Heuristic: matches `git`
# (optionally followed by global flags / `-C <path>`) then a mutating subcommand.
_wg_is_git_mutation() {
  # Value-taking flags (-C <path>, -c <key=val>) are matched BEFORE the bare
  # -flag alternative so their space-separated value token is consumed rather
  # than the bare-flag branch stopping the chain early (else `git -c k=v commit`
  # slips through). Verb list is intentionally narrow — the spec's scoped set;
  # restore/checkout/rm/revert are deliberately out of scope for now.
  printf '%s' "$1" | grep -Eq \
    '(^|[^[:alnum:]_])git([[:space:]]+-C[[:space:]]+[^[:space:]]+|[[:space:]]+-c[[:space:]]+[^[:space:]]+|[[:space:]]+-[^[:space:]]+)*[[:space:]]+(commit|add|apply|am|cherry-pick|merge|rebase|reset)([[:space:]]|$)'
}

worktree_guard_decide() {
  local tool="$1" target="$2" cwd="$3"
  local dir
  case "$tool" in
    Edit|Write) dir="$(dirname "$target")" ;;
    Bash)       dir="$cwd" ;;
    *)          echo "ALLOW"; return 0 ;;
  esac

  # Gate 1: inside a git repo? (cd may fail if dir doesn't exist yet — that's
  # fine for a brand-new file; walk up to the nearest existing parent.)
  local probe="$dir"
  while [[ -n "$probe" && ! -d "$probe" ]]; do probe="$(dirname "$probe")"; done
  local toplevel
  if ! toplevel="$(cd "$probe" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null)"; then
    echo "ALLOW"; return 0
  fi

  # Gate 2: HARNESS-convention repo?
  if [[ ! -d "$toplevel/.harness" ]]; then echo "ALLOW"; return 0; fi

  # Gate 3: shared checkout vs linked worktree (compare resolved abspaths).
  local gitdir commondir
  gitdir="$(cd "$probe" && cd "$(git rev-parse --git-dir)" 2>/dev/null && pwd -P)"
  commondir="$(cd "$probe" && cd "$(git rev-parse --git-common-dir)" 2>/dev/null && pwd -P)"
  if [[ "$gitdir" != "$commondir" ]]; then echo "ALLOW"; return 0; fi

  # Gate 4: HEAD is a shared branch?
  local branch
  branch="$(cd "$probe" && git symbolic-ref --short HEAD 2>/dev/null || true)"
  if [[ "$branch" != "dev" && "$branch" != "main" ]]; then echo "ALLOW"; return 0; fi

  # Gate 5: real-source / mutation check
  case "$tool" in
    Edit|Write)
      if (cd "$probe" && git check-ignore -q -- "$target") 2>/dev/null; then
        echo "ALLOW"; return 0
      fi
      ;;
    Bash)
      if ! _wg_is_git_mutation "$target"; then echo "ALLOW"; return 0; fi
      ;;
  esac

  # Gate 6: explicit, logged override
  if [[ "${HARNESS_ALLOW_SHARED_EDIT:-}" == "1" ]]; then echo "OVERRIDE"; return 0; fi

  # BLOCK
  printf 'BLOCK\n'
  if [[ "$tool" == Bash ]]; then
    printf 'HARNESS: refusing to run git mutations in the shared %s checkout directly.\n' "$branch"
  else
    printf 'HARNESS: refusing to edit the shared %s checkout directly.\n' "$branch"
  fi
  printf 'All feature work goes through a durable worktree.\n'
  printf '  start one:  harness worktree add <intent> <slug>   (then cd into it)\n'
  printf 'Override (logged, discouraged):  HARNESS_ALLOW_SHARED_EDIT=1\n'
  return 0
}
