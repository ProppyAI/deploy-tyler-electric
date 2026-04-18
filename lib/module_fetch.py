#!/usr/bin/env python3
"""Fetch module manifests and hooks from GitHub repos into local .harness/modules/ cache."""

import json
import os
import re
import subprocess
import sys
import base64


def load_json(path):
    with open(path) as f:
        return json.load(f)


def gh_api(endpoint):
    """Call gh api and return parsed JSON. Returns None on failure."""
    try:
        result = subprocess.run(
            ["gh", "api", endpoint],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            if result.stderr.strip():
                print(f"gh api error: {result.stderr.strip()}", file=sys.stderr)
            return None
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        print(f"gh api timed out: {endpoint}", file=sys.stderr)
        return None
    except FileNotFoundError:
        print("gh CLI not found — install GitHub CLI: https://cli.github.com", file=sys.stderr)
        return None
    except json.JSONDecodeError:
        print(f"gh api returned invalid JSON for: {endpoint}", file=sys.stderr)
        return None


def fetch_file_content(org, repo, path):
    """Fetch a file's content from GitHub API. Returns decoded string or None."""
    data = gh_api(f"repos/{org}/{repo}/contents/{path}")
    if data and "content" in data:
        try:
            return base64.b64decode(data["content"]).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as e:
            print(f"WARNING: could not decode {path} from {org}/{repo}: {e}", file=sys.stderr)
            return None
    return None


def fetch_directory_listing(org, repo, path):
    """List files in a directory on GitHub. Returns list of {name, path, type}, or None on API error."""
    data = gh_api(f"repos/{org}/{repo}/contents/{path}")
    if isinstance(data, list):
        return [{"name": f["name"], "path": f["path"], "type": f["type"]} for f in data]
    if data is None:
        return None  # API error — distinct from empty directory
    return []  # Not a directory listing (e.g. single file response)


def fetch_directory_recursive(org, repo, remote_path, local_base):
    """Recursively fetch a directory tree from GitHub into local_base.

    Validates all file and directory names against an allowlist pattern.
    Files are written verbatim; directories are recursed into.
    """
    listing = fetch_directory_listing(org, repo, remote_path)
    if listing is None:
        print(f"WARNING: could not list {remote_path} for {org}/{repo} — API error", file=sys.stderr)
        return
    for entry in listing:
        name = entry["name"]
        # Validate name — alphanumeric start, then alphanumeric/dot/underscore/hyphen
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]*', name):
            print(f"WARNING: skipping invalid name in {remote_path}: {name}", file=sys.stderr)
            continue
        entry_remote = f"{remote_path}/{name}"
        entry_local = os.path.join(local_base, name)
        if entry["type"] == "dir":
            os.makedirs(entry_local, exist_ok=True)
            fetch_directory_recursive(org, repo, entry_remote, entry_local)
        elif entry["type"] == "file":
            content = fetch_file_content(org, repo, entry_remote)
            if content is None:
                print(f"WARNING: failed to fetch {entry_remote} from {org}/{repo}", file=sys.stderr)
                continue
            with open(entry_local, "w") as f:
                f.write(content)


def fetch_module(module_name, cache_dir, org="ProppyAI"):
    """Fetch a module's manifest and hooks from GitHub.

    Downloads to cache_dir/<module_name>/module.harness.json and hooks/.
    Returns (success: bool, message: str).
    """
    # Validate module name to prevent path traversal
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', module_name):
        return False, f"Invalid module name: {module_name}"

    repo = f"module-{module_name}"
    module_dir = os.path.join(cache_dir, module_name)
    os.makedirs(module_dir, exist_ok=True)

    # Fetch manifest
    manifest_content = fetch_file_content(org, repo, "module.harness.json")
    if manifest_content is None:
        return False, f"Failed to fetch manifest from {org}/{repo}"

    # Validate it's valid JSON and parse once
    try:
        manifest = json.loads(manifest_content)
    except json.JSONDecodeError as e:
        return False, f"Invalid JSON in manifest from {org}/{repo}: {e}"

    manifest_path = os.path.join(module_dir, "module.harness.json")
    with open(manifest_path, "w") as f:
        f.write(manifest_content)

    # Fetch hooks directory
    hooks_dir = os.path.join(module_dir, "hooks")
    os.makedirs(hooks_dir, exist_ok=True)

    hook_files = fetch_directory_listing(org, repo, "hooks")
    if hook_files is None:
        print(f"WARNING: could not list hooks for {org}/{repo} — API error", file=sys.stderr)
    for hf in (hook_files or []):
        if hf["type"] != "file":
            continue
        # Validate hook filename — allowlist of safe characters only
        if not re.fullmatch(r'[a-z0-9][a-z0-9._-]*', hf["name"]):
            print(f"WARNING: skipping invalid hook filename: {hf['name']}", file=sys.stderr)
            continue
        content = fetch_file_content(org, repo, f"hooks/{hf['name']}")
        if content is None:
            print(f"WARNING: failed to fetch hook {hf['name']} from {org}/{repo}", file=sys.stderr)
            continue
        hook_path = os.path.join(hooks_dir, hf["name"])
        with open(hook_path, "w") as f:
            f.write(content)
        os.chmod(hook_path, 0o755)

    # Fetch skills directory (recursive — prompt-engineering skills like SeedDance)
    skills_listing = fetch_directory_listing(org, repo, "skills")
    if skills_listing:  # None means API error, [] means no skills dir
        skills_dir = os.path.join(module_dir, "skills")
        os.makedirs(skills_dir, exist_ok=True)
        fetch_directory_recursive(org, repo, "skills", skills_dir)

    version = manifest.get("version", "?")

    return True, f"{module_name} ({version})"


