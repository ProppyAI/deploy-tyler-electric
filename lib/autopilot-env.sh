#!/usr/bin/env bash
# lib/autopilot-env.sh — git-environment sanitation for headless Autopilot runs.
#
# When a process is launched from inside a git hook (the HARNESS pre-push hook
# dispatches /pr-review), git exports GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE/
# GIT_COMMON_DIR/GIT_PREFIX pointing at the REAL repo. Those override `git -C`,
# so any downstream git call — including temp-repo tests — silently mutates the
# shared HARNESS repo (the documented core.bare/index-scramble corruption).
# GIT_EXEC_PATH is also cleared: left set it would redirect git's sub-command
# binary lookup for the entire headless session (git-subcommand hijack).
# Call autopilot_sanitize_git_env before any headless claude/test invocation.
#
# bash 3.2 compatible. Safe under `set -u` (unset of an unset var is fine).

autopilot_sanitize_git_env() {
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_PREFIX GIT_EXEC_PATH
}
