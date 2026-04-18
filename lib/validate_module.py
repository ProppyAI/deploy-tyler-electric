#!/usr/bin/env python3
"""Validate a module.harness.json against the HARNESS module manifest schema and entity schemas."""

import json
import os
import re
import sys


def load_json(path):
    with open(path) as f:
        return json.load(f)


def get_entity_names(harness_root):
    """Return set of valid entity names from schemas/entities/."""
    entities_dir = os.path.join(harness_root, "schemas", "entities")
    names = set()
    if os.path.isdir(entities_dir):
        for fname in os.listdir(entities_dir):
            if fname.endswith(".schema.json"):
                names.add(fname.replace(".schema.json", ""))
    return names


def get_entity_properties(harness_root, entity_name):
    """Return set of property names for a base entity schema."""
    schema_path = os.path.join(harness_root, "schemas", "entities", f"{entity_name}.schema.json")
    if not os.path.isfile(schema_path):
        return set()
    schema = load_json(schema_path)
    return set(schema.get("properties", {}).keys())


def get_known_modules(harness_root, extra_paths=None):
    """Return set of known module names from core, examples, and extra paths."""
    names = set()
    # Core modules
    lib_dir = os.path.join(harness_root, "lib")
    if os.path.isdir(lib_dir):
        for d in os.listdir(lib_dir):
            manifest = os.path.join(lib_dir, d, "module.harness.json")
            if os.path.isfile(manifest):
                try:
                    m = load_json(manifest)
                    names.add(m.get("name", ""))
                except json.JSONDecodeError:
                    pass
    # Example modules
    examples_dir = os.path.join(harness_root, "examples", "modules")
    if os.path.isdir(examples_dir):
        for d in os.listdir(examples_dir):
            manifest = os.path.join(examples_dir, d, "module.harness.json")
            if os.path.isfile(manifest):
                try:
                    m = load_json(manifest)
                    names.add(m.get("name", ""))
                except json.JSONDecodeError:
                    pass
    # Extra paths
    if extra_paths:
        for p in extra_paths:
            manifest = os.path.join(p, "module.harness.json")
            if os.path.isfile(manifest):
                try:
                    m = load_json(manifest)
                    names.add(m.get("name", ""))
                except json.JSONDecodeError:
                    pass
    return names


