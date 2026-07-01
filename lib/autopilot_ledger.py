#!/usr/bin/env python3
"""lib/autopilot_ledger.py — Approval Ledger + graduation engine for HARNESS
Autopilot earned/graduated autonomy (Component L). Stdlib only.

Risk tiers: T0 read-only, T1 reversible-local, T2 shared-branch,
T3 external/irreversible (never auto-graduates). Unknown actions FAIL CLOSED to T3.

Threat model (read this before trusting the ledger):
  This is a SINGLE-OPERATOR tool. The Autopilot worker must itself write the
  ledger, so it inherently can write entries — full cryptographic
  non-repudiation against a *fully compromised agent* is OUT OF SCOPE. A worker
  that controls the ledger file can forge a self-consistent chain + anchor +
  matching session records (the test suite documents this boundary explicitly).
  We therefore do NOT add an HMAC: any key the worker can read cannot defend
  against the worker. The defense is DEFENSE-IN-DEPTH against accidental
  corruption, naive rollback, and unattributed approvals:
    1. chain-hash       — detects edits, reorders, middle-deletes (accidental
                          corruption / sloppy hand-editing).
    2. anchor file      — detects TAIL-TRUNCATION / rollback (deleting trailing
                          lines leaves a valid shorter chain otherwise — which
                          would silently re-graduate a demoted action).
    3. session gate     — a clean "approved" entry must resolve to a real,
                          BRANCH-MATCHED operator session record, or it earns no
                          streak credit (operator-attributability).
    4. operator veto    — operator-owned list OUTSIDE the agent-writable repo.
    5. conservative seed — source=seed entries never auto-graduate.
  NOTE: the ledger lives under .harness/ which HARNESS gitignores; integrity is
  provided by chain-hash + anchor (not git-tracking, since .harness/ is gitignored).

v1 intentionally blocks seed-sourced graduation for ALL tiers (including T0/T1),
not just T2+. The spec only requires blocking T2+; v1 is more conservative as
a deliberate choice to keep the graduation path simple: only source=operator
entries with verified sessions earn streak credit. Phase 2 can relax T0/T1 if
the operator wants a faster warm-up path.
"""
import sys
import json
import os
import re
import hashlib
import datetime
import fcntl

# action_type -> tier. Audited from the driver action vocabulary (Component L
# security req: anything with an external write surface or machine-level side
# effect is T3). Extend deliberately; novel keys are T3 until assigned.
TIER_MAP = {
    # T0 read-only
    "autopilot_doctor": "T0", "autopilot_status": "T0", "gh_pr_view": "T0",
    "read_file": "T0", "git_status": "T0",
    # T0 read-only (everyday interactive surface — added for capture, 2026-06-24)
    "gh_pr_list": "T0", "gh_pr_checks": "T0", "git_log": "T0", "git_diff": "T0",
    "glob_search": "T0", "grep_search": "T0",
    # T1 reversible-local
    "create_worktree": "T1", "commit_feature_branch": "T1",
    "write_spec_doc": "T1", "write_plan_doc": "T1", "remove_worktree": "T1",
    # T2 shared-branch
    "git_push": "T2", "open_pr_to_dev": "T2", "auto_merge_dev": "T2",
    # T3 external / irreversible (NEVER auto)
    "deploy_prod": "T3", "apply_migration": "T3", "qbo_write": "T3",
    "git_push_force": "T3", "git_push_force_with_lease": "T3",  # FIX D: explicit entry per exhaustive-audit req
    "force_delete_branch": "T3",
    "force_remove_worktree": "T3", "install_global_hook": "T3",
    "git_reset_hard_shared": "T3", "git_clean_fd": "T3", "set_secret": "T3",
    "netlify_env_set": "T3", "edit_backbone_manifest": "T3",
    "edit_contracts": "T3", "edit_pr_review_yml": "T3",
}

TIERS = ("T0", "T1", "T2", "T3")

# Maximum seed entries per action (robustness cap — prevents DoS from an
# inflated or malformed proposal file).
MAX_SEED_COUNT = 100

def classify(action_type):
    """Map an action_type to its tier. Fail closed: unknown/empty -> T3."""
    return TIER_MAP.get(action_type or "", "T3")

# Sentinel returned by read_entries when a line is not valid JSON, so verify can
# fail closed (TAMPERED) instead of raising an unhandled traceback (M4).
_MALFORMED = object()

def _ledger_path(ledger_dir):
    return os.path.join(ledger_dir, "ledger.jsonl")

def _anchor_path(ledger_dir):
    return os.path.join(ledger_dir, "ledger.anchor.json")

