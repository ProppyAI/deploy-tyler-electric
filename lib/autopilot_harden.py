#!/usr/bin/env python3
"""lib/autopilot_harden.py — operator-ratified promotion of a graduated action
into the durable autopilot hardened-allowlist (Component L "harden" verb).

The approval-hardening flywheel earns autonomy in the ledger (capture + graduation),
but the LEDGER lives under .harness/ (gitignored, agent-writable). `harden` is the
bridge from "earned in the ledger" to "durably ratified": it proposes promoting a
graduated action into a git-tracked allowlist (lib/autopilot/hardened-allowlist.json),
and `apply` mutates that file so the landing goes through an operator-ratified PR.

Verbs:
  status   — is the action graduated AND not already hardened? (read-only)
  propose  — assert {is-autonomous (C2 session gate) ∧ tier != T3 ∧ still
             operator-allowlisted}; resolve the operator's allow-RULE string for
             the action; write .harness/autopilot/harden/<action>.proposal.json.
  apply    — re-validate the proposal still holds, append `rule` to
             hardened-allowlist.json (creating it if absent), return the result
             for the caller to commit. STATE-CHANGING + operator-invoked only.

Trust model: status/propose are read-only-ish (propose writes a reviewable
proposal under the agent-writable .harness/, NOT the tracked allowlist). Only
`apply` mutates the tracked allowlist, and bin/harness routes it through an
operator-driven git/PR flow (never a hook). Every assert fails CLOSED with a
`refused: <reason>` message + non-zero exit. stdlib only.
"""
import json
import os
import sys
import tempfile

# Make sibling lib modules importable (autopilot_ledger, autopilot_capture).
_LIB = os.path.dirname(os.path.abspath(__file__))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import autopilot_ledger as ledger          # noqa: E402
import autopilot_capture as capture         # noqa: E402
import autopilot_trace as trace             # noqa: E402

# The durable, git-tracked hardened allowlist (CREATED in Task 6 — may be absent).
# Path is relative to the HARNESS root (the lib/ parent).
HARDENED_ALLOWLIST_REL = os.path.join("lib", "autopilot", "hardened-allowlist.json")


def _harden_dir(root):
    return os.path.join(root, ".harness", "autopilot", "harden")


def _proposal_path(root, action):
    return os.path.join(_harden_dir(root), "%s.proposal.json" % action)


def _hardened_allowlist_path():
    # The allowlist lives under the HARNESS root (lib/ parent), git-tracked.
    return os.path.join(os.path.dirname(_LIB), HARDENED_ALLOWLIST_REL)


def _load_hardened_allowlist(path=None):
    """Load the hardened allowlist. Absent file -> empty default (Task 6 creates
    it; until then we treat absence as {"version":1,"allow":[]}). A malformed file
    raises so apply fails loudly rather than silently overwriting operator data."""
    path = path or _hardened_allowlist_path()
    if not os.path.exists(path):
        return {"version": 1, "allow": []}
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("hardened-allowlist.json root is not a dict: %s" % path)
    data.setdefault("version", 1)
    if not isinstance(data.get("allow"), list):
        data["allow"] = []
    return data


def _settings_stack(root, settings_paths):
    """If explicit --settings given, use exactly those; else the default stack."""
    if settings_paths:
        return list(settings_paths)
    return capture._default_settings_stack(root)


def _allowlisted_rule_for(action, settings_paths):
    """Return (allowed_set, rule_string_or_None). The rule string is the FIRST
    operator allow-rule that maps to `action` via rule_to_action; allowed_set is
    the set of operator-allowlisted (non-denied/asked) action_types."""
    allow, deny, ask = capture._load_allow_deny_ask(settings_paths)
    allowed = capture._allowlisted_actions(allow, deny, ask)
    rule = None
    for r in sorted(allow):
        if capture.rule_to_action(r) == action:
            rule = r
            break
    return allowed, rule


def _already_hardened(action, settings_paths, allowlist_path=None):
    """True iff an allow rule in the hardened allowlist already maps to `action`."""
    try:
        data = _load_hardened_allowlist(allowlist_path)
    except (ValueError, OSError):
        return False
    for r in data.get("allow", []):
        if isinstance(r, str) and capture.rule_to_action(r) == action:
            return True
    return False


