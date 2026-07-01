---
name: harness:gstack-design-review
description: Wraps gstack:design-review against a HARNESS UI prototype URL or path. Captures a UI/UX second opinion before code is written. Writes a HARNESS-shaped artifact at .harness/ui-review/<session_id>/design-review.json preserving gstack_session_id for audit. Runs in the feature:ui chain between ui-ux-pro-max and gstack-browse-smoke; advisory:true (SKIP does not block).
argument-hint: <prototype_url_or_path>
---

# /harness:gstack-design-review

The "outside-eyes UI/UX critique" checkpoint. Inserted at index 4 of `feature:ui`,
between `ui-ux-pro-max` (designer-mind first pass) and `gstack-browse-smoke`
(headless reachability smoke). Captures a structured second opinion on the
prototype *before* code is written — surfaces flow, hierarchy, accessibility,
and brand-fit issues at the cheapest possible moment to fix them.

Wraps the upstream `gstack:design-review` skill (installed at
`~/.claude/skills/gstack/design-review/SKILL.md`). The wrapper preserves the
upstream session marker (`~/.gstack/sessions/$PPID`) as `gstack_session_id` in
the HARNESS report — the load-bearing anti-stub field.

Source: `docs/superpowers/specs/2026-05-15-gstack-integration-design.md` §PR-3.

## Inputs

- `<prototype_url_or_path>` — either an HTTP URL the upstream skill can navigate
  to (preview Netlify deploy, local `vite dev` server, Figma published prototype)
  OR a filesystem path to a self-contained HTML prototype. The caller resolves
  this from the feature:ui session context before invoking the skill.

## Outputs

- `.harness/ui-review/<session_id>/design-review.json` in the HARNESS repo. Schema:

  ```json
  {
    "session_id": "<session_id>",
    "run_at": "<ISO-8601 UTC>",
    "target": "<prototype_url_or_path>",
    "verdict": "ok|concerns|fail|skip",
    "findings": [
      { "severity": "high|medium|low", "category": "flow|hierarchy|a11y|brand|copy", "note": "<text>" }
    ],
    "gstack_session_id": "<PPID from ~/.gstack/sessions/>",
    "skip_reason": "<string, present only on skip>"
  }
  ```

  `gstack_session_id` is the load-bearing anti-stub field — operator UAT on the
  first feature:ui PR asserts the corresponding `~/.gstack/sessions/<id>` file
  exists. A stub wrapper that fabricates findings without invoking
  `gstack:design-review` will not have a real session id.

## Pre-dispatch gate

```bash
# Input shape validation (defense-in-depth — $TARGET and $SESSION_ID flow
# into f-string path construction below). $SESSION_ID is the harness session
# id which is always slug-shaped; $TARGET is either http(s):// URL or an
# absolute filesystem path.
[[ "$SESSION_ID" =~ ^[a-zA-Z0-9._-]+$ ]] || { echo "ERROR: invalid SESSION_ID '$SESSION_ID'" >&2; exit 1; }
[[ "$TARGET" =~ ^(https?://|/).+ ]] || { echo "ERROR: TARGET must be http(s):// URL or absolute filesystem path, got '$TARGET'" >&2; exit 1; }

# gstack-absent fallback (universal rule per always-HARNESS spec)
if [[ ! -f "$HOME/.claude/skills/gstack/design-review/SKILL.md" ]]; then
  python3 -c "
import json, sys, os
from datetime import datetime, timezone
sid = sys.argv[1]
target = sys.argv[2]
out_dir = f'.harness/ui-review/{sid}'
os.makedirs(out_dir, exist_ok=True)
out_path = f'{out_dir}/design-review.json'
json.dump({
    'session_id': sid,
    'run_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'target': target,
    'verdict': 'skip',
    'findings': [],
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

# Skill tool dispatch — this is the canonical invocation. Do NOT shell out to
# any non-existent gstack/bin/* binary; the gstack pack ships SKILL.md files
# only (verified against ~/.claude/skills/gstack/SKILL.md AGENTS.md guidance).
Skill(skill="design-review", args="$TARGET")
```

When the Skill returns, resolve the session marker using a before/after diff of
`~/.gstack/sessions/`. Capture the session-file set before dispatch, compute
the set difference after, and select the new entry. On rare concurrent
invocations, fall back to most-recent-by-mtime.

```bash
# gstack writes ~/.gstack/sessions/$PPID on design-review start (PPID-only, no epoch suffix).
AFTER_SESSIONS=$(ls ~/.gstack/sessions/ 2>/dev/null | sort -u || true)
NEW_SESSIONS=$(comm -13 <(echo "$BEFORE_SESSIONS") <(echo "$AFTER_SESSIONS"))
NEW_COUNT=$(echo "$NEW_SESSIONS" | grep -c . 2>/dev/null || echo 0)

if [[ "$NEW_COUNT" -eq 1 ]]; then
  GSTACK_SID="$NEW_SESSIONS"
elif [[ "$NEW_COUNT" -gt 1 ]]; then
  # Multiple new sessions created during dispatch (concurrent gstack work on
  # the same machine). Pick the most recent by mtime as best-effort. Anti-stub
  # check will still resolve to a real gstack session file.
  GSTACK_SID=$(echo "$NEW_SESSIONS" | xargs -I {} stat -f '%m {}' ~/.gstack/sessions/{} 2>/dev/null | sort -nr | head -1 | awk '{print $2}')
else
  # No new session — gstack:design-review did not run (or wrote nothing). Mark
  # null; the verdict should already reflect this via SKIP or fail.
  GSTACK_SID=""
fi
```

## Result mapping

Parse the gstack:design-review report (it writes to `~/.gstack/design-review/`
per the upstream skill's documented output path) and extract:

- `verdict` — `ok` if zero high-severity findings; `concerns` if 1+ medium/high; `fail` if upstream skill reported a hard error; `skip` if upstream gated out.
- `findings` — list of `{severity, category, note}` triples mapped from the
  upstream report's structured findings (preserve severity verbatim).

Write the HARNESS-shaped artifact to `.harness/ui-review/<session_id>/design-review.json`
with all 5 required fields plus `gstack_session_id` (6 total non-optional fields).

## SKIP cases

| Scenario | Verdict | Block chain? |
|---|---|---|
| gstack not installed | skip | No (advisory:true; SKIP recorded in chain via auto-advance) |
| prototype URL unreachable | fail | No (advisory:true) — chain advances; verdict carries the signal (matches sibling browse-smoke for the same scenario) |
| upstream gstack:design-review errors | fail | No (advisory:true) — chain advances; verdict carries the signal |

Skip artifacts include the optional `skip_reason` field (string) explaining
why the review did not run. This field is absent on `ok`, `concerns`, and `fail` verdicts.

## Acceptance criteria

- Artifact at `.harness/ui-review/<session_id>/design-review.json` exists after invocation.
- All 5 required schema fields populated (or null for skip), plus `gstack_session_id`.
- `gstack_session_id` resolves to a file in `~/.gstack/sessions/` for non-skip verdicts.
- Skill never raises; SKIP is the universal fallback (mirrors canary-smoke).

## See also

- Spec §PR-3: `docs/superpowers/specs/2026-05-15-gstack-integration-design.md`
- Upstream gstack:design-review skill: `~/.claude/skills/gstack/design-review/SKILL.md`
- Sibling stage (runs immediately after this one): `lib/skills/gstack-browse-smoke.md`
- Pattern precedent: `lib/skills/canary-smoke.md` (PR-2, wraps gstack:canary)
