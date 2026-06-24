#!/usr/bin/env python3
"""Idempotently add a PreToolUse hook entry to a Claude Code settings.json.

Usage: merge-pretooluse-hook.py <settings_path> <command_path>

Inserts {matcher:"Edit|Write|Bash", hooks:[{type:"command",
command:<command_path>, timeout:5}]} under hooks.PreToolUse. Re-runs are no-ops
(keyed on command_path). Existing hooks are preserved. Creates the file and the
hooks/PreToolUse structure if absent.
"""
import json
import os
import sys
import tempfile

MATCHER = "Edit|Write|Bash"
TIMEOUT = 5


def main() -> int:
    if len(sys.argv) != 3:
        sys.stderr.write("usage: merge-pretooluse-hook.py <settings> <command>\n")
        return 2
    settings_path, command = sys.argv[1], sys.argv[2]

    data = {}
    if os.path.exists(settings_path):
        with open(settings_path) as fh:
            try:
                data = json.load(fh)
            except json.JSONDecodeError:
                sys.stderr.write(f"refusing to overwrite unparseable {settings_path}\n")
                return 1
    if not isinstance(data, dict):
        sys.stderr.write("settings root is not an object\n")
        return 1

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        sys.stderr.write("settings.hooks is not an object\n")
        return 1
    pre = hooks.setdefault("PreToolUse", [])
    if not isinstance(pre, list):
        sys.stderr.write("settings.hooks.PreToolUse is not an array\n")
        return 1

    already = any(
        h.get("command") == command
        for entry in pre if isinstance(entry, dict)
        for h in entry.get("hooks", []) if isinstance(h, dict)
    )
    if not already:
        pre.append({
            "matcher": MATCHER,
            "hooks": [{"type": "command", "command": command, "timeout": TIMEOUT}],
        })
        dirn = os.path.dirname(settings_path) or "."
        os.makedirs(dirn, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dirn, prefix=".settings-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, settings_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        print(f"added PreToolUse hook -> {command}")
    else:
        print("PreToolUse hook already present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
