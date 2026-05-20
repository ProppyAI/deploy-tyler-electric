---
name: harness:canary-smoke
description: Wraps gstack:canary against a tenant's live deploy URL. Resolves URL from harness.json:deployment.subdomain. Writes a HARNESS-shaped artifact at .harness/canary/<tenant>/<date>.json preserving gstack_session_id for audit. Runs in the feature:infra and feature:migration chains; advisory:true (SKIP does not block).
argument-hint: <tenant_name>
---

# /harness:canary-smoke

The "did the live tenant just break?" checkpoint. Inserted between
`executing-plans` and `requesting-code-review` on `feature:infra`, and
between `executing-plans` and `dry-run-on-staging` on `feature:migration`.
Captures a post-implementation snapshot of the tenant's live state so
drift since the last deploy is visible before review.

Wraps `gstack:canary` in `--quick` (single-pass) mode by default; daily
cron uses default duration.

Source: `docs/superpowers/specs/2026-05-15-gstack-integration-design.md` §PR-2.

## Inputs

- `<tenant_name>` — directory name of the tenant repo under the operator's workspace
  (typically `tyler-electric`). The caller (bin/harness canary or
  scripts/canary-tenant-daily.sh) resolves `$TENANT_REPO` to the tenant repo
  path and exports it (along with `$URL` and `$HARNESS_ROOT`) into the
  subprocess environment before invoking this skill; the default is
  `../<tenant_name>` relative to HARNESS. The caller also `cd`s into
  `$HARNESS_ROOT` so the skill's relative `.harness/canary/<tenant>/<date>.json`
  write lands at the absolute path the caller computed.

## Outputs

- `.harness/canary/<tenant_name>/<YYYY-MM-DD>.json` in the HARNESS repo. Schema:

  ```json
  {
    "tenant": "<tenant_name>",
    "run_at": "<ISO-8601 UTC>",
    "url": "https://<subdomain>",
    "endpoints_checked": ["/"],
    "latency_p95_ms": <int|null>,
    "errors": [<list of strings; empty when verdict ok>],
    "verdict": "ok|fail|skip",
    "gstack_session_id": "<PPID from ~/.gstack/sessions/>"
  }
  ```

  `gstack_session_id` is the load-bearing audit field — measurement step 4 of the metric
  asserts the corresponding `~/.gstack/sessions/<id>` file exists. A stub canary that
  fakes the report file without invoking gstack will not have a real session id.

## Pre-dispatch gate

```bash
# gstack-absent fallback (universal rule per always-HARNESS spec)
if [[ ! -f "$HOME/.claude/skills/gstack/SKILL.md" ]]; then
  python3 -c "
import json, sys, os
from datetime import datetime, timezone
tenant = sys.argv[1]
out_dir = f'.harness/canary/{tenant}'
os.makedirs(out_dir, exist_ok=True)
date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
out_path = f'{out_dir}/{date}.json'
json.dump({
    'tenant': tenant,
    'run_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'url': None,
    'endpoints_checked': [],
    'latency_p95_ms': None,
    'errors': [],
    'verdict': 'skip',
    'gstack_session_id': None,
    'skip_reason': 'gstack not installed'
}, open(out_path, 'w'), indent=2)
print(f'SKIP (gstack not installed) — wrote {out_path}')
" "$TENANT"
  exit 0
fi
```

## Invocation

```bash
# Resolve URL from harness.json:deployment.subdomain
URL=$(python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
sub = d['deployment']['subdomain']
print(f'https://{sub}')
" "$TENANT_REPO/harness.json")

# Invoke gstack:canary via Skill tool. The skill writes its own session marker
# to ~/.gstack/sessions/$PPID on dispatch — capture that as gstack_session_id.
# Default duration; --quick for single-pass.

# Skill tool dispatch (this is the canonical invocation; do NOT shell out to
# a non-existent gstack/bin/gstack binary — there is no such top-level binary,
# only gstack-*-prefixed helpers).

# Capture the session-file set before dispatch so we can identify the new entry.
BEFORE_SESSIONS=$(ls ~/.gstack/sessions/ 2>/dev/null | sort -u || true)

Skill(skill="canary", args="$URL --quick")
```

When the Skill returns, resolve the session marker using a before/after diff of
`~/.gstack/sessions/`. Capture the session-file set before dispatch, compute
the set difference after, and select the new entry. On rare concurrent
invocations, fall back to most-recent-by-mtime.

```bash
# gstack writes ~/.gstack/sessions/$PPID on canary start (PPID-only, no epoch suffix).
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
  # No new session — gstack:canary did not run (or wrote nothing). Mark null;
  # the verdict should already reflect this via SKIP or fail.
  GSTACK_SID=""
fi
```

## Result mapping

Parse the gstack:canary report (it writes `.gstack/canary-reports/<date>/`) and
extract:
- `endpoints_checked` — list of URLs probed
- `latency_p95_ms` — from gstack's `perf` output (page load time p95 across pages)
- `errors` — combined `console --errors` count + non-200 status codes
- `verdict` — `ok` if zero errors and all pages 200; `fail` otherwise

Write the HARNESS-shaped artifact to `.harness/canary/<tenant>/<date>.json` with all 8 required fields (`tenant`, `run_at`, `url`, `endpoints_checked`, `latency_p95_ms`, `errors`, `verdict`, `gstack_session_id`).

## SKIP cases

| Scenario | Verdict | Block chain? |
|---|---|---|
| gstack not installed | skip | No (advisory:true; SKIP recorded in chain via auto-advance) |
| tenant `harness.json` missing | skip | No (operator misconfiguration; surface in report) |
| tenant `deployment.subdomain` missing | skip | No (incomplete tenant config) |
| URL unreachable (DNS/TLS) | fail | No directly — verdict is fail but chain advances. Daily cron picks it up; review-time visibility. |

**Skip artifacts** add 1 optional field — `skip_reason` (string) — explaining
why the canary did not run. This field is absent on `ok` and `fail` verdicts.
Total: 8 required + 1 optional = 9 fields on skip artifacts.

## Acceptance criteria

- Artifact at `.harness/canary/<tenant>/<date>.json` exists after invocation.
- All 8 required schema fields populated (or null for skip).
- `gstack_session_id` resolves to a file in `~/.gstack/sessions/` for non-skip verdicts.
- Skill never raises; SKIP is the universal fallback.

## See also

- Spec §PR-2: `docs/superpowers/specs/2026-05-15-gstack-integration-design.md`
- gstack:canary skill: `~/.claude/skills/gstack/canary/SKILL.md`
- Tenant cron entry: `templates/repo-bootstrap/harness.json` (default `enabled: false`).
  NOTE: `harness cron run canary-tenant-daily` does NOT work — `cron_manager.py` only reads
  `module.harness.json`, not `harness.json`. To activate the daily cron, add a shell-level
  crontab entry pointing directly at `scripts/canary-tenant-daily.sh <tenant>`, or run
  `harness canary run <tenant>` manually. The `harness.json` cron block is a future-state
  placeholder for a cron-registrar not yet implemented (post-PR-3 work).
- Cron driver: `scripts/canary-tenant-daily.sh`
- Operator CLI: `harness canary run <tenant>` (`bin/harness:cmd_canary`)
