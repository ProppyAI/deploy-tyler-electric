#!/usr/bin/env python3
"""autopilot_capture.py — map tool calls and allow-rule strings to stable
action_type keys for the approval-hardening flywheel. Pure + fail-closed:
an unrecognized shape yields a key that autopilot_ledger.classify() resolves to
T3, and a too-broad allow-rule yields None (no credit). stdlib only."""
import json
import os
import shlex
import subprocess
import sys

# Make sibling lib modules importable (autopilot_trace).
_LIB = os.path.dirname(os.path.abspath(__file__))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import autopilot_trace as trace             # noqa: E402

# Path to the Phase-L ledger CLI (same lib/ dir). Resolved once at import.
_LEDGER = os.path.join(_LIB, "autopilot_ledger.py")

# Native (non-Bash) tools we key by tool name.
_TOOL_KEYS = {
    "Read": "read_file", "Glob": "glob_search", "Grep": "grep_search",
}

# Bash: (first_token, subcommand) -> action_type. Mirrors TIER_MAP keys so usage
# and trust join on the same string. Unlisted shapes fall through to a generated
# key that classify() maps to T3.
_BASH_KEYS = {
    ("git", "status"): "git_status",
    ("git", "log"): "git_log",
    ("git", "diff"): "git_diff",
    ("git", "push"): "git_push",
    ("netlify", "deploy"): "deploy_prod",
}
# gh pr <verb> refinement
_GH_PR = {"view": "gh_pr_view", "list": "gh_pr_list", "checks": "gh_pr_checks"}


def _strip_env(tokens):
    """Drop leading VAR=value env assignments (e.g. HARNESS_SKIP=1 git push)."""
    i = 0
    while i < len(tokens) and "=" in tokens[i] and not tokens[i].startswith("-") \
            and "/" not in tokens[i].split("=", 1)[0]:
        i += 1
    return tokens[i:]


# Shell operators that chain/compose multiple commands. Their presence means the
# rule/command spans more than one simple command and must fail closed.
_SHELL_OPS = {"&&", "||", "|", ";", "&"}


def command_to_action(command):
    """Bash command string -> action_type (never raises; unknown -> bash_<v>_<s>).
    Keys only the FIRST simple command; compound/piped inputs (any shell operator
    in _SHELL_OPS) fail closed to "bash_compound" (not in TIER_MAP -> T3)."""
    try:
        toks = _strip_env(shlex.split(command))
    except ValueError:
        toks = _strip_env(command.split())
    if not toks:
        return "bash_empty"
    if any(t in _SHELL_OPS for t in toks):
        return "bash_compound"  # too broad to key -> fail closed to T3
    first = toks[0].rsplit("/", 1)[-1]
    sub = toks[1] if len(toks) > 1 else ""
    if first == "gh" and sub == "pr":
        verb = toks[2] if len(toks) > 2 else ""
        return _GH_PR.get(verb, "gh_pr_other")
    key = _BASH_KEYS.get((first, sub))
    if key:
        return key
    # fail-closed generated key (not in TIER_MAP -> classify() == T3)
    return "bash_%s_%s" % (first, sub) if sub else "bash_%s" % first


def classify_tool(tool_name, tool_input):
    """(tool_name, tool_input dict) -> action_type."""
    if tool_name == "Bash":
        return command_to_action((tool_input or {}).get("command", ""))
    return _TOOL_KEYS.get(tool_name, "tool_%s" % tool_name.lower())


def rule_to_action(rule):
    """Settings allow-rule string -> action_type, or None if too broad to credit.
    Handles `Tool(prefix:*)` and exact `Tool(cmd)`; bare `Tool`, `Tool(*)`, or a
    multi-wildcard prefix is too broad -> None."""
    rule = (rule or "").strip()
    if "(" not in rule or not rule.endswith(")"):
        return None  # bare `Bash` etc.
    tool, inner = rule[:-1].split("(", 1)
    inner = inner.strip()
    if inner in ("", "*", ":*"):
        return None  # Tool(*) / Tool(:*) -> too broad
    # strip a single trailing ":*" glob; reject if other wildcards remain
    body = inner[:-2] if inner.endswith(":*") else inner
    if "*" in body:
        return None  # multi-wildcard / embedded glob -> too broad
    if tool == "Bash":
        action = command_to_action(body)
        if action == "bash_compound":
            return None  # compound/piped rule is too broad to credit
        return action
    return _TOOL_KEYS.get(tool, "tool_%s" % tool.lower())


