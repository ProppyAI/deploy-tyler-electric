#!/usr/bin/env python3
"""Run full validation suite against a deployment in one shot."""

import json
import os
import subprocess
import sys


def load_json(path):
    with open(path) as f:
        return json.load(f)


def run_check(name, cmd):
    """Run a check command, return (success, output)."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30
        )
        return result.returncode == 0, result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)


def validate_all(deployment_path, harness_root):
    """Run all validation checks. Returns (passed, failed, results)."""
    results = []
    passed = 0
    failed = 0

    config_path = os.path.join(deployment_path, "harness.json")
    if not os.path.isfile(config_path):
        return 0, 1, [{"check": "harness.json exists", "success": False, "detail": "not found"}]

    config = load_json(config_path)
    client = config.get("client", os.path.basename(os.path.abspath(deployment_path)))

    print("HARNESS — Full validation for {}\n".format(client))

    # 1. Config validation
    success, output = run_check("config validate", [
        sys.executable, os.path.join(harness_root, "lib", "config_validator.py"),
        deployment_path, harness_root
    ])
    status = "+" if success else "x"
    print("  {} Config validation".format(status))
    results.append({"check": "config", "success": success})
    if success:
        passed += 1
    else:
        failed += 1

    # 2. Module fetch check
    modules_dir = os.path.join(deployment_path, ".harness", "modules")
    enabled = config.get("modules", {}).get("enabled", [])
    fetched = []
    if os.path.isdir(modules_dir):
        fetched = [d for d in os.listdir(modules_dir)
                   if os.path.isfile(os.path.join(modules_dir, d, "module.harness.json"))]

    missing = set(enabled) - set(fetched)
    if not missing:
        print("  + All {} modules fetched".format(len(enabled)))
        passed += 1
    else:
        print("  x Missing modules: {}".format(", ".join(missing)))
        failed += 1
    results.append({"check": "modules_fetched", "success": not missing, "missing": list(missing)})

    # 3. Module validation (each fetched module)
    all_valid = True
    for mod_name in fetched:
        mod_path = os.path.join(modules_dir, mod_name)
        success, output = run_check("validate {}".format(mod_name), [
            sys.executable, os.path.join(harness_root, "lib", "validate_module.py"),
            mod_path, harness_root
        ])
        if not success:
            print("  x Module '{}' validation failed".format(mod_name))
            all_valid = False
    if all_valid and fetched:
        print("  + All {} modules pass validation".format(len(fetched)))
        passed += 1
    elif not fetched:
        print("  ! No modules to validate")
    else:
        failed += 1
    results.append({"check": "module_validation", "success": all_valid})

    # 4. Dependency graph
    if fetched:
        mod_paths = [os.path.join(modules_dir, d) for d in fetched]
        success, output = run_check("deps", [
            sys.executable, os.path.join(harness_root, "lib", "module_deps.py")
        ] + mod_paths)
        if not success:
            print("  x Dependency check crashed: {}".format(output.strip()[:100]))
            failed += 1
            results.append({"check": "deps", "success": False})
        else:
            # Check for unresolved (excluding 'job' which is the entry point)
            has_unresolved = "Unresolved:" in output
            if has_unresolved:
                # Parse the actual unresolved entities from "Unresolved: a, b, c (no module...)"
                unresolved_match = [l for l in output.split("\n") if "Unresolved:" in l]
                unresolved_str = unresolved_match[0].split("Unresolved:")[1].split("(")[0].strip() if unresolved_match else ""
                unresolved_entities = [e.strip() for e in unresolved_str.split(",") if e.strip()]
                unresolved_non_job = [e for e in unresolved_entities if e != "job"]
                unresolved_only_job = len(unresolved_non_job) == 0
            else:
                unresolved_only_job = True
            if not has_unresolved or unresolved_only_job:
                print("  + Dependency graph resolved (job is entry point)")
                passed += 1
            else:
                unresolved_line = [l for l in output.split("\n") if "Unresolved:" in l]
                print("  x {}".format(unresolved_line[0].strip() if unresolved_line else "Unresolved entities"))
                failed += 1
            results.append({"check": "deps", "success": not has_unresolved or unresolved_only_job})
    else:
        print("  - Dependency check skipped (no modules fetched)")
        results.append({"check": "deps", "success": True, "skipped": True})

    # 5. Hook count
    success, output = run_check("hooks", [
        sys.executable, os.path.join(harness_root, "lib", "hook_registry.py"),
        harness_root
    ] + ([os.path.join(modules_dir, d) for d in fetched] if fetched else []))
    hook_line = [l for l in output.split("\n") if "hook(s)" in l]
    if success and hook_line:
        print("  + {}".format(hook_line[-1].strip()))
        passed += 1
    elif not success:
        print("  x Hook registry failed: {}".format(output.strip()[:100]))
        failed += 1
    else:
        print("  + No hooks found (registry OK)")
        passed += 1
    results.append({"check": "hooks", "success": success})

    # 6. Agent count
    success, output = run_check("agents", [
        sys.executable, os.path.join(harness_root, "lib", "agent_registry.py"),
        harness_root
    ] + ([os.path.join(modules_dir, d) for d in fetched] if fetched else []))
    agent_line = [l for l in output.split("\n") if "agent(s)" in l]
    if success and agent_line:
        print("  + {}".format(agent_line[-1].strip()))
        passed += 1
    elif not success:
        print("  x Agent registry failed: {}".format(output.strip()[:100]))
        failed += 1
    else:
        print("  + No agents found (registry OK)")
        passed += 1
    results.append({"check": "agents", "success": success})

    # 7. Cron count
    success, output = run_check("cron", [
        sys.executable, os.path.join(harness_root, "lib", "cron_manager.py"),
        "list", harness_root
    ] + ([os.path.join(modules_dir, d) for d in fetched] if fetched else []))
    cron_line = [l for l in output.split("\n") if "job(s)" in l]
    if success and cron_line:
        print("  + {}".format(cron_line[-1].strip()))
        passed += 1
    elif not success:
        print("  x Cron manager failed: {}".format(output.strip()[:100]))
        failed += 1
    else:
        print("  + No cron jobs found (manager OK)")
        passed += 1
    results.append({"check": "cron", "success": success})

    print("\n  {} passed, {} failed".format(passed, failed))
    return passed, failed, results


def main():
    if len(sys.argv) < 3:
        print("Usage: full_validator.py <deployment-path> <harness-root>", file=sys.stderr)
        sys.exit(2)

    deployment_path = sys.argv[1]
    harness_root = sys.argv[2]

    passed, failed, _ = validate_all(deployment_path, harness_root)
    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
