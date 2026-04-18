#!/usr/bin/env python3
"""Scan module manifests and build an agent type registry."""

import json
import os
import re
import sys


def load_json(path):
    with open(path) as f:
        return json.load(f)


def build_agent_registry(module_paths, harness_root):
    """Scan manifests, return list of {name, description, capabilities, module, module_path}.

    Auto-discovers from lib/ and examples/modules/ if no explicit paths given.
    """
    if not module_paths:
        module_paths = []
        for scan_dir in ["lib", os.path.join("examples", "modules"), os.path.join(".harness", "modules")]:
            full = os.path.join(harness_root, scan_dir)
            if os.path.isdir(full):
                for d in sorted(os.listdir(full)):
                    manifest = os.path.join(full, d, "module.harness.json")
                    if os.path.isfile(manifest):
                        module_paths.append(os.path.join(full, d))

    registry = []
    for module_path in module_paths:
        manifest_path = os.path.join(module_path, "module.harness.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            manifest = load_json(manifest_path)
        except json.JSONDecodeError:
            continue

        module_name = manifest.get("name", os.path.basename(module_path))
        for agent in manifest.get("agents", []):
            name = agent.get("name")
            desc = agent.get("description", "")
            caps = agent.get("capabilities", [])
            if not name:
                continue
            if not re.fullmatch(r'[a-z][a-z0-9-]*', name):
                print(f"WARNING: invalid agent name '{name}' in {module_name} — skipping", file=sys.stderr)
                continue
            registry.append({
                "name": name,
                "description": desc,
                "capabilities": caps,
                "module": module_name,
                "module_path": os.path.abspath(module_path),
            })

    # Check for duplicate agent names
    seen = {}
    for a in registry:
        if a["name"] in seen:
            print(f"WARNING: duplicate agent name '{a['name']}' in {a['module']} and {seen[a['name']]}", file=sys.stderr)
        seen[a["name"]] = a["module"]

    return registry


def list_agents(registry, json_output=False):
    """Print registry in human-readable or JSON format."""
    if json_output:
        output = [
            {"name": a["name"], "module": a["module"], "description": a["description"], "capabilities": a["capabilities"]}
            for a in registry
        ]
        print(json.dumps(output, indent=2))
        return

    if not registry:
        print("No agents registered.")
        return

    modules = set()
    print("HARNESS — Agent registry\n")
    for agent in registry:
        print(f"  {agent['name']} ({agent['module']})")
        print(f"    {agent['description']}")
        print(f"    Capabilities: {', '.join(agent['capabilities'])}")
        print()
        modules.add(agent["module"])

    print(f"  {len(registry)} agent(s) across {len(modules)} module(s)")


def main():
    if len(sys.argv) < 2:
        print("Usage: agent_registry.py <harness-root> [--json] [module-path ...]", file=sys.stderr)
        sys.exit(2)

    harness_root = sys.argv[1]
    json_output = "--json" in sys.argv
    module_paths = [a for a in sys.argv[2:] if a != "--json"]

    registry = build_agent_registry(module_paths, harness_root)
    list_agents(registry, json_output)


if __name__ == "__main__":
    main()