# ---------------------------------------------------------------------------
# Reconciler — the security-critical trust boundary. Joins action *usage* with
# the operator's *local allowlist* and writes operator-attributable credit
# entries to the Phase-L ledger. Usage frequency alone grants NOTHING: an action
# is credited only if it is operator-allowlisted (a permissions.allow rule maps
# to it via rule_to_action AND no deny/ask rule blocks it). Bounded one credit
# per action per call; decline-on-removal; idempotent.
# ---------------------------------------------------------------------------

def _load_allow_deny_ask(settings_paths):
    """Merge permissions.allow/deny/ask across the given settings files. We UNION
    (any file that allows counts; any deny/ask blocks). Missing/garbage files are
    skipped. A non-dict root or non-list permission value yields nothing."""
    allow, deny, ask = set(), set(), set()
    for p in settings_paths:
        try:
            with open(p) as f:
                root = json.load(f)
        except Exception:
            continue
        if not isinstance(root, dict):
            continue
        perms = root.get("permissions", {})
        if not isinstance(perms, dict):
            continue
        for key, dest in (("allow", allow), ("deny", deny), ("ask", ask)):
            vals = perms.get(key, [])
            if isinstance(vals, list):
                dest |= {v for v in vals if isinstance(v, str)}
    return allow, deny, ask


def _rule_tool(rule):
    """The tool-family name of a rule: the substring before the first '(', stripped.
    e.g. `Bash(gh pr view:*)` -> `Bash`; bare `Bash` -> `Bash`; `Read(*)` -> `Read`.
    Returns "" if empty."""
    rule = (rule or "").strip()
    return rule.split("(", 1)[0].strip()


def _allowlisted_actions(allow, deny, ask):
    """Return {action_type} that map from an allow rule via rule_to_action AND are
    NOT blocked. Fail-closed in TWO ways:
      * SPECIFIC block: a deny/ask rule that maps to a concrete action via
        rule_to_action blocks that action (and only it).
      * BROAD block: a deny/ask rule too broad to map (rule_to_action -> None, e.g.
        `Bash(*)`, bare `Bash`, `Read(*)`) vetoes the WHOLE tool family — every
        allow rule whose tool matches is dropped. This is the security veto: an
        operator's broad `Bash(*)` deny must beat any specific `Bash(...)` allow."""
    broad_block_tools = {
        _rule_tool(r) for r in (deny | ask)
        if rule_to_action(r) is None and _rule_tool(r)
    }
    specific_block = {
        rule_to_action(r) for r in (deny | ask) if rule_to_action(r)
    }
    out = set()
    for r in allow:
        a = rule_to_action(r)
        if a is None:
            continue
        if a in specific_block or _rule_tool(r) in broad_block_tools:
            continue  # fail-closed: specifically denied OR family broadly denied
        out.add(a)
    return out


def _read_int_file(path):
    """Read a small integer from a file; 0 on any error / missing / garbage."""
    try:
        with open(path) as f:
            return int(f.read().strip() or "0")
    except Exception:
        return 0


