---
name: harness:gstack-browse-smoke
description: Wraps gstack:browse against a HARNESS UI prototype URL or path in smoke mode. Headless CDP single-pass verification that the prototype loads and primary navigation is reachable. Writes a HARNESS-shaped artifact at .harness/ui-review/<session_id>/browse-smoke.json preserving gstack_session_id for audit. Runs in the feature:ui chain between gstack-design-review and writing-plans; advisory:true (SKIP does not block).
argument-hint: <prototype_url_or_path>
---

# /harness:gstack-browse-smoke

The "does the prototype actually load and click?" checkpoint. Inserted at index
5 of `feature:ui`, between `gstack-design-review` (UI/UX critique) and
`writing-plans` (where the implementation plan is drafted). Captures a
headless CDP smoke against the prototype so reachability + console-error +
network-error issues surface before any production code is written.

Wraps the upstream `gstack:browse` skill (installed at
`~/.claude/skills/gstack/browse/SKILL.md`) in smoke mode — single-pass
navigation, primary-route verification. The wrapper preserves the upstream
session marker (`~/.gstack/sessions/$PPID`) as `gstack_session_id` in the
HARNESS report — the load-bearing anti-stub field.

Source: `docs/superpowers/specs/2026-05-15-gstack-integration-design.md` §PR-3.

## Inputs

- `<prototype_url_or_path>` — same shape as the sibling `gstack-design-review`
  stage. URL or filesystem path the upstream gstack:browse skill can navigate.

## Outputs

- `.harness/ui-review/<session_id>/browse-smoke.json` in the HARNESS repo. Schema:

  ```json
  {
    "session_id": "<session_id>",
    "run_at": "<ISO-8601 UTC>",
    "target": "<prototype_url_or_path>",
    "routes_checked": ["/"],
    "console_errors": <int|null>,
    "network_errors": <int|null>,
    "verdict": "ok|fail|skip",
    "gstack_session_id": "<PPID from ~/.gstack/sessions/>",
    "skip_reason": "<string, present only on skip>"
  }
  ```

  `gstack_session_id` is the load-bearing anti-stub field — operator UAT on the
  first feature:ui PR asserts the corresponding `~/.gstack/sessions/<id>` file
  exists. A stub wrapper that fabricates the report without invoking
  `gstack:browse` will not have a real session id.

## Pre-dispatch gate

```bash
# Input shape validation (defense-in-depth — $TARGET and $SESSION_ID flow
# into f-string path construction below). $SESSION_ID is the harness session
# id which is always slug-shaped; $TARGET is either http(s):// URL or an
# absolute filesystem path.
[[ "$SESSION_ID" =~ ^[a-zA-Z0-9._-]+$ ]] || { echo "ERROR: invalid SESSION_ID '$SESSION_ID'" >&2; exit 1; }
[[ "$TARGET" =~ ^(https?://|/).+ ]] || { echo "ERROR: TARGET must be http(s):// URL or absolute filesystem path, got '$TARGET'" >&2; exit 1; }

# gstack-absent fallback (universal rule per always-HARNESS spec)
if [[ ! -f "$HOME/.claude/skills/gstack/browse/SKILL.md" ]]; then
  python3 -c "
import json, sys, os
from datetime import datetime, timezone
sid = sys.argv[1]
target = sys.argv[2]
out_dir = f'.harness/ui-review/{sid}'
os.makedirs(out_dir, exist_ok=True)
out_path = f'{out_dir}/browse-smoke.json'
json.dump({
    'session_id': sid,
    'run_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'target': target,
    'routes_checked': [],
    'console_errors': None,
    'network_errors': None,
    'verdict': 'skip',
    'gstack_session_id': None,
    'skip_reason': 'gstack not installed'
}, open(out_path, 'w'), indent=2)
print(f'SKIP (gstack not installed) — wrote {out_path}')
" "$SESSION_ID" "$TARGET"
  exit 0
fi
```

## Invocation

```bash
# Capture the session-file set before dispatch so we can identify the new entry.
BEFORE_SESSIONS=$(ls ~/.gstack/sessions/ 2>/dev/null | sort -u || true)

# Skill tool dispatch — this is the canonical invocation. Smoke semantics
# (single-pass: navigate to $TARGET, capture console + network errors,
# verify primary route loads, exit) come from gstack:browse's default
# behavior — the upstream skill does NOT define a --smoke flag (verified
# against ~/.claude/skills/gstack/browse/SKILL.md frontmatter and
# arguments). Per feedback_subagents_invent_cli_flags_when_wrapping_external_tools,
# pass only $TARGET. Do NOT shell out to any non-existent gstack/bin/*
# binary; the gstack pack ships SKILL.md files only.
Skill(skill="browse", args="$TARGET")
```

The HARNESS schema doesn't depend on a specific upstream flag — it depends
on the session-marker contract (presence of `~/.gstack/sessions/$PPID`
after dispatch).

When the Skill returns, resolve the session marker using a before/after diff
of `~/.gstack/sessions/`:

```bash
# gstack writes ~/.gstack/sessions/$PPID on browse start (PPID-only, no epoch suffix).
AFTER_SESSIONS=$(ls ~/.gstack/sessions/ 2>/dev/null | sort -u || true)
NEW_SESSIONS=$(comm -13 <(echo "$BEFORE_SESSIONS") <(echo "$AFTER_SESSIONS"))
NEW_COUNT=$(echo "$NEW_SESSIONS" | grep -c . 2>/dev/null || echo 0)

if [[ "$NEW_COUNT" -eq 1 ]]; then
  GSTACK_SID="$NEW_SESSIONS"
elif [[ "$NEW_COUNT" -gt 1 ]]; then
  GSTACK_SID=$(echo "$NEW_SESSIONS" | xargs -I {} stat -f '%m {}' ~/.gstack/sessions/{} 2>/dev/null | sort -nr | head -1 | awk '{print $2}')
else
  GSTACK_SID=""
fi
```

## Result mapping

Parse the gstack:browse report and extract:

- `routes_checked` — list of routes the smoke pass touched (typically just `/` in smoke mode).
- `console_errors` — count of console errors captured during the pass.
- `network_errors` — count of non-2xx HTTP responses for primary route resources.
- `verdict` — `ok` if zero console + zero network errors; `fail` otherwise.

Write the HARNESS-shaped artifact to `.harness/ui-review/<session_id>/browse-smoke.json`
with all 7 required fields plus `gstack_session_id`.

## SKIP cases

| Scenario | Verdict | Block chain? |
|---|---|---|
| gstack not installed | skip | No (advisory:true; SKIP recorded in chain via auto-advance) |
| prototype URL unreachable (DNS/TLS) | fail | No (advisory:true) — chain advances; review-time visibility |
| upstream gstack:browse errors | fail | No (advisory:true) — chain advances; verdict carries the signal |

Skip artifacts include the optional `skip_reason` field (string). Absent on
`ok` and `fail` verdicts.

## Acceptance criteria

- Artifact at `.harness/ui-review/<session_id>/browse-smoke.json` exists after invocation.
- All 7 required schema fields populated (or null for skip).
- `gstack_session_id` resolves to a file in `~/.gstack/sessions/` for non-skip verdicts.
- Skill never raises; SKIP is the universal fallback.

## See also

- Spec §PR-3: `docs/superpowers/specs/2026-05-15-gstack-integration-design.md`
- Upstream gstack:browse skill: `~/.claude/skills/gstack/browse/SKILL.md`
- Sibling stage (runs immediately before this one): `lib/skills/gstack-design-review.md`
- Pattern precedent: `lib/skills/canary-smoke.md` (PR-2, wraps gstack:canary)
