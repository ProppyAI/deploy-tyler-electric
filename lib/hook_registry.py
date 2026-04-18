#!/usr/bin/env python3
"""Scan module manifests and build an event-to-action hook registry."""

import json
import os
import sys


def load_json(path):
    with open(path) as f:
        return json.load(f)


def build_registry(module_paths, harness_root):
    """Scan manifests, return {event: [{module, action, type, module_path}]}.

    Also scans core modules in lib/ and examples in examples/modules/
    if no explicit paths are given.
    """
    if not module_paths:
        module_paths = []
        # Auto-discover from lib/ and examples/
        for scan_dir in ["lib", os.path.join("examples", "modules"), os.path.join(".harness", "modules")]:
            full = os.path.join(harness_root, scan_dir)
            if os.path.isdir(full):
                for d in sorted(os.listdir(full)):
                    manifest = os.path.join(full, d, "module.harness.json")
                    if os.path.isfile(manifest):
                        module_paths.append(os.path.join(full, d))

    registry = {}
    for module_path in module_paths:
        manifest_path = os.path.join(module_path, "module.harness.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            manifest = load_json(manifest_path)
        except json.JSONDecodeError as exc:
            print(f"WARNING: Skipping {manifest_path}: {exc}", file=sys.stderr)
            continue

        module_name = manifest.get("name", os.path.basename(module_path))
        for hook in manifest.get("hooks", []):
            event = hook.get("event")
            action = hook.get("action")
            hook_type = hook.get("type", "post")
            if not event or not action:
                continue
            if hook_type not in ("pre", "post"):
                hook_type = "post"
            registry.setdefault(event, []).append({
                "module": module_name,
                "action": action,
                "type": hook_type,
                "module_path": os.path.abspath(module_path),
            })

    return registry


def list_hooks(registry):
    """Print the registry in human-readable format."""
    if not registry:
        print("No hooks registered.")
        return

    total = 0
    modules = set()
    print("HARNESS — Hook registry\n")
    for event in sorted(registry.keys()):
        print(f"  {event}")
        for entry in registry[event]:
            print(f"    {entry['module']} -> {entry['action']} ({entry['type']})")
            modules.add(entry["module"])
            total += 1
        print()

    print(f"  {total} hook(s) across {len(modules)} module(s)")


def main():
    if len(sys.argv) < 2:
        print("Usage: hook_registry.py <harness-root> [module-path ...]", file=sys.stderr)
        sys.exit(2)

    harness_root = sys.argv[1]
    module_paths = sys.argv[2:] if len(sys.argv) > 2 else []

    registry = build_registry(module_paths, harness_root)
    list_hooks(registry)


if __name__ == "__main__":
    main()
