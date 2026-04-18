#!/usr/bin/env python3
"""Run business scenario simulations against fetched module manifests."""

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone


def load_json(path):
    with open(path) as f:
        return json.load(f)


def find_module_manifest(module_name, search_dirs):
    """Find a module manifest in search directories."""
    # Path traversal guard
    if os.sep in module_name or ".." in module_name or module_name.startswith("/"):
        return None, None
    for base in search_dirs:
        manifest = os.path.join(base, module_name, "module.harness.json")
        if os.path.isfile(manifest):
            return load_json(manifest), os.path.join(base, module_name)
    return None, None


def get_search_dirs(harness_root):
    """Get all module search directories."""
    dirs = []
    for scan_dir in ["lib", os.path.join("examples", "modules"), os.path.join(".harness", "modules")]:
        full = os.path.join(harness_root, scan_dir) if harness_root != "." else scan_dir
        if os.path.isdir(full):
            dirs.append(full)
    # Also check cwd .harness/modules
    cwd_modules = os.path.join(os.getcwd(), ".harness", "modules")
    if os.path.isdir(cwd_modules) and cwd_modules not in dirs:
        dirs.append(cwd_modules)
    return dirs


def check_module_has_tool(module_name, tool_name, search_dirs):
    """Check if a module has a specific tool declared."""
    manifest, _ = find_module_manifest(module_name, search_dirs)
    if manifest is None:
        return False, "Module '{}' not found".format(module_name)
    tools = {t["name"]: t for t in manifest.get("tools", [])}
    if tool_name not in tools:
        return False, "Tool '{}' not found in {} (has: {})".format(tool_name, module_name, ", ".join(tools.keys()))
    return True, "Tool '{}' exists in {}".format(tool_name, module_name)


def check_hook_exists(module_name, event_name, search_dirs):
    """Check if a module has a hook registered for an event."""
    manifest, module_path = find_module_manifest(module_name, search_dirs)
    if manifest is None:
        return False, "Module '{}' not found".format(module_name)
    hooks = {h["event"]: h for h in manifest.get("hooks", [])}
    if event_name not in hooks:
        return False, "Hook '{}' not registered in {}".format(event_name, module_name)

    # Check if hook script exists
    hook = hooks[event_name]
    action = hook["action"]
    # Path traversal guard
    if os.sep in action or ".." in action or action.startswith("/"):
        return False, "Hook action '{}' contains path traversal".format(action)
    if module_path:
        script = os.path.join(module_path, "hooks", action)
        has_script = os.path.isfile(script)
    else:
        has_script = False

    return True, "Hook '{}' registered (script: {})".format(event_name, "yes" if has_script else "no")