def _evaluate(root, ledger_dir, action, settings_paths, sessions_dir,
              allowlist_path=None):
    """Run all gates for an action. Returns (ok, reason, info).
    info = {tier, clean_streak, rule, sessions, autonomous}. Fail-closed: any
    failing gate -> (False, "<reason>", info)."""
    veto = ledger._load_veto()
    st = ledger.graduation_status(ledger_dir, veto=veto,
                                  sessions_dir=sessions_dir).get(action, {})
    tier = ledger.classify(action)
    clean_streak = st.get("clean_streak", 0)
    info = {"tier": tier, "clean_streak": clean_streak, "rule": None,
            "sessions": [], "autonomous": bool(st.get("autonomous"))}

    # T3 (or unknown -> T3) never auto-graduates; refuse before anything else.
    if tier == "T3":
        return (False, "tier %s is never auto-graduated (T3/unknown)" % tier, info)

    # Production autonomy gate (C2 branch-matched session record enforced).
    if not ledger.is_autonomous(ledger_dir, action, veto=veto,
                                sessions_dir=sessions_dir):
        return (False,
                "not graduated (autonomy gate failed: streak=%s, vetoed=%s, "
                "integrity_ok=%s)" % (clean_streak, st.get("vetoed"),
                                      st.get("integrity_ok")),
                info)

    # Still operator-allowlisted, and resolve the rule string we'd promote.
    allowed, rule = _allowlisted_rule_for(action, settings_paths)
    if action not in allowed or rule is None:
        return (False,
                "not operator-allowlisted (no allow rule maps to %r)" % action,
                info)
    info["rule"] = rule

    # Don't re-propose something already in the durable hardened allowlist.
    if _already_hardened(action, settings_paths, allowlist_path):
        return (False, "already in hardened allowlist", info)

    # Operator-attributable sessions backing the streak (audit context).
    info["sessions"] = _backing_sessions(ledger_dir, action)
    return (True, "", info)


def _backing_sessions(ledger_dir, action):
    """The distinct operator-session ids of clean approved entries for `action`,
    most recent last. Purely informational context for the proposal/PR."""
    out = []
    for e in ledger.read_entries(ledger_dir):
        if e is ledger._MALFORMED or not isinstance(e, dict):
            continue
        if e.get("action_type") != action:
            continue
        if e.get("source") == "operator" and e.get("decision") == "approved" \
                and e.get("outcome") == "success":
            sid = e.get("session")
            if sid and sid not in out:
                out.append(sid)
    return out


