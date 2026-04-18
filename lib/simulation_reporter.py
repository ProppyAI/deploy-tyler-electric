#!/usr/bin/env python3
"""Read simulation results, classify failures, report trends."""

import json
import os
import sys
from collections import defaultdict


def load_json(path):
    with open(path) as f:
        return json.load(f)


def classify_failure(step):
    """Classify a failed step into a gap category."""
    action = step.get("action", "")
    message = step.get("message", "")

    if action == "module.has_tool":
        if "Tool" in message and "not found" in message:
            return "tool-gap"
        if "not found" in message and "Module" in message:
            return "module-gap"
        return "module-gap"
    elif action == "hook.exists" or action == "hook.fire":
        return "hook-gap"
    elif action == "channel.classify":
        return "classify-gap"
    else:
        return "unknown-gap"


def load_all_results(sim_dir):
    """Load all simulation result files from a directory."""
    results = []
    if not os.path.isdir(sim_dir):
        return results
    for fname in os.listdir(sim_dir):
        if not fname.endswith(".json"):
            continue
        try:
            result = load_json(os.path.join(sim_dir, fname))
            results.append(result)
        except (json.JSONDecodeError, IOError):
            continue
    results.sort(key=lambda r: r.get("timestamp", ""))
    return results


def generate_report(sim_dir):
    """Generate a summary report from all simulation results."""
    results = load_all_results(sim_dir)

    if not results:
        print("HARNESS — Simulation Report\n")
        print("  No simulation results found.")
        print(f"  Run: harness simulate scenarios/ to generate results.")
        return

    print("HARNESS — Simulation Report\n")

    # Overall stats
    total_runs = len(results)
    total_passed = sum(r.get("passed", 0) for r in results)
    total_failed = sum(r.get("failed", 0) for r in results)
    total_steps = sum(r.get("total", 0) for r in results)

    print(f"  Runs: {total_runs}")
    print(f"  Steps: {total_passed}/{total_steps} passed ({total_failed} failed)")
    if total_steps > 0:
        pass_rate = (total_passed / total_steps) * 100
        print(f"  Pass rate: {pass_rate:.1f}%")
    print()

    # Scenario breakdown
    scenario_stats = defaultdict(lambda: {"runs": 0, "passed": 0, "failed": 0, "total": 0})
    for r in results:
        name = r.get("name", "unknown")
        scenario_stats[name]["runs"] += 1
        scenario_stats[name]["passed"] += r.get("passed", 0)
        scenario_stats[name]["failed"] += r.get("failed", 0)
        scenario_stats[name]["total"] += r.get("total", 0)

    print("  Scenarios:")
    for name, stats in sorted(scenario_stats.items()):
        rate = (stats["passed"] / stats["total"] * 100) if stats["total"] > 0 else 0
        status = "pass" if stats["failed"] == 0 else "FAIL"
        print(f"    [{status}] {name}: {stats['passed']}/{stats['total']} ({rate:.0f}%) over {stats['runs']} run(s)")
    print()

    # Failure classification
    all_failures = []
    for r in results:
        for step in r.get("results", []):
            if not step.get("success", True):
                gap = classify_failure(step)
                all_failures.append({
                    "scenario": r.get("name", "?"),
                    "step": step.get("step", "?"),
                    "action": step.get("action", "?"),
                    "message": step.get("message", "?"),
                    "gap_type": gap,
                    "timestamp": r.get("timestamp", ""),
                })

    if all_failures:
        print("  Failure Classification:")
        gap_counts = defaultdict(int)
        for f in all_failures:
            gap_counts[f["gap_type"]] += 1
        for gap, count in sorted(gap_counts.items(), key=lambda x: -x[1]):
            print(f"    {gap}: {count} occurrence(s)")
        print()

        print("  Recent Failures:")
        recent = sorted(all_failures, key=lambda f: f["timestamp"])[-10:]
        for f in recent:
            print(f"    [{f['gap_type']}] {f['scenario']} step {f['step']}: {f['message'][:80]}")
    else:
        print("  No failures — all scenarios passing!")

    print()

    # Trend (if multiple runs exist)
    if total_runs > 1:
        first = results[0]
        last = results[-1]
        first_rate = (first.get("passed", 0) / first.get("total", 0) * 100) if first.get("total", 0) > 0 else 0
        last_rate = (last.get("passed", 0) / last.get("total", 0) * 100) if last.get("total", 0) > 0 else 0
        trend = "improving" if last_rate > first_rate else "stable" if last_rate == first_rate else "declining"
        print(f"  Trend: {trend} ({first_rate:.0f}% -> {last_rate:.0f}%)")


def main():
    if len(sys.argv) < 2:
        print("Usage: simulation_reporter.py <simulations-dir>", file=sys.stderr)
        sys.exit(2)

    sim_dir = sys.argv[1]
    generate_report(sim_dir)


if __name__ == "__main__":
    main()