def _now_z():
    # Timezone-aware UTC, same ...Z wire shape as before (replaces deprecated
    # datetime.utcnow()).
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _current_git_branch():
    # Best-effort current branch for stamping ledger entries (C2). Returns "" if
    # not in a git repo or git is unavailable — callers may also pass --branch.
    import subprocess
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL)
        return out.decode("utf-8", "replace").strip()
    except (OSError, subprocess.CalledProcessError):
        return ""

def _utf8_open_0600(path):
    # Create/truncate a file with 0600 from the start (no world-readable race
    # window between open and chmod). Also enforce 0600 on pre-existing files
    # (fchmod after open so perms tighten even if the file already existed wider).
    # O_NOFOLLOW: refuse to follow a symlink on the final path component so a
    # symlink planted at the protected path cannot redirect the write (TOCTOU #3).
    # Returns a text-mode file object.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)  # H: tighten pre-existing files too (no TOCTOU)
    return os.fdopen(fd, "w")

def _canonical(entry):
    # Stable serialization of everything EXCEPT the hash field.
    body = {k: entry[k] for k in entry if k != "hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"))

def _entry_hash(prev_hash, entry):
    h = hashlib.sha256()
    h.update((prev_hash + "\n" + _canonical(entry)).encode("utf-8"))
    return h.hexdigest()

def read_entries(ledger_dir):
    """Parse ledger lines. A malformed (non-JSON) line, or a valid-JSON line
    whose parsed value is not a dict (e.g. [1,2,3], 42, "hello"), yields the
    _MALFORMED sentinel rather than the raw value, so verify_integrity can fail
    closed (M4). A non-dict value is NOT a valid ledger entry and downstream
    callers doing e["hash"] / e.get(...) would crash on it — treat it the same
    as an unparseable line (TAMPERED)."""
    p = _ledger_path(ledger_dir)
    if not os.path.exists(p):
        return []
    out = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except (ValueError, TypeError):
                out.append(_MALFORMED)
                continue
            if not isinstance(parsed, dict):
                out.append(_MALFORMED)
            else:
                out.append(parsed)
    return out

def _read_anchor(ledger_dir):
    p = _anchor_path(ledger_dir)
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            d = json.load(f)
        # C (fix): non-dict root (list, string, null, etc.) must not crash .get()
        if not isinstance(d, dict):
            return _MALFORMED
        return d
    except (ValueError, TypeError, OSError, AttributeError):
        return _MALFORMED

def _write_anchor(ledger_dir, count, last_seq, last_hash):
    # Anchor holds no secret, but keep perms tidy (0600) and atomic-create.
    with _utf8_open_0600(_anchor_path(ledger_dir)) as f:
        json.dump({"count": count, "last_seq": last_seq, "last_hash": last_hash},
                  f, sort_keys=True)