def validate_module(module_path, harness_root):
    """Validate a module manifest. Returns (errors: list[str], warnings: list[str])."""
    errors = []
    warnings = []

    manifest_path = os.path.join(module_path, "module.harness.json")
    if not os.path.isfile(manifest_path):
        errors.append(f"module.harness.json not found in {module_path}")
        return errors, warnings

    try:
        manifest = load_json(manifest_path)
    except json.JSONDecodeError as e:
        errors.append(f"Invalid JSON in module.harness.json: {e}")
        return errors, warnings

    # Required fields
    for field in ["name", "version", "description", "category", "entities", "tools"]:
        if field not in manifest:
            errors.append(f"Missing required field: {field}")

    if errors:
        return errors, warnings

    # Name format
    name = manifest["name"]
    if not re.match(r"^[a-z][a-z0-9-]*$", name):
        errors.append(f"Invalid name '{name}' — must match ^[a-z][a-z0-9-]*$")

    # Version format
    version = manifest["version"]
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        errors.append(f"Invalid version '{version}' — must be semver (e.g., 1.0.0)")

    # Category
    valid_categories = ["ops", "frontend", "backend", "data", "content", "social",
                        "ml", "llm", "security", "analytics", "scientific", "comms"]
    if manifest["category"] not in valid_categories:
        errors.append(f"Invalid category '{manifest['category']}' — must be one of: {', '.join(valid_categories)}")

    # Entity references
    valid_entities = get_entity_names(harness_root)
    entities = manifest["entities"]

    for entity in entities.get("produces", []):
        if entity not in valid_entities:
            errors.append(f"Entity '{entity}' in produces is not a known entity (available: {', '.join(sorted(valid_entities))})")

    for entity in entities.get("consumes", []):
        if entity not in valid_entities:
            errors.append(f"Entity '{entity}' in consumes is not a known entity (available: {', '.join(sorted(valid_entities))})")

    # Extension conflicts
    for entity_name, ext in (entities.get("extends") or {}).items():
        if entity_name not in valid_entities:
            errors.append(f"Cannot extend unknown entity '{entity_name}'")
            continue
        base_props = get_entity_properties(harness_root, entity_name)
        ext_props = set(ext.get("properties", {}).keys())
        conflicts = base_props & ext_props
        if conflicts:
            errors.append(f"Extension field(s) {', '.join(sorted(conflicts))} on entity '{entity_name}' conflict with base schema")

    # Tool permissions format
    for tool in manifest.get("tools", []):
        tool_name = tool.get("name", "<unnamed>")
        for perm in tool.get("permissions", []):
            if not re.match(r"^[a-z][a-z0-9-]*:(read|write)$", perm):
                errors.append(f"Invalid permission '{perm}' on tool '{tool_name}' — must match entity:read|write")

    # Permission-entity cross-check: permissions must reference entities in produces, consumes, or extends
    all_module_entities = (
        set(entities.get("produces", []))
        | set(entities.get("consumes", []))
        | set((entities.get("extends") or {}).keys())
    )
    for tool in manifest.get("tools", []):
        tool_name = tool.get("name", "<unnamed>")
        for perm in tool.get("permissions", []):
            parts = perm.split(":")
            if len(parts) == 2:
                perm_entity = parts[0]
                if perm_entity not in all_module_entities:
                    errors.append(
                        f"Tool '{tool_name}' permission '{perm}' references entity "
                        f"'{perm_entity}' which is not in produces or consumes"
                    )

    # Hook entries validation
    for hook in manifest.get("hooks", []):
        hook_event = hook.get("event", "")
        hook_action = hook.get("action", "")
        hook_type = hook.get("type", "post")
        if not hook_event:
            errors.append("Hook entry missing 'event' field")
        elif not re.match(r"^[A-Z][A-Za-z0-9]*$", hook_event):
            errors.append(
                f"Hook event '{hook_event}' must be PascalCase (e.g. EstimateApproved)"
            )
        if not hook_action:
            errors.append(f"Hook entry for event '{hook_event}' missing 'action' field")
        elif not re.match(r"^[a-z][a-z0-9-]*$", hook_action):
            errors.append(
                f"Hook action '{hook_action}' for event '{hook_event}' is invalid "
                f"— must match ^[a-z][a-z0-9-]*$"
            )
        if hook_type not in ("pre", "post"):
            errors.append(f"Hook type '{hook_type}' for event '{hook_event}' must be 'pre' or 'post'")

    # Dependencies
    known_modules = get_known_modules(harness_root)
    for dep in manifest.get("dependencies", []):
        if dep not in known_modules and dep != name:
            warnings.append(f"Dependency '{dep}' not found in known modules (may exist in an external repo)")

    # Config format
    for key, val in manifest.get("config", {}).items():
        if not isinstance(val, dict):
            errors.append(f"Config '{key}' must be an object with type and default")
        elif "type" not in val or "default" not in val:
            errors.append(f"Config '{key}' must have both 'type' and 'default' fields")

    return errors, warnings


def main():
    if len(sys.argv) < 3:
        print("Usage: validate_module.py <module-path> <harness-root>", file=sys.stderr)
        sys.exit(2)

    module_path = sys.argv[1]
    harness_root = sys.argv[2]

    name = os.path.basename(module_path)
    print(f"HARNESS — Module validation: {name}")

    errors, warnings = validate_module(module_path, harness_root)

    if not errors and not warnings:
        print(f"  Module \"{name}\" is valid.")
        sys.exit(0)

    for w in warnings:
        print(f"  ! {w}")
    for e in errors:
        print(f"  ✗ {e}")

    if errors:
        print(f"\n  Module \"{name}\" has {len(errors)} error(s).")
        sys.exit(1)
    else:
        print(f"\n  Module \"{name}\" is valid ({len(warnings)} warning(s)).")
        sys.exit(0)


if __name__ == "__main__":
    main()