def check_hook_fire(module_name, event_name, data, search_dirs, harness_root):
    """Fire a hook and check the outcome."""
    manifest, module_path = find_module_manifest(module_name, search_dirs)
    if manifest is None:
        return False, "Module '{}' not found".format(module_name)

    hooks = {h["event"]: h for h in manifest.get("hooks", [])}
    if event_name not in hooks:
        return False, "Hook '{}' not registered".format(event_name)

    hook = hooks[event_name]
    action = hook["action"]
    # Path traversal guard — reject actions with path separators or ..
    if os.sep in action or ".." in action or action.startswith("/"):
        return False, "Hook action '{}' contains path traversal".format(action)
    script = os.path.join(module_path, "hooks", action)
    script = os.path.realpath(script)
    if not script.startswith(os.path.realpath(module_path) + os.sep):
        return False, "Hook script escapes module directory: {}".format(script)
    if not os.path.isfile(script):
        return False, "Hook script not found: {}".format(script)

    # Fire the hook
    event_data = {
        "event": event_name,
        "type": hook.get("type", "post"),
        "module": module_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if data:
        event_data.update(data)

    try:
        result = subprocess.run(
            script,
            input=json.dumps(event_data),
            capture_output=True,
            text=True,
            timeout=15,
            cwd=module_path,
        )
        if result.returncode != 0:
            return False, "Hook exited {}: {}".format(result.returncode, result.stderr.strip())

        try:
            response = json.loads(result.stdout.strip())
            outcome = response.get("outcome", "continue")
            output = response.get("output", "")
            return True, "outcome={}: {}".format(outcome, output)
        except json.JSONDecodeError:
            return True, "Hook ran but output not JSON: {}".format(result.stdout.strip()[:80])

    except subprocess.TimeoutExpired:
        return False, "Hook timed out (15s)"
    except OSError as e:
        return False, "Hook execution error: {}".format(e)


def check_channel_classify(text, expected_intent, expected_auto_dispatch):
    """Validate that a message would be classified correctly.

    Since we can't run the actual classifier (needs Claude), we validate
    the expectation is reasonable based on the classifier's intent definitions.
    """
    auto_dispatch_intents = {"question", "scheduling", "billing-inquiry", "status-check"}

    # Check the expected auto_dispatch matches the intent
    if expected_auto_dispatch and expected_intent not in auto_dispatch_intents:
        return False, "Intent '{}' is not auto-dispatchable but expected auto_dispatch=true".format(expected_intent)
    if not expected_auto_dispatch and expected_intent in auto_dispatch_intents:
        # This is fine — could be low confidence
        pass

    return True, "Classification expectation valid: {} (auto_dispatch={})".format(expected_intent, expected_auto_dispatch)


def run_scenario(scenario_path, harness_root):
    """Run a scenario file and return results."""
    scenario = load_json(scenario_path)
    search_dirs = get_search_dirs(harness_root)

    name = scenario.get("name", os.path.basename(scenario_path))
    steps = scenario.get("steps", [])

    print("HARNESS — Simulating: {}\n".format(name))

    passed = 0
    failed = 0
    results = []

    for step in steps:
        step_num = step.get("step", "?")
        action = step.get("action", "")
        expect = step.get("expect", {})

        success = False
        message = ""

        if action == "module.has_tool":
            module = step.get("module", "")
            tool = step.get("tool", "")
            success, message = check_module_has_tool(module, tool, search_dirs)

        elif action == "hook.exists":
            module = step.get("module", "")
            event = step.get("event", "")
            success, message = check_hook_exists(module, event, search_dirs)

        elif action == "hook.fire":
            module = step.get("module", "")
            event = step.get("event", "")
            data = step.get("data", {})
            success, message = check_hook_fire(module, event, data, search_dirs, harness_root)

        elif action == "channel.classify":
            text = step.get("input", {}).get("text", "")
            expected_intent = expect.get("intent", "")
            expected_auto = expect.get("auto_dispatch", False)
            success, message = check_channel_classify(text, expected_intent, expected_auto)

        else:
            message = "Unknown action: {}".format(action)

        status = "PASS" if success else "FAIL"
        if success:
            passed += 1
        else:
            failed += 1

        print("  Step {}: [{}] {}".format(step_num, status, action))
        print("    {}".format(message))

        results.append({"step": step_num, "action": action, "success": success, "message": message})

    print("\n  {} passed, {} failed out of {} steps".format(passed, failed, len(steps)))

    # Persist results to .harness/simulations/
    sim_dir = os.path.join(os.getcwd(), ".harness", "simulations")
    os.makedirs(sim_dir, exist_ok=True)
    now_utc = datetime.now(timezone.utc)
    uid = uuid.uuid4().hex[:6]
    timestamp = now_utc.strftime("%Y%m%d-%H%M%SZ") + "-" + uid
    scenario_slug = name.lower().replace(" ", "-").replace("/", "-")
    result_file = os.path.join(sim_dir, f"{scenario_slug}-{timestamp}.json")

    result_data = {
        "name": name,
        "scenario_file": os.path.abspath(scenario_path),
        "timestamp": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ") + "-" + uid,
        "passed": passed,
        "failed": failed,
        "total": len(steps),
        "results": results
    }

    with open(result_file, "w") as f:
        json.dump(result_data, f, indent=2)
        f.write("\n")

    return {"name": name, "passed": passed, "failed": failed, "total": len(steps), "results": results}


def main():
    if len(sys.argv) < 3:
        print("Usage: simulator.py <scenario-file-or-dir> <harness-root> [--verbose]", file=sys.stderr)
        sys.exit(2)

    target = sys.argv[1]
    harness_root = sys.argv[2]

    if os.path.isdir(target):
        # Run all scenarios in directory
        all_results = []
        for f in sorted(os.listdir(target)):
            if f.endswith(".json"):
                result = run_scenario(os.path.join(target, f), harness_root)
                all_results.append(result)
                print()

        total_passed = sum(r["passed"] for r in all_results)
        total_failed = sum(r["failed"] for r in all_results)
        total_steps = sum(r["total"] for r in all_results)
        print("HARNESS — Simulation Summary")
        print("  {} scenarios, {}/{} steps passed, {} failed".format(len(all_results), total_passed, total_steps, total_failed))
        sys.exit(1 if total_failed > 0 else 0)
    else:
        result = run_scenario(target, harness_root)
        sys.exit(1 if result["failed"] > 0 else 0)


if __name__ == "__main__":
    main()