def append_entry(ledger_dir, action_type, decision, outcome, session, source,
                 pr=None, ts=None, branch=None):
    # Fix 3: cap action_type and session at write time to match the 256-char
    # guard in _session_valid and prevent ledger bloat / O(n·len) status work.
    if len(action_type) > 256:
        raise ValueError("action_type exceeds 256 chars (got %d)" % len(action_type))
    if len(session) > 256:
        raise ValueError("session exceeds 256 chars (got %d)" % len(session))
    if branch is not None and len(branch) > 256:
        raise ValueError("branch exceeds 256 chars (got %d)" % len(branch))
    # Refuse a symlinked ledger_dir: O_NOFOLLOW only guards the final path
    # component, so a symlinked directory would redirect every ledger write.
    if os.path.islink(ledger_dir):
        raise OSError("ledger_dir is a symlink (refusing to follow): %s" % ledger_dir)
    os.makedirs(ledger_dir, mode=0o700, exist_ok=True)  # Fix 6: 0700 not world-listable
    # E: serialize the read→compute→write→anchor sequence with an exclusive lock
    # so concurrent appends don't corrupt the hash chain.
    lock_path = os.path.join(ledger_dir, "ledger.lock")
    try:
        lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise OSError("ledger lock path is a symlink or unwritable (TOCTOU guard): %s" % e) from e
    os.fchmod(lock_fd, 0o600)  # FIX E: tighten pre-existing lock file (mode arg only applies on creation)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        entries = read_entries(ledger_dir)
        # A non-_MALFORMED dict can still lack "hash" (partial write / hand-edit).
        # Use .get(...) or GENESIS so append never crashes; the resulting chain
        # mismatch is then surfaced by verify_integrity (TAMPERED) — fail-safe.
        # Fix 1: compute seq and prev_hash from non-malformed entries only so a
        # _MALFORMED sentinel doesn't skew the ordinal or cause a GENESIS fallback
        # that compounds corruption (removing the bad line would still leave a
        # gap in the hash chain, correctly detected as TAMPERED).
        real = [e for e in entries if e is not _MALFORMED]
        prev_hash = (real[-1].get("hash") or "GENESIS") if real else "GENESIS"
        entry = {
            "seq": len(real) + 1,
            "ts": ts or _now_z(),
            "action_type": action_type,
            "tier": classify(action_type),
            "decision": decision,
            "outcome": outcome,
            "session": session,
            "branch": branch or "",
            "pr": pr,
            "source": source,
            "prev_hash": prev_hash,
        }
        entry["hash"] = _entry_hash(prev_hash, entry)
        lp = _ledger_path(ledger_dir)
        try:
            fd = os.open(lp, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        except OSError as _e:
            raise OSError("ledger path is a symlink or unwritable (TOCTOU guard): %s" % _e) from _e
        os.fchmod(fd, 0o600)  # H: tighten pre-existing files too
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
        # Update the anchor on EVERY append (C1: makes tail-truncation detectable —
        # a shorter valid chain no longer matches count/last_seq/last_hash).
        _write_anchor(ledger_dir, entry["seq"], entry["seq"], entry["hash"])
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    return entry

def verify_integrity(ledger_dir):
    """Return (ok: bool, bad_seqs: list). Recompute the hash chain AND check the
    anchor (count/last_seq/last_hash). Fails closed on:
      - any edited/reordered/middle-deleted entry (chain mismatch),
      - tail-truncation or append-without-anchor-update (anchor mismatch, C1),
      - a malformed (non-JSON) line (M4),
      - a missing/malformed anchor when entries exist (rollback of the anchor),
      - full ledger deletion while anchor still records count > 0 (rollback)."""
    bad = []
    prev = "GENESIS"
    entries = read_entries(ledger_dir)
    last_hash = "GENESIS"
    # Detect full ledger deletion: no entries but anchor says there should be some.
    if not entries:
        anchor = _read_anchor(ledger_dir)
        if anchor is _MALFORMED:
            # F: malformed anchor with empty ledger is also a failure.
            bad.append("anchor-malformed")
            return (False, bad)
        if anchor is not None:
            if anchor.get("count", 0) > 0:
                bad.append("ledger-missing")
                return (False, bad)
        # Genuinely empty (no anchor, or anchor count==0): clean slate.
        return (True, [])
    for e in entries:
        if e is _MALFORMED:
            bad.append("malformed-line")
            # Cannot continue the chain past an unparseable line.
            return (False, bad)
        if e.get("prev_hash") != prev or _entry_hash(prev, e) != e.get("hash"):
            bad.append(e.get("seq"))
        prev = e.get("hash", prev)
        last_hash = prev
    # Anchor check (only meaningful once at least one entry exists).
    anchor = _read_anchor(ledger_dir)
    last_seq = entries[-1].get("seq")
    if anchor is None or anchor is _MALFORMED:
        bad.append("anchor-missing")
    elif (anchor.get("count") != len(entries)
          or anchor.get("last_seq") != last_seq
          or anchor.get("last_hash") != last_hash):
        bad.append("anchor-mismatch")
    # Also verify seq matches ordinal position (finding 7).
    for idx, e in enumerate(entries):
        expected = idx + 1
        if e.get("seq") != expected:
            bad.append("seq-mismatch-%d" % expected)
    return (len(bad) == 0, bad)

# N consecutive clean operator approvals required to graduate, per tier.
THRESHOLDS = {"T0": 5, "T1": 5, "T2": 10, "T3": None}  # None => never auto

def _session_valid(entry, sessions_dir):
    """Resolve an entry's `session` to a real, BRANCH-MATCHED session record.

    A session is valid iff sessions_dir is provided AND
    <sessions_dir>/<session>.json exists, is valid JSON, AND its `branch` matches
    the entry's `branch` (C2).

    Legacy exemption ONLY when the `branch` KEY is ABSENT from the entry (genuine
    pre-C2 entries written before the branch field existed). When the key IS present
    (all code-written entries include it), we enforce the match. A present-but-empty
    branch="" on a clean (operator) entry is FAIL-CLOSED: it earns no streak credit
    (only an entry that genuinely predates C2 — absent key — gets the existence-only
    pass). This closes the C2 bypass where an operator-sourced entry recorded outside
    a git repo (branch resolves to "") would otherwise skip the branch-match. Note
    seed entries (source!="operator") never reach here — the _is_clean gate filters
    them before _session_valid is ever called — so this cannot affect seeding.
    """
    if sessions_dir is None:
        return True  # session check skipped (default / unit-test path)
    if sessions_dir == "":
        # An empty-string sessions_dir is NOT the skip sentinel — only None is.
        # Fail closed: an empty path can never resolve a real session record, and
        # a future API caller passing "" must not silently bypass the gate.
        return False
    sid = entry.get("session")
    if not sid:
        return False
    # Reject ids that could escape the sessions directory via path traversal.
    # Length cap: real HARNESS session ids are ~30 chars; >256 is never legitimate.
    if len(sid) > 256:
        return False
    if not re.match(r'^[A-Za-z0-9._-]+$', sid) or sid in (".", "..") or "/" in sid:
        return False
    p = os.path.join(sessions_dir, "%s.json" % sid)
    if not os.path.exists(p):
        return False
    try:
        with open(p) as f:
            rec = json.load(f)
    except (ValueError, TypeError, OSError):
        return False
    if "branch" in entry:
        # branch key is present — enforce C2 match for ALL code-written entries.
        # Only the ABSENT-key case is the genuine legacy exemption.
        entry_branch = entry.get("branch") or ""
        if entry_branch == "":
            # Key present but empty on a clean (operator) entry: FAIL CLOSED. Code
            # written entries always resolve a real branch when recorded in-repo;
            # an empty branch here means the C2 match cannot be performed, so we
            # deny streak credit rather than silently skipping the gate.
            sys.stderr.write(
                "WARN: ledger entry seq=%r action=%r session=%r has empty branch; "
                "C2 branch-match cannot be verified -> no streak credit\n"
                % (entry.get("seq"), entry.get("action_type"), entry.get("session")))
            return False
        rec_branch = rec.get("branch") or ""
        if rec_branch != entry_branch:
            return False
    # "branch" key absent entirely: genuine pre-C2 legacy entry — existence is sufficient.
    return True

def _is_clean(e):
    # Clean only if operator-approved AND succeeded. (Transient errors are the
    # driver's 3-strike concern and must be logged as outcome != failure to
    # avoid demoting here; this engine treats outcome=="success" as the only
    # clean signal and decision=="declined" or outcome=="failure" as breaking.)
    return e.get("source") == "operator" and e.get("decision") == "approved" \
        and e.get("outcome") == "success"

def _breaks_streak(e):
    return e.get("decision") == "declined" or e.get("outcome") == "failure"

def graduation_status(ledger_dir, veto=None, sessions_dir=None):
    veto = veto or {"vetoed": [], "demoted": []}
    # If the veto file exists but was unreadable, fail closed: deny all autonomy.
    veto_unreadable = veto.get("_unreadable", False)
    ok, _bad = verify_integrity(ledger_dir)
    entries = read_entries(ledger_dir)
    # Group by action_type, walk in order, compute current clean streak.
    streak, tier_of = {}, {}
    for e in entries:
        if e is _MALFORMED:
            # Integrity already failed (ok==False above), so nothing graduates;
            # just don't crash walking the streak.
            continue
        a = e.get("action_type", "")
        tier_of[a] = classify(a)  # always recompute from TIER_MAP; stored tier is audit-only
        if _is_clean(e):
            # Session-resolution gate (fold-in + C2 branch-match): when
            # sessions_dir is provided, a clean entry whose session does not
            # resolve to a real, branch-matched record is skipped with a warning
            # and neither advances nor breaks the streak (treated like a
            # non-operator entry).
            if not _session_valid(e, sessions_dir):
                sys.stderr.write(
                    "WARN: ledger entry seq=%r action=%r session=%r branch=%r "
                    "does not resolve to a real branch-matched session; "
                    "skipping (no streak credit)\n"
                    % (e.get("seq"), a, e.get("session"), e.get("branch")))
                continue
            streak[a] = streak.get(a, 0) + 1
        elif _breaks_streak(e):
            streak[a] = 0
        # pending/non-operator entries neither advance nor break
    out = {}
    for a in sorted(set(list(streak) + list(tier_of))):
        tier = tier_of.get(a, classify(a))
        thr = THRESHOLDS.get(tier)
        vetoed = (veto_unreadable or
                  a in veto.get("vetoed", []) or a in veto.get("demoted", []))
        auto = bool(ok) and (thr is not None) and (streak.get(a, 0) >= thr) and not vetoed
        out[a] = {"tier": tier, "clean_streak": streak.get(a, 0),
                  "threshold": thr, "vetoed": vetoed,
                  "integrity_ok": ok, "autonomous": auto}
    return out

def is_autonomous(ledger_dir, action_type, veto=None, sessions_dir=None):
    st = graduation_status(ledger_dir, veto=veto, sessions_dir=sessions_dir).get(action_type)
    return bool(st and st["autonomous"])

def _veto_path():
    # Operator-owned, OUTSIDE the agent-writable repo .harness/. Test override
    # via AUTOPILOT_VETO_FILE.
    return os.environ.get("AUTOPILOT_VETO_FILE",
                          os.path.expanduser("~/.harness/autopilot-veto.json"))

def _load_veto():
    p = _veto_path()
    if not os.path.exists(p):
        return {"vetoed": [], "demoted": []}
    try:
        with open(p) as f:
            d = json.load(f)
        # C: non-dict root (list, null, etc.) must not crash .get()
        if not isinstance(d, dict):
            raise AttributeError("veto file root is not a dict")
        return {"vetoed": list(d.get("vetoed", [])), "demoted": list(d.get("demoted", []))}
    except (ValueError, TypeError, OSError, AttributeError):
        # Fail CLOSED on an existing-but-unreadable veto file: previously-vetoed
        # actions must NOT silently become autonomous because the file is corrupt
        # or permissions changed. Return a sentinel that graduation_status checks.
        sys.stderr.write("WARN: veto file unreadable (failing closed): %r\n" % p)
        return {"vetoed": [], "demoted": [], "_unreadable": True}

def _save_veto(d):
    p = _veto_path()
    # G: only call makedirs when there is a non-empty parent directory.
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)  # Fix 6: 0700 not world-listable
    # Create 0600 atomically — no world-readable window between open and chmod
    # (I2). The veto file is the operator's authority surface.
    with _utf8_open_0600(p) as f:
        json.dump(d, f, sort_keys=True, indent=2)

