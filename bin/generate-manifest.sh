#!/usr/bin/env bash
# Generate backbone-manifest.json from the HARNESS repo structure.
# Run from the HARNESS repo root. Output goes to stdout.
#
# Sourcing: this script uses `git ls-files` (NOT disk-glob) so untracked
# working-tree files cannot leak into the manifest. If you add a new file
# that should propagate to tenants, `git add` it first OR add a new
# `git ls-files` glob below.
#
# user_files + removed top-level sections are hard-coded below — they
# change infrequently and live outside the auto-globbed paths. Update
# them by hand when adding/removing user-scope files or queueing tenant
# deletions.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

python3 - << 'PYEOF'
import json, os, subprocess

def ls_files(pattern):
    """Return tracked files matching the pathspec. Empty list if none."""
    out = subprocess.run(
        ["git", "ls-files", "--", pattern],
        check=True, capture_output=True, text=True,
    ).stdout
    return [line for line in out.splitlines() if line]

files = []

# bin/ — all tracked executables (excluding *.md)
for f in sorted(ls_files("bin/*")):
    if os.path.isfile(f) and not f.endswith(".md"):
        files.append({"src": f, "dst": f, "executable": True})

# lib/ — tracked .py, .sh, .md files (top-level)
for f in sorted(ls_files("lib/*")):
    if os.path.isfile(f) and f.split(".")[-1] in ("py", "sh", "md") and "/" not in f[len("lib/"):]:
        files.append({"src": f, "dst": f})

# lib/adapters/ — all tracked files
for f in sorted(ls_files("lib/adapters/*")):
    if os.path.isfile(f):
        files.append({"src": f, "dst": f})

# lib/skills/ — all tracked files
for f in sorted(ls_files("lib/skills/*")):
    if os.path.isfile(f):
        files.append({"src": f, "dst": f})

# lib/karpathy/ — all tracked .py files (runner + __init__)
for f in sorted(ls_files("lib/karpathy/*.py")):
    if os.path.isfile(f):
        files.append({"src": f, "dst": f})

# scripts/ — all tracked .sh files (canary, tenant-management, etc.)
for f in sorted(ls_files("scripts/*.sh")):
    if os.path.isfile(f):
        files.append({"src": f, "dst": f, "executable": True})

# Workflow templates → .github/workflows/ in target
for f in sorted(ls_files("templates/repo-bootstrap/.github/workflows/*")):
    if os.path.isfile(f):
        dst = f.replace("templates/repo-bootstrap/", "")
        files.append({"src": f, "dst": dst})

# Skill templates → .claude/skills/ in target
for f in sorted(ls_files("templates/repo-bootstrap/.claude/skills/*")):
    if os.path.isfile(f):
        dst = f.replace("templates/repo-bootstrap/", "")
        files.append({"src": f, "dst": dst})

# .claude/settings.json and .claude/settings.local.json are intentionally
# NOT in the manifest. They are copied once by harness-init (preserving
# user customizations). Propagating via `harness update` would overwrite
# each consuming repo's team-wide hooks and plugin config on every backbone
# bump — and the file is a shell-command-carrying supply-chain surface,
# which compounds the risk. Ship new hooks as explicit migrations instead.

# Doc templates
for name in ("CLAUDE.md", "REVIEW.md"):
    src = f"templates/repo-bootstrap/{name}"
    if ls_files(src):
        files.append({"src": src, "dst": name})

# Superpowers metric + trace templates — required by
# tests/backbone_manifest_propagation_test.sh; consumed by harness:define-metric
# and harness:trace-audit skills which look for the template alongside the
# skill body in each tenant repo.
for f in sorted(ls_files("docs/superpowers/templates/*")):
    if os.path.isfile(f):
        files.append({"src": f, "dst": f})

# user_files — installed once by bin/harness-setup-dev into the user's
# home (~/.claude/...). NOT propagated by `harness update`; tracked here
# so harness-setup-dev knows what to lay down on each dev machine.
user_files = [
    {
        "src": "lib/user-commands/pr-review.md",
        "dst": "~/.claude/commands/pr-review.md",
        "_note": "Installed by bin/harness-setup-dev — user-scope Claude Code command",
    },
    {
        "src": ".claude/git-hooks/pre-push",
        "dst": "~/.claude/git-hooks/pre-push",
        "mode": "0755",
        "_note": "Installed by bin/harness-setup-dev — global git pre-push hook that dispatches /pr-review on every push with an open PR",
    },
]

# removed — paths to delete from tenant repos on the next sync. Consumed
# by bin/harness-migrate-tenant.
removed = {
    "_note": "Paths to delete from tenant repos on next sync. Tenants still carrying these files should run HARNESS/bin/harness-migrate-tenant to prune them.",
    "paths": [
        ".github/workflows/pr.yml",
        ".claude/skills/fix-pr-reviews.md",
        "~/.claude/commands/pr-review-scan.md",
        "~/Library/LaunchAgents/com.propster.pr-review-scan.plist",
        "~/.claude/pr-review-autoloop/auto-enroll.sh",
    ],
}

manifest = {
    "version": "1.0",
    "files": files,
    "user_files": user_files,
    "removed": removed,
    "directories": [
        {"src": "schemas/", "dst": "schemas/"},
        {"src": "verticals/", "dst": "verticals/"}
    ]
}

print(json.dumps(manifest, indent=2))
PYEOF