def _ledger_append(ledger_dir, action, decision, outcome, session, branch):
    """Shell out to the Phase-L ledger CLI for ONE append. source is always
    'operator' (this is the operator-attributable credit/decline path)."""
    subprocess.run(
        ["python3", _LEDGER, "append", ledger_dir, action, decision, outcome,
         session, "operator", "--branch", branch],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def reconcile(root, ledger_dir, session, branch, settings_paths):
    """Join action usage x the operator allowlist -> Phase-L ledger appends.

    Trust model (do NOT violate):
      * An action is credited ONLY if it is operator-allowlisted (an allow rule
        maps to it via rule_to_action and no deny/ask rule blocks it). Usage
        frequency alone grants nothing.
      * BOUNDED: at most ONE credit entry per action_type per call. A per-action
        watermark (count at last reconcile) means a single new use credits once;
        a loop of N uses cannot inflate the streak.
      * DECLINE-ON-REMOVAL: if an action we previously credited (tracked by a
        `<action>.credited` marker) is no longer allowlisted, append ONE
        declined/failure entry (Phase-L resets its streak) and clear the marker.
      * IDEMPOTENT: re-running with no new usage and an unchanged allowlist writes
        nothing.

    Returns {"credited": [...], "declined": [...]}.
    """
    udir = os.path.join(root, ".harness", "autopilot", "usage")
    if not os.path.isdir(udir):
        return {"credited": [], "declined": []}
    allow, deny, ask = _load_allow_deny_ask(settings_paths)
    allowed = _allowlisted_actions(allow, deny, ask)

    def _matched_rule(action):
        """The first operator allow-rule that maps to `action`, for the trace."""
        for r in sorted(allow):
            if rule_to_action(r) == action:
                return r
        return None

    credited, declined = [], []
    trace_lines = []
    for name in sorted(os.listdir(udir)):
        if not name.endswith(".count"):
            continue
        action = name[:-len(".count")]
        count = _read_int_file(os.path.join(udir, name))
        wm_path = os.path.join(udir, action + ".watermark")
        wm = _read_int_file(wm_path)
        credited_marker = os.path.join(udir, action + ".credited")
        was_credited = os.path.exists(credited_marker)
        if action in allowed:
            if count > wm:
                # BOUNDED: one entry per reconcile regardless of how many new uses.
                _ledger_append(ledger_dir, action, "approved", "success",
                               session, branch)
                credited.append(action)
                trace_lines.append(
                    "reconcile: action=%s rule=%r decision=credited "
                    "(operator-allowlisted)" % (action, _matched_rule(action)))
                with open(credited_marker, "w"):
                    pass
            # Advance the watermark even when count == wm (no-op) so a later use
            # credits exactly once. Always write so the file reflects current count.
            with open(wm_path, "w") as f:
                f.write(str(count))
        else:
            # Not allowlisted now. If we credited it before, record ONE decline
            # (Phase-L resets the streak) and clear the marker so re-runs are
            # idempotent. Never-credited unused/used actions stay untouched.
            if was_credited:
                _ledger_append(ledger_dir, action, "declined", "failure",
                               session, branch)
                declined.append(action)
                trace_lines.append(
                    "reconcile: action=%s rule=<none> decision=declined "
                    "(operator allow-rule removed)" % action)
                try:
                    os.remove(credited_marker)
                except OSError:
                    pass
    # Human-readable trace (best-effort, fail-soft). Skip entirely when nothing
    # was credited or declined — no empty noise.
    if trace_lines:
        trace.append(root, session, trace_lines)
    return {"credited": credited, "declined": declined}


def _default_settings_stack(root):
    """The default settings stack when --settings is omitted: operator-global,
    then repo-local, then repo-local-private. Non-existent files are skipped by
    _load_allow_deny_ask."""
    return [
        os.path.expanduser("~/.claude/settings.json"),
        os.path.join(root, ".claude", "settings.json"),
        os.path.join(root, ".claude", "settings.local.json"),
    ]


def main(argv):
    if len(argv) >= 2 and argv[1] == "reconcile":
        root = ledger = session = None
        branch = ""
        settings_paths = []
        i = 2
        while i < len(argv):
            a = argv[i]
            if a == "--root" and i + 1 < len(argv):
                root = argv[i + 1]; i += 2
            elif a == "--ledger" and i + 1 < len(argv):
                ledger = argv[i + 1]; i += 2
            elif a == "--session" and i + 1 < len(argv):
                session = argv[i + 1]; i += 2
            elif a == "--branch" and i + 1 < len(argv):
                branch = argv[i + 1]; i += 2
            elif a == "--settings" and i + 1 < len(argv):
                settings_paths.append(argv[i + 1]); i += 2
            else:
                i += 1
        if root is None or ledger is None or session is None:
            sys.stderr.write(
                "usage: autopilot_capture.py reconcile --root R --ledger L "
                "--session S [--branch B] [--settings P]...\n")
            return 2
        if not settings_paths:
            settings_paths = _default_settings_stack(root)
        result = reconcile(root, ledger, session, branch, settings_paths)
        json.dump(result, sys.stdout)
        sys.stdout.write("\n")
        return 0
    if len(argv) >= 4 and argv[1] == "classify-tool":
        try:
            ti = json.loads(argv[3])
        except Exception:
            ti = {}
        sys.stdout.write(classify_tool(argv[2], ti))
        return 0
    if len(argv) >= 3 and argv[1] == "rule-to-action":
        sys.stdout.write(rule_to_action(argv[2]) or "")
        return 0
    sys.stderr.write("usage: autopilot_capture.py classify-tool <tool> <json> | "
                     "rule-to-action <rule> | reconcile --root R --ledger L "
                     "--session S [--branch B] [--settings P]...\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
