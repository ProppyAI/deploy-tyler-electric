"""Runner for `harness tenant probe <client>`.

Built up incrementally: env loading first, then probe discovery, then
runner core, then secret-leak guard, then the CLI entry. Each task
adds one piece.
"""

import argparse
import importlib
import json as _json
import multiprocessing
import pkgutil
import queue as _queue
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from lib.probes._base import ProbeResult


DEFAULT_OPERATOR_ENV = "~/.harness/operator.env"
DEFAULT_PROBE_PACKAGE = "lib.probes"


def _parse_env_file(path: Path) -> dict:
    """Parse a KEY=value env file. Strips matched single/double quotes
    and skips blank lines and lines starting with '#'."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, rest = line.partition("=")
        key = key.strip()
        rest = rest.lstrip()  # leading space after = is whitespace, not value
        if not rest:
            out[key] = ""
            continue
        # Quoted value: parse until matching closing quote.
        # Anything after closing quote (e.g. ' # comment') is ignored.
        if rest[0] in ("'", '"'):
            quote = rest[0]
            end = rest.find(quote, 1)
            if end == -1:
                # No closing quote — fall through and treat whole rest as raw
                out[key] = rest[1:]
            else:
                out[key] = rest[1:end]
            continue
        # Unquoted value: strip inline ' #...' comment if present.
        # Hash with space before it = comment; hash inside value (e.g.
        # 'p@ss#word') has no leading space, kept as-is.
        hash_pos = rest.find(" #")
        if hash_pos != -1:
            rest = rest[:hash_pos]
        out[key] = rest.rstrip()
    return out


def load_env(deploy_path: str, operator_path: Optional[str] = None) -> dict:
    """Build the env dict for a probe run.

    Loads <deploy_path>/.env.production then ~/.harness/operator.env (or
    operator_path if provided). Tenant wins on key collision; a one-line
    warning is emitted to stderr for each shadowed operator key.
    """
    op_path = Path(operator_path).expanduser() if operator_path else Path(DEFAULT_OPERATOR_ENV).expanduser()
    tenant = _parse_env_file(Path(deploy_path) / ".env.production")
    operator = _parse_env_file(op_path)

    merged = dict(operator)  # start with operator
    for k, v in tenant.items():
        if k in operator:
            print(f"warning: operator.env key {k} shadowed by tenant env", file=sys.stderr)
        merged[k] = v  # tenant always wins
    return merged


# ---------------------------------------------------------------------------
# Probe discovery
# ---------------------------------------------------------------------------


def discover_probes(package: str = DEFAULT_PROBE_PACKAGE) -> list:
    """Discover Probe instances in <package>.

    Imports every module under <package>, looks for a class named `Probe`
    that is a strict subclass of lib.probes._base.Probe (skips _base itself
    and modules that don't expose a Probe class), instantiates it, and
    returns the list sorted by name.
    """
    from lib.probes._base import Probe as ProbeBase

    pkg = importlib.import_module(package)
    instances = []
    for finder, modname, ispkg in pkgutil.iter_modules(pkg.__path__):
        if modname.startswith("_"):
            continue
        full = f"{package}.{modname}"
        try:
            mod = importlib.import_module(full)
        except Exception as e:
            print(
                f"warning: failed to import probe module {full}: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            continue
        cls = getattr(mod, "Probe", None)
        if cls is None:
            continue
        if not (isinstance(cls, type) and issubclass(cls, ProbeBase) and cls is not ProbeBase):
            continue
        if not cls.name:
            print(
                f"warning: probe {full} has empty name attribute, skipping",
                file=sys.stderr,
            )
            continue
        instances.append(cls())
    instances.sort(key=lambda p: p.name)
    return instances


# ---------------------------------------------------------------------------
# Runner core
# ---------------------------------------------------------------------------


@dataclass
class ProbeRun:
    """One probe's name + ProbeResult after execution."""
    name: str
    result: ProbeResult


def _check_required_env(probe, env: dict):
    missing = [k for k in probe.required_env if not env.get(k)]
    return missing


def _run_in_subprocess(probe, env, deployment, queue):
    try:
        r = probe.run(env, deployment)
        queue.put(("ok", r))
    except Exception:
        queue.put(("exc", traceback.format_exc()))


def _run_one(probe, env: dict, deployment: dict) -> "ProbeRun":
    missing = _check_required_env(probe, env)
    if missing:
        return ProbeRun(
            name=probe.name,
            result=ProbeResult(
                status="skipped",
                message=f"required env not set: {', '.join(missing)}",
            ),
        )

    start = time.monotonic()
    ctx = multiprocessing.get_context("spawn")
    queue: multiprocessing.Queue = ctx.Queue()
    proc = ctx.Process(
        target=_run_in_subprocess, args=(probe, env, deployment, queue)
    )
    proc.start()
    try:
        proc.join(probe.timeout_s)
        duration_ms = int((time.monotonic() - start) * 1000)

        if proc.is_alive():
            proc.terminate()
            proc.join(1)
            if proc.is_alive():
                proc.kill()
            return ProbeRun(
                name=probe.name,
                result=ProbeResult(
                    status="fail",
                    message=f"exceeded {probe.timeout_s}s",
                    duration_ms=duration_ms,
                ),
            )

        # Queue.empty() is unreliable across processes per the stdlib docs;
        # use a bounded blocking get() instead. The child has already exited,
        # so any pending pipe data is delivered near-instantly.
        try:
            kind, payload = queue.get(timeout=2)
        except _queue.Empty:
            return ProbeRun(
                name=probe.name,
                result=ProbeResult(
                    status="fail",
                    message="probe exited without returning a result",
                    duration_ms=duration_ms,
                ),
            )
        if kind == "exc":
            first = payload.splitlines()[-1] if payload else "unknown exception"
            return ProbeRun(
                name=probe.name,
                result=ProbeResult(
                    status="fail",
                    message=f"probe raised {first}",
                    details={"traceback_tail": payload[-500:]},
                    duration_ms=duration_ms,
                ),
            )
        payload.duration_ms = duration_ms
        return ProbeRun(name=probe.name, result=payload)
    finally:
        queue.close()
        queue.join_thread()


def run_probes(probes: list, env: dict, deployment: dict) -> list:
    """Run each probe sequentially; isolate exceptions; enforce per-probe timeout."""
    return [_run_one(p, env, deployment) for p in probes]


_CHANNEL_PROBE_MAP = (("telegram-webhook", "telegram"),)


def compute_disabled_skips(deployment: dict) -> tuple[set, dict]:
    """Return (disabled_probe_names, name->skipped_message) for the deployment.

    Per spec §4.4 this MUST run before `_check_required_env` so a channel
    that is intentionally disabled (and therefore has no provisioned secrets)
    surfaces a distinct skipped message rather than 'required env not set'.
    """
    disabled = set(deployment.get("probes", {}).get("disabled", []))
    messages = {n: "disabled in harness.json" for n in disabled}

    channels = deployment.get("channels", {})
    for probe_name, channel_name in _CHANNEL_PROBE_MAP:
        if channels.get(channel_name, {}).get("enabled") is False:
            disabled.add(probe_name)
            messages[probe_name] = f"{channel_name} channel disabled in harness.json"

    return disabled, messages


def exit_code_for_results(results: list) -> int:
    """0 if all ok|skipped, 1 if any warn|fail."""
    for r in results:
        if r.result.status in ("warn", "fail"):
            return 1
    return 0


# ---------------------------------------------------------------------------
# Secret-leak guard
# ---------------------------------------------------------------------------


MIN_SECRET_LEN_FOR_LEAK_CHECK = 8  # avoid false positives on short shared values


def _flatten_to_strings(obj) -> list[str]:
    """Walk obj (dict/list/scalar) and yield every string in keys and values.
    Dict keys are walked too so a probe accidentally emitting a secret as a
    key name (`details={env[KEY]: value}`) is still caught by the guard."""
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_flatten_to_strings(k))
            out.extend(_flatten_to_strings(v))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_flatten_to_strings(v))
    elif isinstance(obj, str):
        out.append(obj)
    return out


