#!/usr/bin/env python3
"""lib/autopilot_trace.py — append-only, human-readable trace of the autopilot
approval-hardening flywheel's decisions (reconcile credits/declines, harden
propose/apply). Pure-append, BEST-EFFORT, FAIL-SOFT: a trace-write failure must
NEVER fail or slow reconcile/propose/apply (reconcile runs from a SessionStart
hook in arbitrary repos). stdlib only.

Target: <repo_root>/docs/superpowers/traces/<session>.md, with a stable
`## Autopilot approval-hardening` section header written once (idempotent).

SAFETY: the feature-pipeline trace files under that same dir are GATED artifacts
that must end with a `## Verdict` ... `PASS` and contain 5 structured `## `
headers (`harness trace verify` enforces this). If the target already exists AND
contains a `## Verdict` line, we MUST NOT append to it (that would corrupt the
gated trace) — instead we write to <session>.autopilot.md. Autopilot synthetic
sessions use ids like `cap-<branch>` that won't collide in practice; this guard
makes corruption impossible regardless.
"""
import datetime
import os

_HEADER = "## Autopilot approval-hardening"


def _now_z():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _is_gated(path):
    """True iff `path` exists and looks like a gated feature-pipeline trace
    (contains a `## Verdict` header line). Best-effort: unreadable -> treat as
    gated (the safe choice — never risk appending to a gated artifact)."""
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("## Verdict"):
                    return True
    except OSError:
        return True
    return False


def _target_path(repo_root, session):
    base = os.path.join(repo_root, "docs", "superpowers", "traces")
    primary = os.path.join(base, "%s.md" % session)
    if os.path.exists(primary) and _is_gated(primary):
        # Never corrupt a gated feature-pipeline trace — sidecar instead.
        return os.path.join(base, "%s.autopilot.md" % session)
    return primary


def append(repo_root, session, lines):
    """Append plain-language `lines` (list[str]) to the per-session autopilot
    trace. Prepends the section header the first time the file is written.
    BEST-EFFORT + FAIL-SOFT: swallows ALL exceptions."""
    try:
        if not lines or not repo_root or not session:
            return
        path = _target_path(repo_root, session)
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        need_header = True
        if os.path.exists(path):
            try:
                with open(path) as f:
                    need_header = _HEADER not in f.read()
            except OSError:
                need_header = False  # can't read -> don't risk a dup header
        with open(path, "a") as f:
            if need_header:
                f.write("\n%s\n" % _HEADER)
            f.write("\n- %s\n" % _now_z())
            for ln in lines:
                f.write("  - %s\n" % ln)
    except Exception:
        # Trace writing must NEVER fail or slow the caller.
        return
