#!/usr/bin/env python3
"""Fire hook events — resolve actions, run shell commands, handle timeouts and outcomes."""

import json
import os
import re
import signal
import subprocess
import sys
import time


def resolve_action(module_path, action):
    """Find the executable for an action.

    Looks for <module_path>/hooks/<action>. Returns the path if found
    and executable, raises if not found or action name is invalid.
    """
    if not re.match(r"^[a-z][a-z0-9-]*$", action):
        raise ValueError(f"Invalid hook action name: {action!r} — must match ^[a-z][a-z0-9-]*$")
    hook_path = os.path.join(module_path, "hooks", action)
    # Belt-and-suspenders: verify resolved path stays inside hooks/
    hooks_dir = os.path.realpath(os.path.join(module_path, "hooks"))
    resolved = os.path.realpath(hook_path)
    if not resolved.startswith(hooks_dir + os.sep) and resolved != hooks_dir:
        raise ValueError(f"Hook action resolves outside hooks directory: {action!r}")
    if not os.path.isfile(hook_path) or not os.access(hook_path, os.X_OK):
        raise FileNotFoundError(f"Hook script not found or not executable: {hook_path}")
    return hook_path


def fire_event(event_name, registry, harness_root, event_data=None, config=None):
    """Fire an event, execute all registered hooks, return results dict.

    Returns: {
        event: str,
        hooks_run: int,
        hooks_failed: int,
        blocked: bool,
        results: [{module, action, outcome, output, duration, exit_code}]
    }
    """
    config = config or {}
    hooks_config = config.get("hooks", {})
    try:
        timeout = max(1, int(hooks_config.get("timeout", 30)))
    except (TypeError, ValueError):
        timeout = 30
    enabled = hooks_config.get("enabled", True)

    result = {
        "event": event_name,
        "hooks_run": 0,
        "hooks_failed": 0,
        "blocked": False,
        "results": [],
    }

    if not enabled:
        return result

    entries = registry.get(event_name, [])
    if not entries:
        return result

    # Build event payload
    payload = {
        "event": event_name,
        "type": "post",
        "module": "",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if event_data:
        payload.update(event_data)

    for entry in entries:
        # Re-assert trusted fields after user-data merge
        payload["event"] = event_name
        payload["type"] = entry["type"]
        payload["module"] = entry["module"]
        payload_json = json.dumps(payload)

        # Resolve the command — must be an explicit script, no shell fallback
        start = time.monotonic()
        try:
            cmd = resolve_action(entry["module_path"], entry["action"])
        except (ValueError, FileNotFoundError) as exc:
            duration = time.monotonic() - start
            hook_result = {
                "module": entry["module"],
                "action": entry["action"],
                "type": entry["type"],
                "outcome": "error",
                "output": "",
                "duration": round(duration, 2),
                "exit_code": -1,
                "error": str(exc),
            }
            result["results"].append(hook_result)
            result["hooks_run"] += 1
            result["hooks_failed"] += 1
            if entry["type"] == "pre":
                result["blocked"] = True
            if result["blocked"]:
                break
            continue

        try:
            proc = subprocess.Popen(
                [cmd],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=entry["module_path"],
                start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(input=payload_json, timeout=timeout)
            except subprocess.TimeoutExpired:
                # Kill the entire process group so grandchildren don't hold pipes open
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    proc.kill()
                try:
                    proc.communicate(timeout=5)  # reap; bounded in case pipes are held
                except subprocess.TimeoutExpired:
                    pass
                duration = time.monotonic() - start
                hook_result = {
                    "module": entry["module"],
                    "action": entry["action"],
                    "type": entry["type"],
                    "outcome": "timeout",
                    "output": "",
                    "duration": round(duration, 2),
                    "exit_code": -1,
                    "error": f"Hook timed out after {timeout}s",
                }
                result["results"].append(hook_result)
                result["hooks_run"] += 1
                result["hooks_failed"] += 1
                # Pre hooks fail closed on timeout
                if entry["type"] == "pre":
                    result["blocked"] = True
                if result["blocked"]:
                    break
                continue

            duration = time.monotonic() - start
            exit_code = proc.returncode

            # Parse JSON output
            outcome = "continue"
            output = stdout.strip()
            try:
                response = json.loads(output)
                outcome = response.get("outcome", "continue")
                output = response.get("output", output)
            except (json.JSONDecodeError, ValueError):
                pass

            hook_result = {
                "module": entry["module"],
                "action": entry["action"],
                "type": entry["type"],
                "outcome": outcome,
                "output": output,
                "duration": round(duration, 2),
                "exit_code": exit_code,
                "error": stderr.strip() if stderr.strip() else None,
            }

        except OSError as exc:
            duration = time.monotonic() - start
            hook_result = {
                "module": entry["module"],
                "action": entry["action"],
                "type": entry["type"],
                "outcome": "error",
                "output": "",
                "duration": round(duration, 2),
                "exit_code": -1,
                "error": str(exc),
            }

        result["results"].append(hook_result)
        result["hooks_run"] += 1

        if hook_result["exit_code"] != 0 or hook_result["outcome"] == "timeout":
            result["hooks_failed"] += 1
            # Pre hooks fail closed: any non-zero exit blocks the action.
            # This is deliberate — a crashing pre-authorization gate should
            # prevent the action, not silently allow it through.
            if entry["type"] == "pre":
                result["blocked"] = True

        if entry["type"] == "pre" and hook_result["outcome"] == "block":
            result["blocked"] = True

        # Stop executing further hooks if blocked
        if result["blocked"]:
            break

    return result


def print_results(result):
    """Print hook execution results in human-readable format."""
    print(f"HARNESS — Firing event: {result['event']}\n")

    for r in result["results"]:
        status = "OK" if r["exit_code"] == 0 and r["outcome"] != "timeout" else "FAIL"
        if r["outcome"] == "block" and r.get("type") == "pre":
            status = "BLOCKED"
        if r["outcome"] == "timeout":
            status = "TIMEOUT"

        print(f"  {r['module']}/{r['action']} ... {status} ({r['duration']}s)")
        if r["output"]:
            print(f"    output: \"{r['output']}\"")
        if r["error"]:
            print(f"    error: {r['error']}")
        print()

    blocked_str = ", BLOCKED" if result["blocked"] else ""
    print(f"  {result['hooks_run']} hook(s) executed, {result['hooks_failed']} failed{blocked_str}")


def main():
    if len(sys.argv) < 3:
        print("Usage: hook_executor.py <event-name> <harness-root> [--data '{}'] [--module-path path ...]", file=sys.stderr)
        sys.exit(2)

    event_name = sys.argv[1]
    if not re.match(r"^[A-Z][A-Za-z0-9]*$", event_name):
        print(f"ERROR: Invalid event name '{event_name}' — must be PascalCase (e.g. EstimateApproved)", file=sys.stderr)
        sys.exit(2)
    harness_root = sys.argv[2]

    # Parse optional args
    event_data = None
    module_paths = []
    config_path = None
    i = 3
    while i < len(sys.argv):
        if sys.argv[i] == "--data" and i + 1 < len(sys.argv):
            try:
                event_data = json.loads(sys.argv[i + 1])
            except json.JSONDecodeError:
                print(f"ERROR: Invalid JSON in --data: {sys.argv[i + 1]}", file=sys.stderr)
                sys.exit(2)
            if not isinstance(event_data, dict):
                print(f"ERROR: --data must be a JSON object, got {type(event_data).__name__}", file=sys.stderr)
                sys.exit(2)
            i += 2
        elif sys.argv[i] == "--config" and i + 1 < len(sys.argv):
            config_path = sys.argv[i + 1]
            i += 2
        elif sys.argv[i] == "--module-path" and i + 1 < len(sys.argv):
            module_paths.append(sys.argv[i + 1])
            i += 2
        else:
            if sys.argv[i].startswith("--"):
                print(f"ERROR: Unknown option {sys.argv[i]!r}", file=sys.stderr)
                sys.exit(2)
            module_paths.append(sys.argv[i])
            i += 1

    # Load config
    config = {}
    if config_path and os.path.isfile(config_path):
        try:
            with open(config_path) as f:
                config = json.load(f)
        except json.JSONDecodeError as e:
            print(f"WARNING: Could not parse config {config_path}: {e}", file=sys.stderr)

    # Build registry
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from hook_registry import build_registry

    registry = build_registry(module_paths, harness_root)
    result = fire_event(event_name, registry, harness_root, event_data, config)
    print_results(result)

    # Exit code: 0 success, 1 failure, 2 blocked
    if result["blocked"]:
        sys.exit(2)
    elif result["hooks_failed"] > 0:
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