def _seed_proposal_path(root):
    return os.path.join(root, ".harness", "autopilot", "seed-proposal.json")

def is_known_action(action_type):
    """True iff action_type is an EXPLICIT key in TIER_MAP, i.e. classify()
    resolves it to a deliberately-assigned tier rather than the unknown->T3
    fail-closed fallback. The seeder counts only known actions (and surfaces the
    rest under skipped_unknown) so the proposal never grants credit for a tier we
    inferred rather than audited."""
    return bool(action_type) and action_type in TIER_MAP

def _parse_uat_records(path):
    """Parse one `harness uat record` markdown file (lib/uat.sh format) into a
    list of per-step records. The real on-disk shape is:

        ## Step recorded <ts>
        - category: <name>
        - driver: <agent|operator>
        - observed: |
              <6-space-indented block scalar>
        <blank line>

    A record STARTS at each column-0 `## Step recorded`. Column-0 `- key: value`
    lines bind to the CURRENT record. `observed` is TERMINAL:

      * Once a record's `observed` field opens, NO further `- key:` line ever binds
        to that record. This closes the post-observed-close overwrite and the
        attribution-upgrade vectors (an appended column-0 `- category:`/`- driver:`
        cannot rewrite a recorded step).
      * While the observed block is open, EVERY line is payload and is ignored —
        column-0 `- key:` lines AND `## Step recorded` markers — so neither
        attribution nor a synthetic step boundary can be injected into the payload.
      * lib/uat.sh indents every payload line by 6 spaces, so the block terminates
        ONLY on a truly-empty column-0 line (`printf "\\n"`). A blank line WITHIN
        a multi-paragraph observed value is written as "      " (strips to "" but
        is space-prefixed) and correctly does NOT terminate the block. After the
        terminator, the next column-0 `## Step recorded` starts a new step.

    The irreducible residual (an agent that writes a byte-perfect fake operator
    step — a whole `## Step recorded` block at column 0) is the documented module
    threat boundary: seed entries earn no streak credit and the operator reviews
    the proposal before ratifying.

    Returns a list of dicts with whatever `- key: value` fields were present.
    """
    records = []
    cur = None
    in_observed = False
    try:
        with open(path) as f:
            for line in f:
                if in_observed:
                    # Inside the observed block: all content is payload. Ignore
                    # field lines AND `## Step recorded` markers. Terminate ONLY on
                    # a truly-empty column-0 line (the lib/uat.sh `printf "\n"`
                    # terminator); an indented blank ("      ") is payload, not a
                    # terminator, so multi-paragraph observed values are preserved.
                    if line.strip() == "" and not line.startswith((" ", "\t")):
                        in_observed = False
                    continue
                if line.startswith("## Step recorded"):
                    cur = {}
                    records.append(cur)
                    continue
                if cur is None:
                    continue  # header / prose before the first step: ignore
                # Only column-0 `- key: value` lines are record fields, and only
                # before this record's observed field opens.
                if line.startswith("- ") and ":" in line:
                    k, _, v = line[2:].partition(":")
                    k = k.strip()
                    v = v.strip()
                    if k == "observed":
                        # observed is terminal: record it, then enter the payload
                        # block so nothing further binds until the terminator.
                        cur["observed"] = v
                        in_observed = True
                    elif "observed" not in cur:
                        cur[k] = v
                    # a `- key:` once observed is set but block already closed:
                    # ignored (post-close overwrite guard).
    except OSError:
        return []
    return records

