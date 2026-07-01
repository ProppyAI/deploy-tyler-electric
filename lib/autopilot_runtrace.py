#!/usr/bin/env python3
"""lib/autopilot_runtrace.py — append-only, human-readable per-task run journal
for the Autopilot conductor. A reviewer reads it top-to-bottom and reconstructs
the run: each stage entered, each decision + WHY, each gate raised (with reason).
FAIL-SOFT: a trace-write error never crashes or slows the conductor. stdlib only.

Reuses the gated-trace guard from autopilot_trace.py: never append to a file that
contains a `## Verdict` line (a gated feature-pipeline artifact) — sidecar instead.
"""
import datetime
import os


def _now_z():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _is_gated(path):
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("## Verdict"):
                    return True
    except OSError:
        return True
    return False


def _target_path(repo_root, task):
    base = os.path.join(repo_root, "docs", "superpowers", "traces")
    primary = os.path.join(base, "autopilot-%s.md" % task)
    if os.path.exists(primary) and _is_gated(primary):
        return os.path.join(base, "autopilot-%s.autopilot.md" % task)
    return primary


def append(repo_root, task, lines):
    try:
        if not lines or not repo_root or not task:
            return
        header = "## Autopilot run: %s" % task
        path = _target_path(repo_root, task)
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        need_header = True
        if os.path.exists(path):
            try:
                with open(path) as f:
                    need_header = header not in f.read()
            except OSError:
                need_header = False
        with open(path, "a") as f:
            if need_header:
                f.write("\n%s\n" % header)
            f.write("\n- %s\n" % _now_z())
            for ln in lines:
                f.write("  - %s\n" % ln)
    except Exception:
        return