def cmd_status(root, ledger_dir, action, settings_paths, sessions_dir,
               allowlist_path=None):
    ok, reason, info = _evaluate(root, ledger_dir, action, settings_paths,
                                 sessions_dir, allowlist_path=allowlist_path)
    payload = {"action_type": action, "tier": info["tier"],
               "clean_streak": info["clean_streak"], "ready": ok,
               "rule": info["rule"]}
    if not ok:
        payload["reason"] = reason
    json.dump(payload, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if ok else 1


def cmd_propose(root, ledger_dir, action, settings_paths, sessions_dir,
                allowlist_path=None, session=None):
    ok, reason, info = _evaluate(root, ledger_dir, action, settings_paths,
                                 sessions_dir, allowlist_path=allowlist_path)
    if not ok:
        sys.stderr.write("refused: %s\n" % reason)
        return 1
    proposal = {
        "action_type": action,
        "tier": info["tier"],
        "clean_streak": info["clean_streak"],
        "rule": info["rule"],
        "target": HARDENED_ALLOWLIST_REL.replace(os.sep, "/"),
        "sessions": info["sessions"],
    }
    d = _harden_dir(root)
    os.makedirs(d, mode=0o700, exist_ok=True)
    p = _proposal_path(root, action)
    with open(p, "w") as f:
        json.dump(proposal, f, sort_keys=True, indent=2)
    # Human-readable trace (best-effort, fail-soft): what was proposed and why.
    if session:
        trace.append(root, session, [
            "harden propose: action=%s tier=%s clean_streak=%s rule=%r "
            "backing_sessions=%s" % (action, info["tier"], info["clean_streak"],
                                     info["rule"], info["sessions"] or "[]"),
        ])
    sys.stdout.write("proposal written: %s\n" % p)
    json.dump(proposal, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def cmd_apply(root, ledger_dir, action, settings_paths, sessions_dir,
              allowlist_path=None, session=None):
    """STATE-CHANGING: re-validate the proposal still holds, then append `rule`
    to the hardened allowlist (creating it if absent). Returns rc 0 + prints a
    JSON result the caller commits; rc 1 + `refused:` if any gate now fails."""
    allowlist_path = allowlist_path or _hardened_allowlist_path()
    p = _proposal_path(root, action)
    if not os.path.exists(p):
        sys.stderr.write("refused: no proposal for %r (run propose first)\n" % action)
        return 1
    try:
        with open(p) as f:
            proposal = json.load(f)
    except (ValueError, OSError) as e:
        sys.stderr.write("refused: proposal unreadable: %s\n" % e)
        return 1
    if not isinstance(proposal, dict):
        sys.stderr.write("refused: proposal is not a JSON object\n")
        return 1

    # Re-validate every gate at apply time — the proposal is agent-writable and
    # the world may have changed since propose (streak broken, veto added, rule
    # removed). Never trust the stored proposal blindly.
    ok, reason, info = _evaluate(root, ledger_dir, action, settings_paths,
                                 sessions_dir, allowlist_path=allowlist_path)
    if not ok:
        sys.stderr.write("refused: %s\n" % reason)
        return 1

    rule = info["rule"]
    # The proposal's rule must still match what we'd resolve now (defense against
    # a tampered proposal pointing at a different/broader rule).
    if proposal.get("rule") != rule:
        sys.stderr.write(
            "refused: proposal rule %r no longer matches resolved rule %r\n"
            % (proposal.get("rule"), rule))
        return 1

    data = _load_hardened_allowlist(allowlist_path)
    if rule in data["allow"]:
        sys.stderr.write("refused: rule %r already in hardened allowlist\n" % rule)
        return 1
    data["allow"].append(rule)
    data["allow"] = sorted(set(data["allow"]))
    parent = os.path.dirname(allowlist_path) or "."
    os.makedirs(parent, exist_ok=True)
    # Atomic write: a temp file in the SAME directory then os.replace, so a kill /
    # disk-full mid-write never leaves the durable hardened allowlist truncated or
    # half-written (a torn write here loses EVERY previously-hardened rule). Mirrors
    # lib/autopilot/merge_allowlist.py:_write_settings — this is the most
    # security-critical file in the flywheel and must get the same atomicity.
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".hardened-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, sort_keys=True, indent=2)
            f.write("\n")
        os.replace(tmp, allowlist_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # `target` is the repo-relative contract string (same as propose's `target`);
    # `target_path` carries the absolute path actually written (operator clarity).
    target_rel = HARDENED_ALLOWLIST_REL.replace(os.sep, "/")
    result = {"action_type": action, "rule": rule,
              "target": target_rel,
              "target_path": allowlist_path, "allow": data["allow"]}
    # Human-readable trace (best-effort, fail-soft): the durable record of what
    # was hardened, where, and the operator git/PR steps the landing requires.
    if session:
        trace.append(root, session, [
            "harden apply: action=%s rule=%r target=%s"
            % (action, rule, target_rel),
            "operator must land via PR: git add %s && commit && push "
            "(pre-push /pr-review reviews + opens the PR; never a hook)"
            % target_rel,
        ])
    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def _parse(argv):
    """Parse `<verb> --root R --ledger L [--settings P]... --action A
    [--sessions D] [--allowlist F]`. Returns (verb, opts) or (None, errmsg)."""
    if not argv:
        return None, "usage: autopilot_harden.py <status|propose|apply> --root R " \
                     "--ledger L --action A [--settings P]... [--sessions D]"
    verb = argv[0]
    opts = {"root": None, "ledger": None, "action": None, "sessions": None,
            "settings": [], "allowlist": None, "session": None}
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--settings" and i + 1 < len(argv):
            opts["settings"].append(argv[i + 1]); i += 2
        elif a in ("--root", "--ledger", "--action", "--sessions", "--allowlist",
                   "--session") and i + 1 < len(argv):
            opts[a[2:]] = argv[i + 1]; i += 2
        else:
            i += 1
    return verb, opts


def main(argv):
    verb, opts = _parse(argv)
    if verb is None:
        sys.stderr.write(opts + "\n")
        return 2
    if verb not in ("status", "propose", "apply"):
        sys.stderr.write("unknown verb: %s\n" % verb)
        return 2
    if not opts["root"] or not opts["ledger"] or not opts["action"]:
        sys.stderr.write("error: --root, --ledger and --action are required\n")
        return 2
    root = opts["root"]
    ledger_dir = opts["ledger"]
    action = opts["action"]
    sessions_dir = opts["sessions"] or os.path.join(root, ".harness", "sessions")
    settings_paths = _settings_stack(root, opts["settings"])
    if verb == "status":
        return cmd_status(root, ledger_dir, action, settings_paths, sessions_dir,
                          allowlist_path=opts["allowlist"])
    if verb == "propose":
        return cmd_propose(root, ledger_dir, action, settings_paths, sessions_dir,
                           allowlist_path=opts["allowlist"],
                           session=opts["session"])
    return cmd_apply(root, ledger_dir, action, settings_paths, sessions_dir,
                     allowlist_path=opts["allowlist"], session=opts["session"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
