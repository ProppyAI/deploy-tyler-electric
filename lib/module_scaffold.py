#!/usr/bin/env python3
"""Generate a new module repo structure from a template."""

import json
import os
import re
import subprocess
import sys

VALID_CATEGORIES = {"ops", "frontend", "backend", "data", "content", "social",
                    "ml", "llm", "security", "analytics", "scientific", "comms"}


def scaffold_module(name, target_path, category="ops", produces=None, consumes=None):
    """Generate module directory structure.

    Args:
        name: Module name (lowercase with hyphens)
        target_path: Where to create the module directory
        category: Module category (default: ops)
        produces: List of entity names this module produces
        consumes: List of entity names this module consumes
    """
    produces = produces or []
    consumes = consumes or []

    # Guard against overwriting existing module files
    for fname in ["module.harness.json", "README.md", "CLAUDE.md"]:
        if os.path.exists(os.path.join(target_path, fname)):
            print(f"ERROR: {fname} already exists at {target_path}. Remove it first or use a different path.", file=sys.stderr)
            sys.exit(1)

    os.makedirs(target_path, exist_ok=True)
    os.makedirs(os.path.join(target_path, "hooks"), exist_ok=True)
    # .gitkeep so git tracks the empty hooks directory
    open(os.path.join(target_path, "hooks", ".gitkeep"), "w").close()

    # Generate module.harness.json
    manifest = {
        "name": name,
        "version": "1.0.0",
        "description": f"HARNESS module: {name}",
        "category": category,
        "entities": {
            "produces": produces,
            "consumes": consumes,
            "extends": {}
        },
        "tools": [],
        "hooks": [],
        "dependencies": [],
        "externalServices": [],
        "config": {},
        "agents": [],
        "cron": []
    }

    manifest_path = os.path.join(target_path, "module.harness.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    # Generate README.md
    produces_str = ", ".join(produces) if produces else "none"
    consumes_str = ", ".join(consumes) if consumes else "none"
    readme = f"""# module-{name}

HARNESS module: {name}.

## Entity Contract

- **Produces:** {produces_str}
- **Consumes:** {consumes_str}

## Tools

(Add tools to module.harness.json)

## Hooks

Hook scripts go in the `hooks/` directory. Each receives JSON on stdin and outputs JSON on stdout.

## Validation

```bash
harness module validate .
```

## Development

1. Add tools, hooks, agents, and cron jobs to `module.harness.json`
2. Create hook scripts in `hooks/` (must be executable)
3. Run `harness module validate .` to verify
4. Open a PR — the HARNESS pipeline reviews automatically
"""

    readme_path = os.path.join(target_path, "README.md")
    with open(readme_path, "w") as f:
        f.write(readme)

    # Generate CLAUDE.md
    claude_md = f"""# module-{name} — Project Standards

This module is part of the HARNESS ecosystem for ProppyAI.

## Module Contract

- **Name:** {name}
- **Category:** {category}
- **Produces:** {produces_str}
- **Consumes:** {consumes_str}

## Code Standards

- Hook scripts receive JSON on stdin, output JSON on stdout
- Hook scripts must be executable (`chmod +x`)
- All tools must declare permissions as `entity:read|write`
- Config values must have `type` and `default` fields
- Run `harness module validate .` before every PR

## Backbone

This repo is governed by the HARNESS backbone. Run `harness update` to pull latest standards.
"""

    claude_path = os.path.join(target_path, "CLAUDE.md")
    with open(claude_path, "w") as f:
        f.write(claude_md)

    return manifest_path


def create_github_repo(name, target_path, org="ProppyAI"):
    """Create a GitHub repo and push the scaffolded module."""
    repo_name = f"module-{name}"

    # Create repo
    result = subprocess.run(
        ["gh", "repo", "create", f"{org}/{repo_name}", "--private",
         "--description", f"HARNESS module: {name}", "--clone=false"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  ERROR: Failed to create repo: {result.stderr.strip()}", file=sys.stderr)
        return False

    # Init git and push
    cmds = [
        ["git", "init", "-b", "main", "-q"],
        ["git", "add", "module.harness.json", "hooks", "README.md", "CLAUDE.md"],
        ["git", "commit", "-q", "-m", f"feat: initial module scaffold for {name}"],
        ["git", "remote", "add", "origin", f"https://github.com/{org}/{repo_name}.git"],
        ["git", "push", "-u", "origin", "HEAD"],
    ]

    for cmd in cmds:
        result = subprocess.run(cmd, cwd=target_path, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  ERROR: {' '.join(cmd)}: {result.stderr.strip()}", file=sys.stderr)
            return False

    return True


def main():
    if len(sys.argv) < 3:
        print("Usage: module_scaffold.py <name> <target-path> [--category cat] [--produces e1,e2] [--consumes e1,e2] [--create-repo] [--org org]", file=sys.stderr)
        sys.exit(2)

    name = sys.argv[1]
    target_path = os.path.realpath(os.path.abspath(sys.argv[2]))

    # Validate module name
    if not re.fullmatch(r'[a-z][a-z0-9-]+', name) or len(name) > 50:
        print(f"ERROR: invalid module name '{name}'. Must be 2-50 lowercase alphanumeric chars or hyphens, starting with a letter.", file=sys.stderr)
        sys.exit(2)

    # Parse flags
    category = "ops"
    produces = []
    consumes = []
    create_repo = False
    org = "ProppyAI"

    i = 3
    while i < len(sys.argv):
        if sys.argv[i] in ("--category", "--produces", "--consumes", "--org") and i + 1 >= len(sys.argv):
            print(f"ERROR: {sys.argv[i]} requires a value", file=sys.stderr)
            sys.exit(2)
        elif sys.argv[i] == "--category":
            category = sys.argv[i + 1]
            i += 2
        elif sys.argv[i] == "--produces":
            produces = [x.strip() for x in sys.argv[i + 1].split(",") if x.strip()]
            i += 2
        elif sys.argv[i] == "--consumes":
            consumes = [x.strip() for x in sys.argv[i + 1].split(",") if x.strip()]
            i += 2
        elif sys.argv[i] == "--create-repo":
            create_repo = True
            i += 1
        elif sys.argv[i] == "--org":
            org = sys.argv[i + 1]
            i += 2
        else:
            print(f"ERROR: unknown flag '{sys.argv[i]}'", file=sys.stderr)
            sys.exit(2)

    # Validate category
    if category not in VALID_CATEGORIES:
        print(f"ERROR: invalid category '{category}'. Must be one of: {', '.join(sorted(VALID_CATEGORIES))}", file=sys.stderr)
        sys.exit(2)

    # Validate org name
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9-]*', org) or len(org) > 39:
        print(f"ERROR: invalid org name '{org}'. Must be 1-39 alphanumeric chars or hyphens.", file=sys.stderr)
        sys.exit(2)

    # Validate entity names
    for ent in produces + consumes:
        if not re.fullmatch(r'[a-z][a-z0-9-]*', ent):
            print(f"ERROR: invalid entity name '{ent}'. Must be lowercase alphanumeric/hyphens.", file=sys.stderr)
            sys.exit(2)

    print(f"HARNESS — Scaffolding module: {name}\n")

    manifest_path = scaffold_module(name, target_path, category, produces, consumes)
    print(f"  Created: module.harness.json")
    print(f"  Created: hooks/")
    print(f"  Created: README.md")
    print(f"  Created: CLAUDE.md")

    if create_repo:
        print(f"\n  Creating GitHub repo: {org}/module-{name}...")
        if create_github_repo(name, target_path, org):
            print(f"  Repo created and pushed.")
        else:
            print(f"  Repo creation failed — files are still at {target_path}")
            sys.exit(1)

    print(f"\n  Module scaffolded at: {target_path}")
    print(f"\n  Next steps:")
    print(f"    1. Add tools, hooks, agents to module.harness.json")
    print(f"    2. Create hook scripts in hooks/")
    print(f"    3. Run: harness module validate {target_path}")
    if not create_repo:
        print(f"    4. Run: harness module scaffold {name} {target_path} --create-repo  (to push to GitHub)")


if __name__ == "__main__":
    main()
