#!/usr/bin/env python3
"""Validate a HARNESS deployment config — cross-checks modules, integrations, permissions."""

import json
import os
import re
import sys

SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def load_json(path):
    with open(path) as f:
        return json.load(f)


def get_module_external_services(module_name, harness_root):
    """Find a module's externalServices declarations.

    Searches lib/ and examples/modules/ for a manifest with matching name.
    Returns list of {name, required} or empty list if not found.
    """
    for scan_dir in ["lib", os.path.join("examples", "modules")]:
        full = os.path.join(harness_root, scan_dir)
        if not os.path.isdir(full):
            continue
        for d in os.listdir(full):
            manifest_path = os.path.join(full, d, "module.harness.json")
            if not os.path.isfile(manifest_path):
                continue
            try:
                manifest = load_json(manifest_path)
            except json.JSONDecodeError:
                continue
            if manifest.get("name") == module_name:
                return manifest.get("externalServices", [])
    return []


def validate_deployment(deployment_path, harness_root):
    """Validate a deployment config. Returns (errors: list[str], warnings: list[str])."""
    errors = []
    warnings = []

    # Check harness.json exists and is valid JSON
    config_path = os.path.join(deployment_path, "harness.json")
    if not os.path.isfile(config_path):
        errors.append(f"harness.json not found in {deployment_path}")
        return errors, warnings

    try:
        config = load_json(config_path)
    except json.JSONDecodeError as e:
        errors.append(f"Invalid JSON in harness.json: {e}")
        return errors, warnings

    # Check vertical exists
    vertical = config.get("vertical", "")
    if vertical and not SAFE_NAME_RE.match(vertical):
        errors.append(f"Invalid vertical name '{vertical}' — must be alphanumeric, hyphens, or underscores only")
    elif vertical:
        vertical_path = os.path.join(harness_root, "verticals", f"{vertical}.json")
        if not os.path.isfile(vertical_path):
            errors.append(f"Vertical '{vertical}' not found at {vertical_path}")

    # Check modules
    modules_config = config.get("modules", {})
    enabled = modules_config.get("enabled", [])

    # Check integrations vs module externalServices
    integrations = config.get("integrations", {})
    seen_services = {}
    for module_name in enabled:
        services = get_module_external_services(module_name, harness_root)
        for svc in services:
            # Browser-type services don't need integration config — they connect to existing sessions
            if svc.get("type") == "browser":
                continue
            svc_name = svc.get("name", "")
            svc_required = svc.get("required", False)
            if svc_name and svc_name not in integrations:
                if svc_required:
                    errors.append(
                        f"Module '{module_name}' requires integration '{svc_name}' "
                        f"but it is not configured in integrations"
                    )
                else:
                    seen_services.setdefault(svc_name, []).append(module_name)

    # Build deduplicated warnings
    for svc_name, module_names in seen_services.items():
        warnings.append(
            f"{svc_name}: declared by {', '.join(module_names)} — not configured in integrations"
        )

    # Check permission rules
    perm_config = config.get("permissions", {})
    rules = perm_config.get("rules", [])
    for i, rule in enumerate(rules):
        if "module" not in rule or "access" not in rule or "behavior" not in rule:
            errors.append(f"Permission rule {i} missing required fields (module, access, behavior)")
        elif rule["behavior"] not in ("allow", "deny", "ask"):
            errors.append(f"Permission rule {i} has invalid behavior: {rule['behavior']}")

    # Check permission mode
    mode = perm_config.get("mode", "supervised")
    if mode not in ("supervised", "autonomous", "unrestricted"):
        errors.append(f"Invalid permissions mode: {mode}")

    return errors, warnings


def print_validation(deployment_path, harness_root):
    """Print validation results in human-readable format."""
    config_path = os.path.join(deployment_path, "harness.json")
    config = {}
    try:
        config = load_json(config_path)
    except (json.JSONDecodeError, FileNotFoundError):
        pass

    client = config.get("client", os.path.basename(os.path.abspath(deployment_path)))
    vertical = config.get("vertical", "")
    enabled = config.get("modules", {}).get("enabled", [])
    mode = config.get("permissions", {}).get("mode", "supervised")
    rules = config.get("permissions", {}).get("rules", [])

    print(f"HARNESS — Config validation for {client}\n")

    errors, warnings = validate_deployment(deployment_path, harness_root)

    if not errors:
        print(f"  ✓ harness.json is valid JSON")
        if vertical:
            vertical_path = os.path.join(harness_root, "verticals", f"{vertical}.json")
            if os.path.isfile(vertical_path):
                print(f"  ✓ Vertical \"{vertical}\" found")
        print(f"  ✓ {len(enabled)} module(s) enabled")
        if rules:
            print(f"  ✓ {len(rules)} permission rule(s) valid")
        else:
            print(f"  ✓ Permissions: {mode} mode, 0 rules")

    for w in warnings:
        print(f"  ! {w}")
    for e in errors:
        print(f"  ✗ {e}")

    if errors:
        print(f"\n  Config invalid ({len(errors)} error(s), {len(warnings)} warning(s))")
        return 1
    elif warnings:
        print(f"\n  Config valid ({len(warnings)} warning(s))")
        return 0
    else:
        print(f"\n  Config valid")
        return 0


def main():
    if len(sys.argv) < 3:
        print("Usage: config_validator.py <deployment-path> <harness-root>", file=sys.stderr)
        sys.exit(2)

    deployment_path = sys.argv[1]
    harness_root = sys.argv[2]

    exit_code = print_validation(deployment_path, harness_root)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
