#!/usr/bin/env python3
"""lib/autopilot_conductor.py — the thin Autopilot conductor spine.

A deterministic, per-task state machine that drives a planned task through its
stages, consulting the approval ledger before each action (gate), enforcing a
3-strike circuit breaker, and draining a follow-up queue. Stage EXECUTION is
delegated to an injected `executor` callable so this core is fully unit-testable
without spawning processes. stdlib only.
"""
import datetime
import json
import os
import re
import sys as _sys
import tempfile

_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autopilot_ledger as _ledger  # noqa: E402  (sibling in lib/)
import autopilot_runtrace as _runtrace  # noqa: E402  (sibling in lib/)


def _valid_task(task):
    """Return True iff task is a safe, non-traversal identifier."""
    return (bool(task)
            and re.match(r'^[A-Za-z0-9][A-Za-z0-9._-]*$', task) is not None
            and task not in (".", "..")
            and "/" not in task
            and "\\" not in task)


STAGES = ("executing", "review", "deploy", "uat", "followups",
          "propagation", "done")

_NEEDS_HUMAN = ("3-strike", "auth")


def _now_z():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def runs_dir(repo_root):
    """Returns the directory path for all autopilot run state."""
    return os.path.join(repo_root, ".harness", "autopilot", "runs")


def run_dir(repo_root, task):
    """Returns the directory path for a specific task's run state."""
    return os.path.join(runs_dir(repo_root), task)


def _state_path(repo_root, task):
    return os.path.join(run_dir(repo_root, task), "state.json")


def init_run(repo_root, task, branch, worktree, plan):
    """Initializes a new task run, creates state directory, returns initial state dict."""
    if not _valid_task(task):
        raise ValueError("invalid task: %r" % task)
    d = run_dir(repo_root, task)
    os.makedirs(d, exist_ok=True)
    now = _now_z()
    state = {
        "task": task,
        "branch": branch,
        "worktree": worktree,
        "plan": plan,
        "stages": list(STAGES),
        "stage": "executing",
        "attempts": {},
        "max_attempts": 3,
        "followups": [],
        "gates": [],
        "status": "running",
        "trace": os.path.join("docs", "superpowers", "traces",
                              "autopilot-%s.md" % task),
        "created_at": now,
        "updated_at": now,
    }
    save_state(repo_root, task, state)
    return state


def load_state(repo_root, task):
    """Loads task state from disk; raises FileNotFoundError if not found."""
    if not _valid_task(task):
        raise ValueError("invalid task: %r" % task)
    with open(_state_path(repo_root, task)) as f:
        state = json.load(f)
    st_task = state.get("task")
    if not _valid_task(st_task):
        raise ValueError("invalid task in state file: %r" % st_task)
    return state


def save_state(repo_root, task, state):
    """Persists state atomically. SIDE EFFECT: stamps state['updated_at'] in place on
    the passed dict (keeps the in-memory dict in sync with what was written)."""
    state["updated_at"] = _now_z()
    p = _state_path(repo_root, task)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _ledger_dir(repo_root):
    return os.path.join(repo_root, ".harness", "autopilot", "ledger")


def _sessions_dir(repo_root):
    return os.path.join(repo_root, ".harness", "sessions")


def gate_check(repo_root, action_type):
    """Decide whether `action_type` may run unattended. Returns
    (decision, tier, reason). decision is 'proceed' iff the ledger has
    graduated this action-type (honoring the operator veto + branch-matched
    sessions); otherwise 'approval-gate'. Unknown actions classify to T3 ->
    always gated. Any unexpected exception fails closed to 'approval-gate'."""
    try:
        tier = _ledger.classify(action_type)
        veto = _ledger._load_veto()
        auto = _ledger.is_autonomous(
            _ledger_dir(repo_root), action_type,
            veto=veto, sessions_dir=_sessions_dir(repo_root))
        if auto:
            return ("proceed", tier, "graduated (%s)" % tier)
        return ("approval-gate", tier,
                "action_type %r (%s) not graduated" % (action_type, tier))
    except Exception as e:
        return ("approval-gate", "T3",
                "gate_check error (fail-closed): %s" % e)


def record_attempt(state, step_key):
    n = state.setdefault("attempts", {}).get(step_key, 0) + 1
    state["attempts"][step_key] = n
    try:
        cap = int(state.get("max_attempts") or 3)
    except (TypeError, ValueError):
        cap = 3
    cap = max(1, min(cap, 10))
    if n >= cap:
        return "3-strike"
    return "ok"


def reset_attempts(state, step_key):
    state.setdefault("attempts", {})[step_key] = 0


def add_followup(state, summary, action_type=None):
    q = state.setdefault("followups", [])
    fid = max([i.get("id", 0) for i in q], default=0) + 1
    item = {"id": fid, "summary": summary, "action_type": action_type,
            "status": "open", "ts": _now_z()}
    q.append(item)
    return item


def next_followup(state):
    for i in state.get("followups", []):
        if i.get("status") == "open":
            return i
    return None


def resolve_followup(state, fid):
    for i in state.get("followups", []):
        if i.get("id") == fid:
            i["status"] = "done"
            return


def followups_drained(state):
    return next_followup(state) is None