def apply_secret_leak_guard(
    results: list,
    env: dict,
    required_env_per_probe: dict,
) -> list:
    """For each ProbeRun, check that no `env[k]` value (for k in the probe's
    required_env) appears in the result's message or details. If a leak is
    found, replace that ProbeRun's result with a fail."""
    guarded: list = []
    for run in results:
        required_keys = required_env_per_probe.get(run.name, [])
        leaked = []
        for k in required_keys:
            secret = env.get(k, "")
            if not secret or len(secret) < MIN_SECRET_LEN_FOR_LEAK_CHECK:
                continue
            haystack_strings = [run.result.message] + _flatten_to_strings(run.result.details)
            if any(secret in h for h in haystack_strings):
                leaked.append(k)
        if leaked:
            run = ProbeRun(
                name=run.name,
                result=ProbeResult(
                    status="fail",
                    message="probe attempted to leak credential into output",
                    details={"leaked_keys": leaked},
                ),
            )
        guarded.append(run)
    return guarded


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def render_table(client: str, results: list) -> str:
    """Human-readable 2-column table."""
    lines = [f"tenant: {client}", ""]
    counts = {"ok": 0, "warn": 0, "fail": 0, "skipped": 0}
    for run in results:
        status = run.result.status
        counts[status] = counts.get(status, 0) + 1
        color = {"ok": "\033[32m", "warn": "\033[33m", "fail": "\033[31m", "skipped": "\033[2m"}.get(status, "")
        reset = "\033[0m"
        badge = f"{color}{status:>8}{reset}"
        lines.append(f"  {badge}  {run.name:<28} {run.result.message}")
    lines.append("")
    summary = " • ".join(f"{v} {k}" for k, v in counts.items() if v)
    lines.append(f"{len(results)} probes • {summary}")
    return "\n".join(lines)