def seed_proposal(ledger_dir, root):
    """Mine ONLY operator-attributable evidence -> proposal counts; touches the
    ledger NOTHING (Component L "Seeded != live approval": the proposal is
    reviewable; nothing reaches the ledger until the operator ratifies).

    Source = the REAL `harness uat record` files under .personal/uat/*.md
    (lib/uat.sh). Each `## Step recorded` block is one record. A record
    contributes a count iff:
      - driver == "operator"  (operator-attributable; agent records are skipped),
      - it names an action via `category`, AND
      - that action is a KNOWN TIER_MAP key (classifier-validated).
    Unknown operator-named actions are surfaced under `skipped_unknown` (visible,
    not silently dropped). The real UAT format has NO machine-readable outcome
    field, so we do NOT mine success from prose — being conservative (mine little)
    is better than mining wrong. MUST NOT infer approvals from agent-authored
    sources (commit messages, MEMORY.md, traces): only operator-driver records.
    """
    counts = {}
    skipped_unknown = []
    seen_unknown = set()
    uat_dir = os.path.join(root, ".personal", "uat")
    if os.path.isdir(uat_dir):
        for name in sorted(os.listdir(uat_dir)):
            if not name.endswith(".md"):
                continue
            for rec in _parse_uat_records(os.path.join(uat_dir, name)):
                if rec.get("driver") != "operator":
                    continue  # operator-attributable only
                action = rec.get("category")
                if not action:
                    continue
                if is_known_action(action):
                    counts[action] = counts.get(action, 0) + 1
                elif action not in seen_unknown:
                    seen_unknown.add(action)
                    skipped_unknown.append(action)
    proposal = {
        "counts": counts,
        "skipped_unknown": sorted(skipped_unknown),
        "note": "operator-driver UAT records only; action=category validated "
                "against TIER_MAP; source=seed entries do NOT auto-graduate "
                "(Component L). No machine-readable outcome field exists in the "
                "real UAT format, so success is not mined from prose.",
    }
    os.makedirs(os.path.dirname(_seed_proposal_path(root)), mode=0o700, exist_ok=True)  # Fix 6: 0700 not world-listable
    # No secret here, but keep perms tidy + atomic-create (same as anchor/veto).
    with _utf8_open_0600(_seed_proposal_path(root)) as f:
        json.dump(proposal, f, sort_keys=True, indent=2)
    return proposal

