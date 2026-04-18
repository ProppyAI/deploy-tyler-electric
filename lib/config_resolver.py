#!/usr/bin/env python3
"""Resolve HARNESS config by merging layers: defaults → vertical → deployment → env."""

import copy
import json
import os
import re
import sys

SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

HARNESS_DEFAULTS = {
    "model": "claude-sonnet-4-6",
    "auto_pr": True,
    "hybrid_threshold": 0.7,
    "channels": {
        "rc": {"enabled": True},
        "discord": {"enabled": False},
        "slack": {"enabled": False},
        "imessage": {"enabled": False},
        "telegram": {"enabled": False},
    },
    "permissions": {
        "mode": "supervised",
        "rules": [],
    },
    "hooks": {
        "timeout": 30,
        "enabled": True,
    },
    "modules": {
        "enabled": [],
        "config": {},
    },
    "integrations": {},
}


def load_json(path):
    with open(path) as f:
        return json.load(f)


def deep_merge(base, override):
    """Deep merge two dicts. Override wins. None/null deletes keys.

    - Objects (dicts) are recursively merged
    - Arrays and scalars are replaced entirely
    - A value of None in override deletes the key from base
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _match_env_parts(parts, schema):
    """Greedy-match underscore-separated parts against known config keys.

    Tries longest key match first so HARNESS_AUTO_PR correctly maps to
    the "auto_pr" key rather than {"auto": {"pr": ...}}.
    Returns a list of matched key segments.
    """
    if not parts:
        return []
    # Try joining progressively more parts to match a key at this level
    for i in range(len(parts), 0, -1):
        candidate = "_".join(parts[:i])
        if isinstance(schema, dict) and candidate in schema:
            rest = parts[i:]
            if rest and isinstance(schema.get(candidate), dict):
                return [candidate] + _match_env_parts(rest, schema[candidate])
            return [candidate] + list(rest)
    # No match found — fall back to treating each part as a separate key
    return list(parts)


def get_env_overrides():
    """Collect HARNESS_* env vars, return dict with dot-path keys resolved.

    HARNESS_PERMISSIONS_MODE=unrestricted → {"permissions": {"mode": "unrestricted"}}
    HARNESS_HOOKS_TIMEOUT=60 → {"hooks": {"timeout": 60}}
    HARNESS_AUTO_PR=false → {"auto_pr": false}

    Key mapping uses greedy matching against HARNESS_DEFAULTS so that
    underscore-containing keys (auto_pr, hybrid_threshold) resolve correctly.

    Values are parsed as JSON first (so "false" becomes bool, "60" becomes int).
    If JSON parsing fails, kept as string.
    """
    overrides = {}
    prefix = "HARNESS_"
    for key, value in os.environ.items():
        if not key.startswith(prefix) or key == "HARNESS_LOCAL":
            continue
        raw_parts = key[len(prefix):].lower().split("_")
        if not raw_parts:
            continue

        # Greedy-match against defaults structure
        parts = _match_env_parts(raw_parts, HARNESS_DEFAULTS)

        # Parse value
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            parsed = value

        # Build nested dict
        current = overrides
        for part in parts[:-1]:
            current = current.setdefault(part, {})
        current[parts[-1]] = parsed

    return overrides


def get_vertical_config(vertical_name, harness_root):
    """Load vertical template and map it to config structure.

    - config_defaults → modules.config
    - entity_extensions → entity_extensions
    - modules.required + modules.recommended → modules.enabled (as default)
    """
    if not SAFE_NAME_RE.match(vertical_name):
        print(f"WARNING: Invalid vertical name '{vertical_name}'", file=sys.stderr)
        return None, None
    vertical_path = os.path.join(harness_root, "verticals", f"{vertical_name}.json")
    if not os.path.isfile(vertical_path):
        return None, vertical_path

    vertical = load_json(vertical_path)
    config = {}

    # Map config_defaults to modules.config
    config_defaults = vertical.get("config_defaults", {})
    if config_defaults:
        config["modules"] = {"config": config_defaults}

    # Map entity_extensions
    entity_ext = vertical.get("entity_extensions", {})
    if entity_ext:
        config["entity_extensions"] = entity_ext

    # Map module tiers to default enabled list
    modules = vertical.get("modules", {})
    default_enabled = modules.get("required", []) + modules.get("recommended", [])
    if default_enabled:
        config.setdefault("modules", {})["enabled"] = default_enabled

    return config, vertical_path


def resolve_config(deployment_path, harness_root):
    """Merge all layers, return resolved config dict."""
    layers = get_layer_sources(deployment_path, harness_root)
    result = {}
    for layer in layers:
        if layer["config"]:
            result = deep_merge(result, layer["config"])
    return result


def get_layer_sources(deployment_path, harness_root):
    """Return list of {layer: str, path: str|None, config: dict} for all layers."""
    layers = []

    # Layer 1: HARNESS defaults
    layers.append({
        "layer": "defaults",
        "path": None,
        "config": copy.deepcopy(HARNESS_DEFAULTS),
    })

    # Load deployment config to discover vertical name
    deployment_config_path = os.path.join(deployment_path, "harness.json")
    deployment_config = {}
    if os.path.isfile(deployment_config_path):
        try:
            deployment_config = load_json(deployment_config_path)
        except json.JSONDecodeError as e:
            print(f"WARNING: Invalid JSON in {deployment_config_path}: {e}", file=sys.stderr)

    # Layer 2: Vertical config (if deployment declares a vertical)
    vertical_name = deployment_config.get("vertical", "")
    if vertical_name:
        vertical_config, vertical_path = get_vertical_config(vertical_name, harness_root)
        if vertical_config is not None:
            layers.append({
                "layer": f"vertical: {vertical_name}",
                "path": vertical_path,
                "config": vertical_config,
            })

    # Layer 3: Deployment config
    if deployment_config:
        layers.append({
            "layer": "deployment",
            "path": deployment_config_path,
            "config": deployment_config,
        })

    # Layer 4: Environment overrides
    env_overrides = get_env_overrides()
    if env_overrides:
        layers.append({
            "layer": "environment",
            "path": None,
            "config": env_overrides,
        })

    return layers


def flatten_config(config, prefix=""):
    """Flatten a nested dict into dot-path key-value pairs."""
    items = []
    for key, value in sorted(config.items()):
        path = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
        if isinstance(value, dict) and value:
            items.extend(flatten_config(value, path))
        else:
            items.append((path, value))
    return items


def print_resolved(deployment_path, harness_root):
    """Print resolved config with layer attribution."""
    layers = get_layer_sources(deployment_path, harness_root)
    resolved = resolve_config(deployment_path, harness_root)

    client = resolved.get("client", os.path.basename(os.path.abspath(deployment_path)))
    print(f"HARNESS — Resolved config for {client}\n")

    # Build attribution: for each leaf value, find which layer set it last
    flat = flatten_config(resolved)
    for path, value in flat:
        # Find the last layer that contains this path
        source = "defaults"
        for layer in layers:
            obj = layer["config"]
            parts = path.split(".")
            found = True
            for part in parts:
                if isinstance(obj, dict) and part in obj:
                    obj = obj[part]
                else:
                    found = False
                    break
            if found:
                source = layer["layer"]

        # Format value
        if isinstance(value, str):
            val_str = f'"{value}"'
        elif isinstance(value, list):
            if len(value) <= 3:
                val_str = json.dumps(value)
            else:
                val_str = f"[{len(value)} items]"
        elif isinstance(value, bool):
            val_str = "true" if value else "false"
        else:
            val_str = str(value)

        print(f"  {path} = {val_str}  [{source}]")

    active = [l["layer"] for l in layers if l["config"]]
    print(f"\n  {len(active)} layer(s) active: {', '.join(active)}")


def main():
    if len(sys.argv) < 3:
        print("Usage: config_resolver.py <deployment-path> <harness-root>", file=sys.stderr)
        sys.exit(2)

    deployment_path = sys.argv[1]
    harness_root = sys.argv[2]

    print_resolved(deployment_path, harness_root)


if __name__ == "__main__":
    main()
