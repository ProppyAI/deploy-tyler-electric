#!/usr/bin/env python3
"""Check module permissions against manifest declarations and deployment rules."""

import fnmatch
import json
import os
import sys


def load_json(path):
    with open(path) as f:
        return json.load(f)


def _extract_permissions(manifest):
    """Extract all unique permissions from a loaded manifest dict.

    Returns {permission_string: [tool_names_that_declare_it]}.
    """
    perms = {}
    for tool in manifest.get("tools", []):
        tool_name = tool.get("name", "<unnamed>")
        for perm in tool.get("permissions", []):
            perms.setdefault(perm, []).append(tool_name)
    return perms


def get_module_declared_permissions(manifest_path):
    """Extract all unique permissions from a module manifest file.

    Returns {permission_string: [tool_names_that_declare_it]}.
    """
    return _extract_permissions(load_json(manifest_path))


def check_permission(module_name, access, manifest_path, harness_root, config=None, declared_cache=None):
    """Check a single permission. Returns ('allow'|'deny'|'ask', reason_string).

    Pass declared_cache to avoid re-reading the manifest on every call.
    """
    config = config or {}
    perm_config = config.get("permissions", {})
    mode = perm_config.get("mode", "supervised")
    rules = perm_config.get("rules", [])

    # Step 1: Check explicit rules (first match wins)
    for rule in rules:
        rule_module = rule.get("module", "")
        rule_access = rule.get("access", "")
        rule_behavior = rule.get("behavior", "")

        if fnmatch.fnmatch(module_name, rule_module) and fnmatch.fnmatch(access, rule_access):
            if rule_behavior not in ("allow", "deny", "ask"):
                continue  # skip rules with invalid behavior
            return rule_behavior, f"rule: {rule_module} / {rule_access} -> {rule_behavior}"

    # Step 2: Check declared permissions
    declared = declared_cache if declared_cache is not None else get_module_declared_permissions(manifest_path)
    if access in declared:
        tools = ", ".join(declared[access])
        return "allow", f"declared in tool: {tools}"

    # Step 3: Apply mode-based default
    if mode == "supervised":
        return "deny", f"not declared, mode: {mode}"
    elif mode == "autonomous":
        return "ask", f"not declared, mode: {mode}"
    elif mode == "unrestricted":
        return "allow", f"mode: {mode}"
    else:
        return "deny", f"unknown mode: {mode}"


def audit_module(module_path, harness_root, config=None):
    """Check all permissions a module requests. Returns list of {access, behavior, reason, tools}."""
    manifest_path = os.path.join(module_path, "module.harness.json")
    if not os.path.isfile(manifest_path):
        return []

    manifest = load_json(manifest_path)
    module_name = manifest.get("name", os.path.basename(module_path))
    declared = _extract_permissions(manifest)

    results = []
    for access in sorted(declared.keys()):
        behavior, reason = check_permission(module_name, access, manifest_path, harness_root, config,
                                            declared_cache=declared)
        results.append({
            "access": access,
            "behavior": behavior,
            "reason": reason,
            "tools": declared[access],
        })

    return results


def print_check(module_name, access, behavior, reason):
    """Print a single permission check result."""
    print("HARNESS — Permission check\n")
    print(f"  Module:  {module_name}")
    print(f"  Access:  {access}")
    print(f"  Result:  {behavior.upper()} ({reason})")


def print_audit(module_name, results):
    """Print a full permission audit."""
    print(f"HARNESS — Permission audit: {module_name}\n")

    if not results:
        print("  No permissions declared.")
        return

    # Calculate column width
    max_access = max(len(r["access"]) for r in results)

    counts = {"allow": 0, "deny": 0, "ask": 0}
    for r in results:
        pad = " " * (max_access - len(r["access"]) + 2)
        print(f"  {r['access']}{pad}{r['behavior'].upper()}  ({r['reason']})")
        counts[r["behavior"]] += 1

    total = len(results)
    parts = []
    if counts["allow"]:
        parts.append(f"{counts['allow']} allowed")
    if counts["deny"]:
        parts.append(f"{counts['deny']} denied")
    if counts["ask"]:
        parts.append(f"{counts['ask']} ask")
    print(f"\n  {total} permission(s), {', '.join(parts)}")


def main():
    if len(sys.argv) < 2:
        print("Usage:", file=sys.stderr)
        print("  permission_checker.py check <module-name> <entity:access> <manifest-path> <harness-root> [--config path]", file=sys.stderr)
        print("  permission_checker.py audit <module-path> <harness-root> [--config path]", file=sys.stderr)
        sys.exit(2)

    subcmd = sys.argv[1]

    if subcmd == "check":
        if len(sys.argv) < 6:
            print("Usage: permission_checker.py check <module-name> <entity:access> <manifest-path> <harness-root> [--config path]", file=sys.stderr)
            sys.exit(2)

        module_name = sys.argv[2]
        access = sys.argv[3]
        manifest_path = sys.argv[4]
        harness_root = sys.argv[5]

        config = {}
        if "--config" in sys.argv:
            idx = sys.argv.index("--config")
            if idx + 1 < len(sys.argv):
                try:
                    config = load_json(sys.argv[idx + 1])
                except (json.JSONDecodeError, FileNotFoundError) as e:
                    print(f"WARNING: Could not load config {sys.argv[idx + 1]}: {e}", file=sys.stderr)

        behavior, reason = check_permission(module_name, access, manifest_path, harness_root, config)
        print_check(module_name, access, behavior, reason)

        if behavior == "allow":
            sys.exit(0)
        elif behavior == "deny":
            sys.exit(1)
        else:
            sys.exit(2)

    elif subcmd == "audit":
        if len(sys.argv) < 4:
            print("Usage: permission_checker.py audit <module-path> <harness-root> [--config path]", file=sys.stderr)
            sys.exit(2)

        module_path = sys.argv[2]
        harness_root = sys.argv[3]

        config = {}
        if "--config" in sys.argv:
            idx = sys.argv.index("--config")
            if idx + 1 < len(sys.argv):
                try:
                    config = load_json(sys.argv[idx + 1])
                except (json.JSONDecodeError, FileNotFoundError) as e:
                    print(f"WARNING: Could not load config {sys.argv[idx + 1]}: {e}", file=sys.stderr)

        manifest_path = os.path.join(module_path, "module.harness.json")
        try:
            manifest = load_json(manifest_path)
        except FileNotFoundError:
            print(f"ERROR: Manifest not found: {manifest_path}", file=sys.stderr)
            sys.exit(1)
        except json.JSONDecodeError as e:
            print(f"ERROR: Invalid JSON in {manifest_path}: {e}", file=sys.stderr)
            sys.exit(1)
        module_name = manifest.get("name", os.path.basename(module_path))

        results = audit_module(module_path, harness_root, config)
        print_audit(module_name, results)

    else:
        print(f"Unknown subcommand: {subcmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
