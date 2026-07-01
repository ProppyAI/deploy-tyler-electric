"""Karpathy loop runner — supervised-autonomous experiment runner.

Implements the spec at docs/superpowers/specs/2026-05-15-karpathy-loop-design.md.
Stdlib-only per HARNESS convention.

Public surface:
- main(argv) -> int                                 CLI entry point
- run_experiment_loop(metric_path, ...) -> list     Top-level loop
- _run_one_experiment(...) -> dict                   Single experiment (mockable)

Unit tests use HARNESS_KARPATHY_MOCK=1 to skip real `claude -p` dispatch.
The mock returns canned output via HARNESS_KARPATHY_MOCK_VERDICT (PASS|FAIL|INCONCLUSIVE)
and HARNESS_KARPATHY_MOCK_SLEEP (seconds to sleep before returning, simulating wall-clock).
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_BUDGET_INPUT_TOKENS = 200000
DEFAULT_BUDGET_OUTPUT_TOKENS = 100000
DEFAULT_WALL_CLOCK_SECONDS = 1800
DEFAULT_MAX_EXPERIMENTS = 20
CONSECUTIVE_FAIL_THRESHOLD = 5
CUMULATIVE_BUDGET_SLACK = 1.2  # 20% slack over max_experiments * wall_clock_seconds

# Spec §4 hard ceilings (rejected at CLI parse time with exit code 2).
MAX_EXPERIMENTS_CEILING = 100
MAX_CUMULATIVE_WALL_CLOCK_SECONDS = 86400
MAX_CUMULATIVE_OUTPUT_TOKENS = 10_000_000

# Spec §7 deny-by-default env allowlist for subagent dispatch.
# HARNESS_-prefixed keys (config overrides, including HARNESS_KARPATHY_MOCK)
# are layered on top in _build_subagent_env.
CLEAN_ENV_ALLOWLIST = ("PATH", "HOME", "ANTHROPIC_API_KEY")


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

def main(argv=None):
    """CLI entry. Returns exit code."""
    if argv is None:
        argv = sys.argv[1:]
    parser = argparse.ArgumentParser(
        prog="harness eval",
        description="Supervised-autonomous Karpathy-loop experiment runner.",
    )
    parser.add_argument("metric_path", help="Path to the parent metric file")
    parser.add_argument("--budget-input-tokens", type=int, default=DEFAULT_BUDGET_INPUT_TOKENS)
    parser.add_argument("--budget-output-tokens", type=int, default=DEFAULT_BUDGET_OUTPUT_TOKENS)
    parser.add_argument("--wall-clock-seconds", type=int, default=DEFAULT_WALL_CLOCK_SECONDS)
    parser.add_argument("--max-experiments", type=int, default=DEFAULT_MAX_EXPERIMENTS)
    parser.add_argument("--worktree-dir", default=None,
                        help="Base directory for experiment worktrees")
    args = parser.parse_args(argv)

    # Spec §4 hard ceilings — reject at CLI parse time with exit code 2.
    if args.max_experiments > MAX_EXPERIMENTS_CEILING:
        sys.stderr.write(
            f"HARNESS: --max-experiments {args.max_experiments} exceeds ceiling "
            f"{MAX_EXPERIMENTS_CEILING}\n"
        )
        return 2
    cumulative_wall_clock = args.max_experiments * args.wall_clock_seconds
    if cumulative_wall_clock > MAX_CUMULATIVE_WALL_CLOCK_SECONDS:
        sys.stderr.write(
            f"HARNESS: cumulative wall-clock {cumulative_wall_clock}s "
            f"(--max-experiments * --wall-clock-seconds) exceeds ceiling "
            f"{MAX_CUMULATIVE_WALL_CLOCK_SECONDS}s\n"
        )
        return 2
    cumulative_output_tokens = args.max_experiments * args.budget_output_tokens
    if cumulative_output_tokens > MAX_CUMULATIVE_OUTPUT_TOKENS:
        sys.stderr.write(
            f"HARNESS: cumulative output-token budget {cumulative_output_tokens} "
            f"(--max-experiments * --budget-output-tokens) exceeds ceiling "
            f"{MAX_CUMULATIVE_OUTPUT_TOKENS}\n"
        )
        return 2

    repo_root = _git_repo_root()
    if not repo_root:
        sys.stderr.write("HARNESS: not inside a git repository\n")
        return 2

    run_id = _mint_session_id()
    worktree_dir = args.worktree_dir or f"/tmp/harness-karpathy/{run_id}"
    os.makedirs(worktree_dir, exist_ok=True)

    # Sweep any stale worktrees under our base dir (gaming pre-mortem #2).
    _sweep_stale_worktrees(repo_root, worktree_dir)

    # Best-effort SIGTERM → KeyboardInterrupt so the existing cleanup path applies.
    # Signal delivery is OS/platform-dependent; this is operator-interrupt hygiene,
    # not a guarantee.
    def _sigterm_handler(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, _sigterm_handler)

    safety_aborted = False
    try:
        outcomes = run_experiment_loop(
            metric_path=args.metric_path,
            budget_input_tokens=args.budget_input_tokens,
            budget_output_tokens=args.budget_output_tokens,
            wall_clock_seconds=args.wall_clock_seconds,
            max_experiments=args.max_experiments,
            worktree_dir=worktree_dir,
            repo_root=repo_root,
            run_id=run_id,
        )
    except KeyboardInterrupt:
        sys.stderr.write("\nHARNESS: operator interrupt (SIGINT/SIGTERM)\n")
        return 130

    # Spec §4 stop #5: parent metric tampering aborts with non-zero exit.
    if outcomes and outcomes[-1].get("stop_reason") == "safety_metric_tampered":
        safety_aborted = True

    # Print final summary table.
    print()
    print(f"=== harness eval run {run_id} ===")
    print(f"experiments: {len(outcomes)}")
    pass_count = sum(1 for o in outcomes if o["verdict"] == "PASS")
    fail_count = sum(1 for o in outcomes if o["verdict"] == "FAIL")
    nh_count = sum(1 for o in outcomes if o["verdict"] == "NEEDS-HUMAN")
    inc_count = sum(1 for o in outcomes if o["verdict"] == "INCONCLUSIVE")
    print(f"  PASS: {pass_count}  FAIL: {fail_count}  NEEDS-HUMAN: {nh_count}  INCONCLUSIVE: {inc_count}")
    if outcomes:
        print(f"stop-reason: {outcomes[-1].get('stop_reason', 'unknown')}")
    if safety_aborted:
        return 4
    return 0


def run_experiment_loop(
    metric_path,
    budget_input_tokens=DEFAULT_BUDGET_INPUT_TOKENS,
    budget_output_tokens=DEFAULT_BUDGET_OUTPUT_TOKENS,
    wall_clock_seconds=DEFAULT_WALL_CLOCK_SECONDS,
    max_experiments=DEFAULT_MAX_EXPERIMENTS,
    worktree_dir=None,
    repo_root=None,
    run_id=None,
):
    """Top-level experiment loop. Returns list of outcome dicts.

    Each outcome dict has keys: experiment_id, verdict, trace_path, branch_name,
    tokens_used, wall_clock_seconds, stop_reason.
    """
    if repo_root is None:
        repo_root = _git_repo_root() or os.getcwd()
    if run_id is None:
        run_id = _mint_session_id()
    if worktree_dir is None:
        worktree_dir = f"/tmp/harness-karpathy/{run_id}"
        os.makedirs(worktree_dir, exist_ok=True)

    # Validate parent metric.
    parent_info = _parse_parent_metric(metric_path, repo_root)
    abs_metric_path = (
        metric_path if os.path.isabs(metric_path)
        else os.path.join(repo_root, metric_path)
    )

    run_dir = os.path.join(repo_root, ".harness", "karpathy", run_id)
    os.makedirs(run_dir, exist_ok=True)

    outcomes = []
    cumulative_start = time.time()
    cumulative_tokens_out = 0
    consecutive_failures = 0
    cumulative_budget_seconds = max_experiments * wall_clock_seconds * CUMULATIVE_BUDGET_SLACK
    cumulative_token_budget = max_experiments * budget_output_tokens * CUMULATIVE_BUDGET_SLACK

    for i in range(max_experiments):
        # Spec §4 stop #5 (pre-iteration): parent metric integrity check.
        if _metric_sha_changed(abs_metric_path, parent_info["metric_sha"], repo_root):
            outcomes.append({
                "experiment_id": None,
                "verdict": "INCONCLUSIVE",
                "trace_path": None,
                "branch_name": None,
                "tokens_used_input": 0,
                "tokens_used_output": 0,
                "wall_clock_seconds": 0,
                "stop_reason": "safety_metric_tampered",
            })
            break

        experiment_id = _mint_session_id()
        branch_name = f"karpathy/exp-{experiment_id}"
        wt_path = os.path.join(worktree_dir, experiment_id)

        outcome = _run_one_experiment(
            parent_metric_path=metric_path,
            parent_metric_sha=parent_info["metric_sha"],
            parent_info=parent_info,
            experiment_id=experiment_id,
            worktree_path=wt_path,
            branch_name=branch_name,
            wall_clock_seconds=wall_clock_seconds,
            budget_input_tokens=budget_input_tokens,
            budget_output_tokens=budget_output_tokens,
            repo_root=repo_root,
        )
        outcomes.append(outcome)
        cumulative_tokens_out += int(outcome.get("tokens_used_output") or 0)

        # Spec §4 stop #5 (post-iteration): re-verify parent metric integrity.
        if _metric_sha_changed(abs_metric_path, parent_info["metric_sha"], repo_root):
            outcome["stop_reason"] = "safety_metric_tampered"
            break

        # Stop conditions.
        if outcome["verdict"] == "PASS":
            # PASS resets the FAIL streak AND terminates with target_met.
            consecutive_failures = 0
            outcome["stop_reason"] = "target_met"
            break
        if outcome["verdict"] in ("FAIL", "INCONCLUSIVE"):
            # MED-1: INCONCLUSIVE counts toward the FAIL streak alongside FAIL.
            consecutive_failures += 1
        # NEEDS-HUMAN and other verdicts leave the counter untouched.
        if consecutive_failures >= CONSECUTIVE_FAIL_THRESHOLD:
            outcome["stop_reason"] = "5_consecutive_failures"
            break
        elapsed_total = time.time() - cumulative_start
        if elapsed_total > cumulative_budget_seconds:
            outcome["stop_reason"] = "cumulative_budget_exhausted"
            outcome["cumulative_budget_axis"] = "wall_clock"
            break
        if cumulative_tokens_out > cumulative_token_budget:
            # Spec §4 stop #2: cumulative output-token consumption exceeded.
            outcome["stop_reason"] = "cumulative_budget_exhausted"
            outcome["cumulative_budget_axis"] = "tokens"
            break
    else:
        # Loop fell through without break.
        if outcomes:
            outcomes[-1]["stop_reason"] = "max_experiments_reached"

    # Write summary.
    summary = {
        "run_id": run_id,
        "metric_path": metric_path,
        "metric_sha": parent_info["metric_sha"],
        "experiments": outcomes,
        "stop_reason": outcomes[-1].get("stop_reason") if outcomes else "no_experiments_run",
    }
    summary_path = os.path.join(run_dir, "summary.json")
    with open(summary_path + ".tmp", "w") as f:
        json.dump(summary, f, indent=2)
    os.rename(summary_path + ".tmp", summary_path)

    return outcomes


def _run_one_experiment(
    parent_metric_path,
    parent_metric_sha,
    experiment_id,
    worktree_path,
    branch_name,
    wall_clock_seconds,
    budget_input_tokens,
    budget_output_tokens,
    repo_root,
    parent_info=None,
):
    """Run a single experiment. Returns outcome dict.

    Mockable via HARNESS_KARPATHY_MOCK=1 env var. When mocked:
      - HARNESS_KARPATHY_MOCK_VERDICT: PASS|FAIL|NEEDS-HUMAN|INCONCLUSIVE
      - HARNESS_KARPATHY_MOCK_SLEEP: seconds to sleep before returning
      - HARNESS_KARPATHY_MOCK_TOKENS_IN / _OUT: token usage to report
    """
    start = time.time()
    outcome = {
        "experiment_id": experiment_id,
        "verdict": "INCONCLUSIVE",
        "trace_path": None,
        "branch_name": branch_name,
        "tokens_used_input": 0,
        "tokens_used_output": 0,
        "wall_clock_seconds": 0,
        "stop_reason": None,
    }

    # Create worktree.
    try:
        subprocess.run(
            ["git", "worktree", "add", "-b", branch_name, worktree_path],
            cwd=repo_root, check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        outcome["verdict"] = "INCONCLUSIVE"
        outcome["stop_reason"] = "worktree_create_failed"
        outcome["error"] = exc.stderr.decode("utf-8", errors="replace")
        outcome["wall_clock_seconds"] = time.time() - start
        return outcome

    # Spec §2 MED-4: mint experiment session JSON inside the worktree so
    # downstream `harness_trace_verify` can find it. Best-effort — failure to
    # write the session does not abort the experiment.
    try:
        _write_experiment_session(
            worktree_path=worktree_path,
            experiment_id=experiment_id,
            branch_name=branch_name,
            parent_metric_path=parent_metric_path,
            parent_metric_sha=parent_metric_sha,
            parent_info=parent_info or {},
        )
    except Exception:
        pass

    try:
        # Dispatch subagent (or mock).
        if os.environ.get("HARNESS_KARPATHY_MOCK") == "1":
            verdict, tokens_in, tokens_out = _mock_subagent(wall_clock_seconds)
            outcome["verdict"] = verdict
            outcome["tokens_used_input"] = tokens_in
            outcome["tokens_used_output"] = tokens_out
            # Mock writes a trace inline so harness_trace_verify path is exercised end-to-end
            # if downstream needs it. For tests we keep it simple.
        else:
            verdict, tokens_in, tokens_out = _dispatch_real_subagent(
                worktree_path=worktree_path,
                parent_metric_path=parent_metric_path,
                parent_metric_sha=parent_metric_sha,
                experiment_id=experiment_id,
                wall_clock_seconds=wall_clock_seconds,
                budget_input_tokens=budget_input_tokens,
                budget_output_tokens=budget_output_tokens,
                repo_root=repo_root,
            )
            outcome["verdict"] = verdict
            outcome["tokens_used_input"] = tokens_in
            outcome["tokens_used_output"] = tokens_out

        outcome["trace_path"] = f"docs/superpowers/traces/{experiment_id}.md"

    finally:
        # Cleanup worktree (gaming pre-mortem #2: --force + graceful missing-dir).
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", worktree_path],
                cwd=repo_root, check=False, capture_output=True,
            )
        except Exception:
            pass

    outcome["wall_clock_seconds"] = time.time() - start
    return outcome


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _git_repo_root():
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], stderr=subprocess.DEVNULL,
        ).decode().strip()
        return out
    except Exception:
        return None


def _mint_session_id():
    """Match the shape of harness_session_id from lib/session.sh."""
    rand = os.urandom(4).hex()
    return f"{int(time.time())}-{os.getpid()}-{rand}"


def _parse_parent_metric(metric_path, repo_root):
    """Read parent metric + session JSON. Returns dict with metric_sha + chain + check_1_command.

    Raises FileNotFoundError if either file is missing.
    """
    abs_metric = os.path.join(repo_root, metric_path) if not os.path.isabs(metric_path) else metric_path
    # MED-5: reject path traversal — resolved path must lie inside repo_root.
    resolved = os.path.realpath(abs_metric)
    repo_resolved = os.path.realpath(repo_root)
    if not (resolved == repo_resolved or resolved.startswith(repo_resolved + os.sep)):
        raise ValueError(f"metric path escapes repo root: {metric_path}")
    abs_metric = resolved
    if not os.path.exists(abs_metric):
        raise FileNotFoundError(f"parent metric not found: {abs_metric}")
    # Parent session ID is the metric filename stem.
    session_id = Path(abs_metric).stem
    session_file = os.path.join(repo_root, ".harness", "sessions", f"{session_id}.json")
    metric_sha = ""
    chain = []
    subcategory = ""
    if os.path.exists(session_file):
        with open(session_file) as f:
            session_data = json.load(f)
        metric_sha = session_data.get("metric_sha", "")
        chain = session_data.get("chain", [])
        subcategory = session_data.get("subcategory", "")
    else:
        # Session JSON unavailable (e.g., backfilled historical metric).
        # Compute sha directly so downstream callers still get a value.
        try:
            metric_sha = subprocess.check_output(
                ["git", "hash-object", abs_metric], cwd=repo_root,
            ).decode().strip()
        except subprocess.CalledProcessError:
            metric_sha = ""
    # Extract Check 1 command — first fenced bash block under "## Measurement method".
    content = open(abs_metric).read()
    check_1_command = _extract_check_command(content, check_index=1)
    return {
        "metric_path": metric_path,
        "metric_sha": metric_sha,
        "chain": chain,
        "subcategory": subcategory,
        "session_id": session_id,
        "check_1_command": check_1_command,
    }


def _extract_check_command(content, check_index=1):
    """Best-effort: extract the FIRST shell command from the measurement method §section.

    The metric's `## Measurement method` typically contains numbered checks (1., 2., 3.)
    with inline `code` snippets or fenced ```bash blocks. This helper returns the first
    `bash …` fenced block; on miss, returns None and the caller falls back to operator UAT.
    """
    m = re.search(r"## Measurement method(.*?)(?=^## )", content, flags=re.DOTALL | re.M)
    if not m:
        return None
    section = m.group(1)
    fences = re.findall(r"```(?:bash|sh)?\s*\n(.*?)```", section, flags=re.DOTALL)
    if fences:
        return fences[0].strip().splitlines()[0] if fences[0].strip() else None
    return None


def _mock_subagent(wall_clock_seconds):
    """Test mock. Honors HARNESS_KARPATHY_MOCK_* env vars.

    Does NOT cap sleep at wall_clock_seconds — that cap is the real-dispatch
    path's `subprocess.run(timeout=...)` kill, not a mock concern. Tests
    intentionally use sleep > wall_clock to simulate cumulative-budget overrun.
    """
    sleep_for = float(os.environ.get("HARNESS_KARPATHY_MOCK_SLEEP", "0"))
    if sleep_for > 0:
        time.sleep(sleep_for)
    verdict = os.environ.get("HARNESS_KARPATHY_MOCK_VERDICT", "PASS")
    tokens_in = int(os.environ.get("HARNESS_KARPATHY_MOCK_TOKENS_IN", "50000"))
    tokens_out = int(os.environ.get("HARNESS_KARPATHY_MOCK_TOKENS_OUT", "20000"))
    return verdict, tokens_in, tokens_out


def _dispatch_real_subagent(
    worktree_path,
    parent_metric_path,
    parent_metric_sha,
    experiment_id,
    wall_clock_seconds,
    budget_input_tokens,
    budget_output_tokens,
    repo_root,
):
    """Real `claude -p` dispatch. Returns (verdict, tokens_in, tokens_out).

    Spec §7: env is scoped to the deny-by-default allowlist (PATH/HOME/ANTHROPIC_API_KEY
    plus HARNESS_-prefixed config). Operator secrets from ~/.harness/operator.env
    (SUPABASE_*, NETLIFY_*, TELEGRAM_*, etc.) are stripped before subprocess.run.
    """
    skill_path = os.path.join(repo_root, "lib", "skills", "karpathy-experiment.md")
    skill_text = open(skill_path).read() if os.path.exists(skill_path) else ""
    prompt = (
        f"{skill_text}\n\n"
        f"---\n\n"
        f"<PARENT_METRIC_PATH>{parent_metric_path}</PARENT_METRIC_PATH>\n"
        f"<PARENT_METRIC_SHA>{parent_metric_sha}</PARENT_METRIC_SHA>\n"
        f"<EXPERIMENT_ID>{experiment_id}</EXPERIMENT_ID>\n"
        f"<WALL_CLOCK_SECONDS>{wall_clock_seconds}</WALL_CLOCK_SECONDS>\n"
        f"<BUDGET_INPUT_TOKENS>{budget_input_tokens}</BUDGET_INPUT_TOKENS>\n"
        f"<BUDGET_OUTPUT_TOKENS>{budget_output_tokens}</BUDGET_OUTPUT_TOKENS>\n"
        f"<WORKTREE_PATH>{worktree_path}</WORKTREE_PATH>\n"
    )
    argv = _build_claude_argv(prompt)
    clean_env = _build_subagent_env(os.environ)
    try:
        proc = subprocess.run(
            argv, cwd=worktree_path, capture_output=True,
            timeout=wall_clock_seconds, text=True, env=clean_env,
        )
    except subprocess.TimeoutExpired:
        return ("FAIL", 0, 0)
    # Parse final JSON line from stdout.
    verdict, tokens_in, tokens_out = "INCONCLUSIVE", 0, 0
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
            verdict = payload.get("verdict", "INCONCLUSIVE")
            tokens_in = int(payload.get("tokens_used_input", 0))
            tokens_out = int(payload.get("tokens_used_output", 0))
            break
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    return (verdict, tokens_in, tokens_out)


def _build_claude_argv(prompt):
    """Build the argv for `claude -p` dispatch. Separate function for testability."""
    return [
        "claude",
        "-p", prompt,
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
    ]


def _build_subagent_env(parent_env):
    """Spec §7 deny-by-default env construction for subagent dispatch.

    Allowlist: PATH, HOME, ANTHROPIC_API_KEY, plus any HARNESS_-prefixed key.
    Operator secrets (SUPABASE_*, NETLIFY_*, TELEGRAM_*, etc.) loaded from
    ~/.harness/operator.env into the parent process are explicitly NOT
    forwarded; subagents have no business with operator credentials.
    """
    clean_env = {k: parent_env[k] for k in CLEAN_ENV_ALLOWLIST if k in parent_env}
    clean_env.update({k: v for k, v in parent_env.items() if k.startswith("HARNESS_")})
    return clean_env


def _metric_sha_changed(abs_metric_path, expected_sha, repo_root):
    """Spec §4 stop #5: re-hash parent metric and compare to pinned sha.

    Returns True iff (a) we have an expected_sha to compare AND (b) the file's
    current hash differs. On hash-compute error returns False (best-effort —
    don't fail the loop on transient git errors).
    """
    if not expected_sha:
        return False
    try:
        current_sha = subprocess.check_output(
            ["git", "hash-object", abs_metric_path],
            cwd=repo_root, stderr=subprocess.DEVNULL,
        ).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return current_sha != expected_sha


def _write_experiment_session(
    worktree_path, experiment_id, branch_name,
    parent_metric_path, parent_metric_sha, parent_info,
):
    """Spec §2 MED-4: write a minimal session JSON for the experiment.

    Lives inside the worktree's .harness/sessions/ so downstream tools
    (harness_trace_verify) can find it. Inherits parent metric_file +
    metric_sha (intentional — the experiment is testing the same pinned
    target). Strips brainstorming/define-metric from the chain.
    """
    from datetime import datetime, timezone

    sessions_dir = os.path.join(worktree_path, ".harness", "sessions")
    os.makedirs(sessions_dir, exist_ok=True)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        user = subprocess.check_output(
            ["git", "config", "user.name"], cwd=worktree_path,
            stderr=subprocess.DEVNULL,
        ).decode().strip() or "karpathy"
    except (subprocess.CalledProcessError, FileNotFoundError):
        user = "karpathy"

    parent_session_id = parent_info.get("session_id", "") if parent_info else ""
    subcategory = (parent_info or {}).get("subcategory") or "infra"
    parent_chain = (parent_info or {}).get("chain") or []
    # Drop stages that don't apply to a single experiment iteration.
    stripped_chain = [s for s in parent_chain if s not in ("brainstorming", "define-metric")]

    session = {
        "session_id": experiment_id,
        "org": "ProppyAI",
        "repo": "HARNESS",
        "branch": branch_name,
        "pr": None,
        "source": "karpathy-runner",
        "intent": "feature",
        "subcategory": subcategory,
        "request": f"karpathy-experiment of parent {parent_session_id}",
        "user": user,
        "stage": "executing",
        "stages_completed": [],
        "metric_file": parent_metric_path,
        "metric_sha": parent_metric_sha,
        "status": "running",
        "iteration": 1,
        "max_iterations": 1,
        "started_at": now,
        "updated_at": now,
        "pid": os.getpid(),
    }
    if stripped_chain:
        session["chain"] = stripped_chain

    session_path = os.path.join(sessions_dir, f"{experiment_id}.json")
    tmp_path = session_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(session, f, indent=2)
    os.rename(tmp_path, session_path)
    return session_path


def _sweep_stale_worktrees(repo_root, worktree_base_dir):
    """Best-effort cleanup of stale worktrees under our base dir (gaming pre-mortem #2)."""
    try:
        out = subprocess.check_output(
            ["git", "worktree", "list", "--porcelain"], cwd=repo_root,
        ).decode()
        for block in out.split("\n\n"):
            wt_line = block.splitlines()[0] if block.strip() else ""
            if not wt_line.startswith("worktree "):
                continue
            path = wt_line[len("worktree "):]
            if path.startswith(worktree_base_dir):
                subprocess.run(
                    ["git", "worktree", "remove", "--force", path],
                    cwd=repo_root, check=False, capture_output=True,
                )
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