def fetch_all_modules(deployment_path, cache_dir, org="ProppyAI"):
    """Fetch all modules declared in a deployment's harness.json.

    Returns list of (module_name, success, message).
    """
    config_path = os.path.join(deployment_path, "harness.json")
    if not os.path.isfile(config_path):
        return [("harness.json", False, f"harness.json not found at {deployment_path}")]

    try:
        config = load_json(config_path)
    except (json.JSONDecodeError, ValueError) as e:
        return [("harness.json", False, f"invalid JSON in harness.json: {e}")]
    if not isinstance(config, dict):
        return [("harness.json", False, "harness.json must contain a JSON object")]
    enabled = config.get("modules", {}).get("enabled", [])
    if not enabled:
        return []

    results = []
    for module_name in enabled:
        if not isinstance(module_name, str):
            results.append((str(module_name), False, "module name must be a string"))
            continue
        success, message = fetch_module(module_name, cache_dir, org)
        results.append((module_name, success, message))

    return results


def print_fetch_results(results):
    """Print fetch results in human-readable format."""
    for name, success, message in results:
        status = "\u2713" if success else "\u2717"
        print(f"  {status} {message}")


def main():
    if len(sys.argv) < 2:
        print("Usage:", file=sys.stderr)
        print("  module_fetch.py fetch <module-name> <cache-dir> [--org org]", file=sys.stderr)
        print("  module_fetch.py fetch-all <deployment-path> <cache-dir> [--org org]", file=sys.stderr)
        sys.exit(2)

    subcmd = sys.argv[1]
    org = "ProppyAI"

    # Parse --org flag
    if "--org" in sys.argv:
        idx = sys.argv.index("--org")
        if idx + 1 < len(sys.argv):
            org = sys.argv[idx + 1]

    # Validate org name
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]*', org):
        print(f"ERROR: invalid org name: {org}", file=sys.stderr)
        sys.exit(2)

    if subcmd == "fetch":
        if len(sys.argv) < 4:
            print("Usage: module_fetch.py fetch <module-name> <cache-dir> [--org org]", file=sys.stderr)
            sys.exit(2)
        module_name = sys.argv[2]
        cache_dir = sys.argv[3]

        print(f"HARNESS \u2014 Fetching module: {module_name}\n")
        success, message = fetch_module(module_name, cache_dir, org)
        status = "\u2713" if success else "\u2717"
        print(f"  {status} {message}")
        sys.exit(0 if success else 1)

    elif subcmd == "fetch-all":
        if len(sys.argv) < 4:
            print("Usage: module_fetch.py fetch-all <deployment-path> <cache-dir> [--org org]", file=sys.stderr)
            sys.exit(2)
        deployment_path = sys.argv[2]
        cache_dir = sys.argv[3]

        print(f"HARNESS \u2014 Fetching modules from {org}...\n")
        results = fetch_all_modules(deployment_path, cache_dir, org)
        print_fetch_results(results)

        failed = sum(1 for _, s, _ in results if not s)
        total = len(results)
        print(f"\n  {total - failed}/{total} modules fetched")
        sys.exit(1 if failed > 0 else 0)

    else:
        print(f"Unknown subcommand: {subcmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