def ratify_seed(ledger_dir, root):
    """One-shot ratify: append the reviewed proposal's counts to the ledger with
    source=seed, then CONSUME the proposal file so a second --approve cannot
    double-append (idempotency: the operator's review/audit surface must not be
    corrupted by an accidental re-run).

    Returns the number of entries written, or -1 if there was no proposal file to
    ratify (caller surfaces an error + non-zero exit).

    Seeded entries carry branch="" (append_entry default) so they never crash the
    C2 branch-match; and because source!="operator" they earn NO streak credit and
    can never auto-graduate (Component L).

    Fix 4: atomic claim via os.rename so concurrent double-approve cannot both
    read before either removes — the loser gets ENOENT and returns -1 cleanly."""
    p = _seed_proposal_path(root)
    if not os.path.exists(p):
        return -1
    # Atomically claim the proposal by renaming it to a pid-tagged temp name.
    # On POSIX, rename is atomic: exactly one concurrent caller wins; others
    # get ENOENT (the file is already gone) and return -1.
    claim = "%s.ratifying.%d" % (p, os.getpid())
    try:
        os.rename(p, claim)
    except OSError:
        # Either the file disappeared between the exists() check and rename
        # (another concurrent caller won), or a genuine FS error. Either way,
        # there is nothing for us to ratify.
        return -1
    try:
        with open(claim) as f:
            proposal = json.load(f)
    except (ValueError, TypeError, OSError):
        # A malformed proposal is not ratifiable; restore it so the operator
        # can inspect rather than silently consuming or appending garbage.
        try:
            os.rename(claim, p)
        except OSError:
            pass
        return -1
    # FIX A: a JSON root that is not a dict (list, str, number) loads fine but
    # then proposal.get("counts", {}) raises AttributeError which is NOT in the
    # except tuple above → escapes and orphans the .ratifying.<pid> claim.
    # Guard immediately after json.load and restore the claim just like the
    # malformed path does.
    if not isinstance(proposal, dict):
        try:
            os.rename(claim, p)
        except OSError:
            pass
        return -1
    n = 0
    # FIX B: wrap the per-action append loop so a mid-loop exception (e.g. an
    # unexpected OSError in append_entry) restores the claimed proposal rather
    # than orphaning the .ratifying.<pid> file.  Re-raise so the caller sees a
    # real failure — do NOT swallow append errors.
    try:
        for action, cnt in sorted(proposal.get("counts", {}).items()):
            # D: re-validate action against TIER_MAP before writing — the proposal
            # file is agent-writable so unknown action names must be rejected here.
            if not is_known_action(action):
                sys.stderr.write("WARN: ratify_seed: skipping unknown action %r\n" % action)
                continue
            try:
                cnt_int = int(cnt)
            except (ValueError, TypeError):
                continue  # skip non-integer counts
            if cnt_int <= 0:
                continue  # skip zero/negative counts
            cnt_int = min(cnt_int, MAX_SEED_COUNT)
            for i in range(cnt_int):
                append_entry(ledger_dir, action, "approved", "success",
                             "seed-%s-%d" % (action, i + 1), "seed")
                n += 1
    except Exception:
        # Mid-loop failure: restore the proposal so the operator can retry after
        # diagnosing the underlying error.  Re-raise so this surfaces to the caller.
        try:
            os.rename(claim, p)
        except OSError:
            pass
        raise
    # Consume the claimed file ONLY after a successful append pass (one-shot).
    try:
        os.remove(claim)
    except OSError:
        pass
    return n

