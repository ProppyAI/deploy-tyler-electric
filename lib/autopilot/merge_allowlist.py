#!/usr/bin/env python3
"""lib/autopilot/merge_allowlist.py — fold the backbone hardened-allowlist into a
repo's .claude/settings.json at `harness update` / `harness-init` time.

The approval-hardening flywheel earns autonomy and ratifies it into the durable,
git-tracked backbone allowlist (lib/autopilot/hardened-allowlist.json). This
helper is the PROPAGATION step: it unions those earned rules into each consuming
repo's operator-owned permissions allowlist so an earned rule reaches EXISTING
repos (retroactive), not only newly-bootstrapped ones.

CLI:  python3 merge_allowlist.py <hardened-allowlist.json> <repo-settings.json>

Invariants (this helper ONLY ever ADDS):
  * Preserve every existing operator allow rule, in its existing order.
  * Append only the hardened rules that are not already present, in order.
  * Idempotent: running twice produces no duplicates.
  * Never remove, reorder, or rewrite operator rules.
  * FAIL-SAFE: a missing/garbage/empty hardened file is a no-op (exit 0) — a
    broken backbone artifact must never corrupt or DELETE a repo's settings.
  * If the repo settings file is missing, create a minimal
    {"permissions":{"allow":[...]}} with just the hardened rules.

stdlib only. Exit 0 on success or fail-safe no-op; exit 2 only on usage error.
"""
import json
import os
import sys
import tempfile


def _write_settings(settings, path):
    """Atomically write `settings` as indent-2 JSON (+ trailing newline) to `path`.
    Writes to a temp file in the SAME directory then os.replace()s it into place,
    so a kill/disk-full mid-write never leaves the live operator settings.json
    truncated or half-written. Mirrors lib/merge-pretooluse-hook.py."""
    dirn = os.path.dirname(path) or "."
    os.makedirs(dirn, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dirn, prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_hardened_rules(path):
    """Return the list of valid (non-empty string) hardened allow rules, or [] for
    any fail-safe condition (missing/garbage/wrong-shape). Never raises."""
    try:
        if not os.path.exists(path):
            return []
        with open(path) as f:
            data = json.load(f)
    except (ValueError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    allow = data.get("allow")
    if not isinstance(allow, list):
        return []
    rules = []
    for r in allow:
        if isinstance(r, str) and r.strip():
            rules.append(r)
    return rules


def _load_settings(path):
    """Load the repo settings dict. Missing -> minimal skeleton. A malformed
    existing settings file is a hard error (we must not silently overwrite
    operator data we failed to parse)."""
    if not os.path.exists(path):
        return {"permissions": {"allow": []}}
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("settings root is not a JSON object: %s" % path)
    perms = data.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
        data["permissions"] = perms
    if not isinstance(perms.get("allow"), list):
        perms["allow"] = []
    return data


def merge(hardened_path, settings_path):
    """Union hardened rules into settings.permissions.allow. Returns the number of
    rules newly added (0 means no-op). Only ever ADDS."""
    hardened_rules = _load_hardened_rules(hardened_path)
    if not hardened_rules:
        # Fail-safe / empty allowlist: never touch the repo settings.
        return 0

    settings = _load_settings(settings_path)
    allow = settings["permissions"]["allow"]
    # Preserve existing entries + order; track membership to dedup additions.
    seen = set()
    for r in allow:
        if isinstance(r, str):
            seen.add(r)

    added = 0
    for rule in hardened_rules:
        if rule not in seen:
            allow.append(rule)
            seen.add(rule)
            added += 1

    if added:
        _write_settings(settings, settings_path)
    return added


def main(argv):
    if len(argv) != 2:
        sys.stderr.write(
            "usage: merge_allowlist.py <hardened-allowlist.json> <repo-settings.json>\n")
        return 2
    hardened_path, settings_path = argv
    try:
        added = merge(hardened_path, settings_path)
    except (ValueError, OSError) as e:
        # Malformed repo settings (or unwritable target): do NOT corrupt it. Warn
        # and exit 0 so a propagation step is best-effort, never a hard failure.
        sys.stderr.write("merge_allowlist: skipped (%s)\n" % e)
        return 0
    if added:
        sys.stdout.write("merged %d hardened rule(s)\n" % added)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