def advance(state):
    """Moves state["stage"] to the next entry in state["stages"]. If already at
    "done" or last, sets state["status"] = "complete" and returns "complete";
    else returns the new stage."""
    stages = state.get("stages", list(STAGES))
    cur = state.get("stage")
    try:
        idx = stages.index(cur)
    except ValueError:
        idx = -1
    if cur == "done" or idx >= len(stages) - 1:
        state["status"] = "complete"
        return "complete"
    state["stage"] = stages[idx + 1]
    return state["stage"]


def run_stage(state, executor):
    """Calls executor(state["stage"], state) and returns its result verbatim."""
    return executor(state.get("stage"), state)


def raise_gate(repo_root, state, gtype, reason):
    """Raise a gate, record it durably, and alert."""
    gate = {"type": gtype, "reason": reason, "ts": _now_z(), "resolved": False}
    state.setdefault("gates", []).append(gate)
    state["status"] = "needs-human" if gtype in _NEEDS_HUMAN else "gated"
    # Durable marker the alerter / `status` reads.
    try:
        d = run_dir(repo_root, state.get("task", "unknown"))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "GATE"), "w") as f:
            f.write("%s: %s\n" % (gtype, reason))
    except OSError:
        pass
    _runtrace.append(repo_root, state.get("task", "unknown"),
                     ["GATE raised (%s): %s" % (gtype, reason),
                      "status -> %s" % state["status"]])
    _sys.stdout.write("AUTOPILOT-GATE %s: %s\n" % (gtype, reason))
    return gate


def open_gate(state):
    """Return the first unresolved gate, or None."""
    for g in state.get("gates", []):
        if not g.get("resolved"):
            return g
    return None


def dry_executor(stage, state):
    """A deterministic no-op executor that succeeds every stage."""
    return ("success", "dry: %s" % stage)


STAGE_ACTION = {
    "executing": "commit_feature_branch",
    "review": "open_pr_to_dev",
    "deploy": "deploy_prod",
    "uat": "qbo_write",
    "followups": "commit_feature_branch",
    "propagation": "auto_merge_dev",
    "done": None,
}


def drive(repo_root, task, executor):
    try:
        state = load_state(repo_root, task)
    except FileNotFoundError:
        state = init_run(repo_root, task,
                         branch="harness/feature/%s" % task,
                         worktree=".worktrees/feature-%s" % task,
                         plan="")
    while state.get("status") == "running":
        stage = state["stage"]
        action = STAGE_ACTION.get(stage)
        if action is None:
            advance(state)
            save_state(repo_root, task, state)
            continue
        decision, tier, reason = gate_check(repo_root, action)
        if decision == "approval-gate":
            raise_gate(repo_root, state, "approval", reason)
            save_state(repo_root, task, state)
            break
        result, note = run_stage(state, executor)
        if result == "success":
            _runtrace.append(repo_root, task,
                             ["stage %s succeeded: %s" % (stage, note)])
            reset_attempts(state, stage)
            advance(state)
        else:
            if record_attempt(state, stage) == "3-strike":
                raise_gate(repo_root, state, "3-strike",
                           "stage %s failed 3x: %s" % (stage, note))
                save_state(repo_root, task, state)
                break
            _runtrace.append(repo_root, task,
                             ["stage %s failed (retry): %s" % (stage, note)])
        save_state(repo_root, task, state)
    return state


def _arg(argv, name, default=None):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def main(argv):
    if not argv:
        _sys.stderr.write("usage: autopilot_conductor.py "
                          "run|status|trace|resume <task> [--repo R]\n")
        return 2
    sub = argv[0]
    rest = argv[1:]
    repo = _arg(rest, "--repo", os.getcwd())
    task = None
    prev = None
    for a in rest:
        if a.startswith("--"):
            prev = a
            continue
        if prev == "--repo":
            prev = a
            continue
        task = a
        break
    if not task:
        _sys.stderr.write("error: <task> required\n")
        return 2
    if not _valid_task(task):
        _sys.stderr.write(
            "error: invalid <task> %r (allowed: [A-Za-z0-9._-], no slashes, not . or ..)\n"
            % task)
        return 2
    if sub == "run":
        st = drive(repo, task, dry_executor)
    elif sub == "resume":
        st = load_state(repo, task)
        g = open_gate(st)
        if g:
            g["resolved"] = True
        st["status"] = "running"
        reset_attempts(st, st.get("stage"))
        try:
            os.unlink(os.path.join(run_dir(repo, task), "GATE"))
        except OSError:
            pass
        save_state(repo, task, st)
        st = drive(repo, task, dry_executor)
    elif sub == "status":
        st = load_state(repo, task)
        _sys.stdout.write("task=%s stage=%s status=%s gate=%s\n" % (
            st["task"], st["stage"], st["status"],
            (open_gate(st) or {}).get("type", "-")))
        return 0
    elif sub == "trace":
        p = os.path.join(repo, "docs", "superpowers", "traces",
                         "autopilot-%s.md" % task)
        _sys.stdout.write(p + "\n")
        if os.path.exists(p):
            with open(p) as f:
                _sys.stdout.write(f.read())
        return 0
    else:
        _sys.stderr.write("unknown subcommand: %s\n" % sub)
        return 2
    _sys.stdout.write("task=%s stage=%s status=%s\n" % (
        st["task"], st["stage"], st["status"]))
    return 0 if st.get("status") == "complete" else 2


if __name__ == "__main__":
    _sys.exit(main(_sys.argv[1:]))