def main(argv):
    if not argv:
        sys.stderr.write("usage: autopilot_ledger.py <classify|...> ...\n")
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "classify":
        action = rest[0] if rest else ""
        sys.stdout.write(classify(action) + "\n")
        return 0
    if cmd == "append":
        # append <ledger_dir> <action> <decision> <outcome> <session> \
        #        [source] [pr] [--branch BRANCH]
        # --branch captures the entry's branch for the C2 session branch-match.
        # If omitted, default to the current git branch (or "" when not in a
        # repo). Positional source/pr are parsed from the non-flag remainder.
        # Omitting [source] records source=agent (least-privileged default).
        # Operator attribution must be EXPLICIT: pass "operator" as the 6th arg.
        branch = None
        pos = []
        i = 0
        while i < len(rest):
            if rest[i] == "--branch":
                if i + 1 >= len(rest):
                    sys.stderr.write("usage: append ... --branch <branch>\n")
                    return 2
                branch = rest[i + 1]
                i += 2
            else:
                pos.append(rest[i]); i += 1
        if len(pos) < 5:
            sys.stderr.write(
                "usage: append <ledger_dir> <action> <decision> <outcome>"
                " <session> [source] [pr] [--branch B]\n")
            return 2
        ld, action, decision, outcome, session = pos[0], pos[1], pos[2], pos[3], pos[4]
        # Fix 3: validate length before calling append_entry so the CLI never
        # raises a traceback on oversized inputs (rc=2 with a clear message).
        if len(action) > 256:
            sys.stderr.write("error: action_type exceeds 256 chars (got %d)\n" % len(action))
            return 2
        if len(session) > 256:
            sys.stderr.write("error: session exceeds 256 chars (got %d)\n" % len(session))
            return 2
        # Validate enum fields — reject bad values rather than writing garbage.
        if decision not in ("approved", "declined"):
            sys.stderr.write(
                "error: decision must be 'approved' or 'declined', got %r\n" % decision)
            return 2
        if outcome not in ("success", "failure", "pending"):
            sys.stderr.write(
                "error: outcome must be 'success', 'failure', or 'pending', got %r\n" % outcome)
            return 2
        source = pos[5] if len(pos) > 5 else "agent"
        if source not in ("operator", "seed", "agent"):
            sys.stderr.write(
                "error: source must be 'operator', 'seed', or 'agent', got %r\n" % source)
            return 2
        pr = int(pos[6]) if len(pos) > 6 and pos[6].isdigit() else None
        # A: normalize explicit empty --branch "" to None so it resolves to the
        # real git branch rather than bypassing the C2 branch-match gate.
        if branch == "":
            branch = None
        if branch is None:
            branch = _current_git_branch()
        # Branch cap after resolution (empty-string normalization happens first).
        if branch is not None and len(branch) > 256:
            sys.stderr.write("error: branch exceeds 256 chars (got %d)\n" % len(branch))
            return 2
        append_entry(ld, action, decision, outcome, session, source, pr=pr, branch=branch)
        return 0
    if cmd == "count":
        if not rest:
            sys.stderr.write("usage: count <ledger_dir>\n")
            return 2
        sys.stdout.write(str(sum(1 for e in read_entries(rest[0]) if e is not _MALFORMED)) + "\n")
        return 0
    if cmd == "verify":
        if not rest:
            sys.stderr.write("usage: verify <ledger_dir>\n")
            return 2
        ok, _ = verify_integrity(rest[0])
        sys.stdout.write(("OK" if ok else "TAMPERED") + "\n")
        return 0 if ok else 1
    if cmd == "is-autonomous":
        if len(rest) < 2:
            sys.stderr.write("usage: is-autonomous <ledger_dir> <action>\n")
            return 2
        ld, action = rest[0], rest[1]
        veto = _load_veto()
        # Fix 7: warn callers that bare is-autonomous skips the C2 session gate;
        # production code should use is-autonomous-sess. The WARN goes to stderr
        # so it doesn't affect stdout-parsed callers and is suppressed by 2>/dev/null
        # in the many unit-test call sites that intentionally skip the session gate.
        sys.stderr.write(
            "WARN: is-autonomous skips the session gate; "
            "use is-autonomous-sess for the production autonomy decision\n")
        sys.stdout.write(("yes" if is_autonomous(ld, action, veto=veto) else "no") + "\n")
        return 0
    if cmd == "is-autonomous-sess":
        # is-autonomous-sess <ledger_dir> <action> <sessions_dir>
        if len(rest) < 3:
            sys.stderr.write("usage: is-autonomous-sess <ledger_dir> <action> <sessions_dir>\n")
            return 2
        ld, action, sess_dir = rest[0], rest[1], rest[2]
        veto = _load_veto()
        sys.stdout.write(
            ("yes" if is_autonomous(ld, action, veto=veto, sessions_dir=sess_dir) else "no") + "\n")
        return 0
    if cmd == "status":
        if not rest:
            sys.stderr.write("usage: status <ledger_dir> [sessions_dir]\n")
            return 2
        veto = _load_veto()
        sessions_dir = rest[1] if len(rest) > 1 else None
        st = graduation_status(rest[0], veto=veto, sessions_dir=sessions_dir)
        for a in sorted(st):
            r = st[a]
            # Fix 5: strip non-printable/control chars from the action column so a
            # forged ledger entry cannot emit terminal escape sequences via status.
            safe_a = "".join(c for c in a if c.isascii() and c.isprintable())
            sys.stdout.write("%-26s %s  streak=%s/%s  auto=%s%s\n" % (
                safe_a, r["tier"], r["clean_streak"], r["threshold"],
                "yes" if r["autonomous"] else "no",
                "  VETOED" if r["vetoed"] else ""))
        if not st:
            sys.stdout.write("(ledger empty)\n")
        return 0
    if cmd in ("veto", "demote", "unveto"):
        action = rest[0] if rest else ""
        # Guard: an empty action string must be rejected, not silently added to
        # (or removed from) the operator's veto list.
        if not action:
            sys.stderr.write("error: %s requires a non-empty <action>\n" % cmd)
            return 2
        d = _load_veto()
        # B: if the veto file was unreadable/corrupt, refuse to overwrite it
        # (we'd lose existing vetoes and persist the _unreadable sentinel).
        if d.get("_unreadable"):
            sys.stderr.write(
                "error: veto file unreadable/corrupt; fix or remove %s before "
                "modifying (refusing to overwrite and lose existing vetoes)\n"
                % _veto_path())
            return 2
        # `demote` writes to the `demoted` list; `veto` writes to `vetoed`.
        # Both block autonomy; `unveto` clears from both lists.
        key = "demoted" if cmd == "demote" else "vetoed"
        if cmd == "unveto":
            d["vetoed"] = [a for a in d.get("vetoed", []) if a != action]
            d["demoted"] = [a for a in d.get("demoted", []) if a != action]
        else:
            if action not in d.get(key, []):
                d.setdefault(key, []).append(action)
        _save_veto(d)
        sys.stdout.write("%s %s\n" % (cmd, action))
        return 0
    if cmd == "seed":
        # seed <ledger_dir> [--root ROOT] [--approve]
        if not rest:
            sys.stderr.write("usage: seed <ledger_dir> [--root ROOT] [--approve]\n")
            return 2
        ld = rest[0]
        root = "."
        approve = False
        i = 1
        while i < len(rest):
            if rest[i] == "--root":
                if i + 1 >= len(rest):
                    sys.stderr.write("usage: seed <ledger_dir> [--root ROOT] [--approve]\n")
                    return 2
                root = rest[i + 1]
                i += 2
            elif rest[i] == "--approve":
                approve = True
                i += 1
            else:
                i += 1
        if approve:
            n = ratify_seed(ld, root)
            if n < 0:
                sys.stderr.write(
                    "no seed proposal to ratify; run "
                    "`harness autopilot autonomy seed` first\n")
                return 1
            sys.stdout.write("ratified %d seed entries (source=seed)\n" % n)
        else:
            prop = seed_proposal(ld, root)
            sys.stdout.write(
                "seed proposal: %s (skipped_unknown: %s) (review %s, then --approve)\n" % (
                    json.dumps(prop["counts"]),
                    json.dumps(prop["skipped_unknown"]),
                    _seed_proposal_path(root)))
        return 0
    sys.stderr.write("unknown subcommand: %s\n" % cmd)
    return 2

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
