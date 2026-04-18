#!/usr/bin/env python3
"""Scan module manifests for cron declarations, list and run jobs via hook executor."""

import json
import os
import re
import sys
from datetime import datetime


def load_json(path):
    with open(path) as f:
        return json.load(f)


def build_cron_registry(module_paths, harness_root):
    """Scan manifests, return list of {name, schedule, action, description, module, module_path}.

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
        for job in manifest.get("cron", []):
            name = job.get("name")
            schedule = job.get("schedule", "")
            action = job.get("action", "")
            desc = job.get("description", "")
            if not name or not action:
                continue
            if not re.fullmatch(r'[a-z][a-z0-9-]*', name):
                print(f"WARNING: invalid job name '{name}' in {module_name} — skipping", file=sys.stderr)
                continue
            if not re.fullmatch(r'[a-z][a-z0-9-]*', action):
                print(f"WARNING: invalid action name '{action}' in {module_name} — skipping", file=sys.stderr)
                continue
            registry.append({
                "name": name,
                "schedule": schedule,
                "action": action,
                "description": desc,
                "module": module_name,
                "module_path": os.path.abspath(module_path),
            })

    return registry


def cron_matches_now(schedule, now=None):
    """Check if a 5-field cron expression matches the current time.

    Supports: numbers, *, */N, and comma-separated values.
    """
    if not schedule or not schedule.strip():
        print(f"WARNING: empty cron schedule — skipping", file=sys.stderr)
        return False
    now = now or datetime.now()
    fields = schedule.strip().split()
    if len(fields) != 5:
        print(f"WARNING: malformed cron schedule '{schedule}' — skipping", file=sys.stderr)
        return False

    current = [now.minute, now.hour, now.day, now.month, now.isoweekday() % 7]
    # isoweekday: Mon=1..Sun=7, cron: Sun=0..Sat=6 — so %7 maps Sun(7)->0

    for field_val, cur_val in zip(fields, current):
        if not _cron_field_matches(field_val, cur_val):
            return False
    return True


def _cron_field_matches(field, value):
    """Check if a single cron field matches a value.

    Supports: *, N, N-M, N/S, */S, N-M/S, and comma-separated combinations.
    """
    if field == "*":
        return True
    # Handle comma-separated alternatives
    for part in field.split(","):
        step = None
        if "/" in part:
            part, step_str = part.split("/", 1)
            try:
                step = int(step_str)
            except ValueError:
                continue
        if part == "*":
            if step and step > 0:
                if value % step == 0:
                    return True
            else:
                return True
        elif "-" in part:
            try:
                lo, hi = [int(x) for x in part.split("-", 1)]
            except ValueError:
                continue
            if step and step > 0:
                if lo <= value <= hi and (value - lo) % step == 0:
                    return True
            else:
                if lo <= value <= hi:
                    return True
        else:
            try:
                if value == int(part):
                    return True
            except ValueError:
                continue
    return False


def filter_due_jobs(registry, now=None):
    """Return only jobs whose schedule matches the current time."""
    return [j for j in registry if cron_matches_now(j["schedule"], now)]


def list_cron_jobs(registry, json_output=False):
    """Print cron jobs in human-readable or JSON format."""
    if json_output:
        output = [
            {"name": j["name"], "module": j["module"], "schedule": j["schedule"],
             "action": j["action"], "description": j["description"]}
            for j in registry
        ]
        print(json.dumps(output, indent=2))
        return

    if not registry:
        print("No cron jobs declared.")
        return

    modules = set()
    print("HARNESS — Cron jobs\n")
    for job in registry:
        print(f"  {job['name']} ({job['module']})")
        print(f"    Schedule: {job['schedule']}")
        print(f"    Action: {job['action']}")
        print(f"    {job['description']}")
        print()
        modules.add(job["module"])

    print(f"  {len(registry)} job(s) across {len(modules)} module(s)")


def run_cron_job(job_name, registry, harness_root, config=None):
    """Execute a cron job via the hook executor. Returns hook execution result."""
    # Find the job
    job = None
    for j in registry:
        if j["name"] == job_name:
            job = j
            break

    if not job:
        print(f"ERROR: Cron job '{job_name}' not found")
        print("Available jobs:")
        for j in registry:
            print(f"  {j['name']} ({j['module']})")
        return None

    # Import hook executor from sibling module
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from hook_executor import fire_event
    except ImportError:
        print("ERROR: hook_executor not found — run harness-init to install it", file=sys.stderr)
        return None

    # Build a synthetic hook registry for this one action
    hook_registry = {
        "CronTrigger": [{
            "module": job["module"],
            "action": job["action"],
            "type": "post",
            "module_path": job["module_path"],
        }]
    }

    event_data = {
        "entity": "cron",
        "data": {
            "job": job["name"],
            "schedule": job["schedule"],
        }
    }

    print(f"HARNESS — Running cron job: {job['name']}\n")
    result = fire_event("CronTrigger", hook_registry, harness_root, event_data, config)

    if not result or "results" not in result:
        print("ERROR: hook executor returned unexpected result", file=sys.stderr)
        return None

    for r in result["results"]:
        status = "OK" if r["exit_code"] == 0 else "FAIL"
        print(f"  {r['module']}/{r['action']} ... {status} ({r['duration']}s)")
        if r["output"]:
            print(f"    output: \"{r['output']}\"")
        if r["error"]:
            print(f"    error: {r['error']}")
        print()

    print(f"  Job {'complete' if result['hooks_failed'] == 0 else 'failed'}")
    return result


def main():
    if len(sys.argv) < 2:
        print("Usage:", file=sys.stderr)
        print("  cron_manager.py list <harness-root> [--json] [module-path ...]", file=sys.stderr)
        print("  cron_manager.py run <job-name> <harness-root> [--module-path path ...] [--config path]", file=sys.stderr)
        sys.exit(2)

    subcmd = sys.argv[1]

    if subcmd == "list":
        if len(sys.argv) < 3:
            print("Usage: cron_manager.py list <harness-root> [--json] [module-path ...]", file=sys.stderr)
            sys.exit(2)
        harness_root = sys.argv[2]
        json_output = "--json" in sys.argv
        due_only = "--due-only" in sys.argv
        module_paths = [a for a in sys.argv[3:] if a not in ("--json", "--due-only")]
        registry = build_cron_registry(module_paths, harness_root)
        if due_only:
            registry = filter_due_jobs(registry)
        list_cron_jobs(registry, json_output)

    elif subcmd == "run":
        if len(sys.argv) < 4:
            print("Usage: cron_manager.py run <job-name> <harness-root> [--module-path path ...] [--config path]", file=sys.stderr)
            sys.exit(2)
        job_name = sys.argv[2]
        harness_root = sys.argv[3]

        module_paths = []
        config = None
        i = 4
        while i < len(sys.argv):
            if sys.argv[i] == "--module-path" and i + 1 < len(sys.argv):
                module_paths.append(sys.argv[i + 1])
                i += 2
            elif sys.argv[i] == "--config" and i + 1 < len(sys.argv):
                try:
                    config = load_json(sys.argv[i + 1])
                except (json.JSONDecodeError, FileNotFoundError) as e:
                    print(f"WARNING: could not load config '{sys.argv[i + 1]}': {e}", file=sys.stderr)
                i += 2
            else:
                module_paths.append(sys.argv[i])
                i += 1

        registry = build_cron_registry(module_paths, harness_root)
        result = run_cron_job(job_name, registry, harness_root, config)
        if result is None:
            sys.exit(1)
        sys.exit(1 if result["hooks_failed"] > 0 else 0)

    else:
        print(f"Unknown subcommand: {subcmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