def render_json(client: str, results: list) -> str:
    """Machine-readable JSON."""
    return _json.dumps({
        "client": client,
        "results": [
            {
                "name": run.name,
                "status": run.result.status,
                "message": run.result.message,
                "details": run.result.details,
                "duration_ms": run.result.duration_ms,
            }
            for run in results
        ],
    }, ensure_ascii=False, indent=2)


def main(argv=None) -> int:
    from lib.deployments_registry import (
        DeploymentNotFoundError,
        RegistryError,
        list_clients,
        resolve_deploy_path,
    )

    parser = argparse.ArgumentParser(prog="harness tenant probe")
    parser.add_argument("client", nargs="?", help="tenant name from deployments.json")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of table")
    parser.add_argument("--list", action="store_true", help="list registered tenants")
    parser.add_argument("--describe", action="store_true",
                        help="list probes that would run + their required_env")
    parser.add_argument("--only", default="", help="comma-separated probe names to run")
    parser.add_argument("--skip", default="", help="comma-separated probe names to skip")
    args = parser.parse_args(argv)

    if args.list:
        try:
            clients = list_clients()
        except RegistryError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        for c in clients:
            print(c)
        return 0

    if not args.client:
        parser.error("client name required (or pass --list)")

    try:
        deploy_path = resolve_deploy_path(args.client)
    except DeploymentNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except RegistryError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    harness_json = Path(deploy_path) / "harness.json"
    try:
        deployment = _json.loads(harness_json.read_text())
    except (OSError, _json.JSONDecodeError) as e:
        print(
            f"error: deploy repo at {deploy_path} is missing/malformed harness.json: {e}",
            file=sys.stderr,
        )
        return 2
    deployment["_deploy_repo_path"] = deploy_path  # injected for netlify-deploy probe
    env = load_env(deploy_path)

    probes = discover_probes()

    disabled, disabled_messages = compute_disabled_skips(deployment)

    # --only / --skip
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        probes = [p for p in probes if p.name in wanted]
    if args.skip:
        unwanted = {s.strip() for s in args.skip.split(",") if s.strip()}
        probes = [p for p in probes if p.name not in unwanted]

    if args.describe:
        for p in probes:
            line = f"{p.name:<28} {','.join(p.required_env) or '-'}  {p.description}"
            print(line)
        return 0

    results = []
    for p in probes:
        if p.name in disabled:
            results.append(ProbeRun(
                name=p.name,
                result=ProbeResult(status="skipped", message=disabled_messages[p.name]),
            ))
        else:
            results.append(_run_one(p, env=env, deployment=deployment))
    required_env_per_probe = {p.name: p.required_env for p in probes}
    results = apply_secret_leak_guard(results, env=env, required_env_per_probe=required_env_per_probe)

    if args.json:
        print(render_json(args.client, results))
    else:
        print(render_table(args.client, results))
    return exit_code_for_results(results)


if __name__ == "__main__":
    sys.exit(main())
