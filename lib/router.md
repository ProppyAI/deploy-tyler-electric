# HARNESS Superpowers Router

Maps classified intents to superpowers skill chains.

## Routes

### trivial
No superpowers needed. Execute directly:
1. Make the change
2. Run any existing tests
3. Commit and open PR

### bug
1. `/superpowers:systematic-debugging` — find root cause
2. `/superpowers:verification-before-completion` — confirm fix works
3. Commit and open PR

### feature

When `intent == "feature"`, the classifier also emits `subcategory` (see `lib/classifier.md`). The router dispatches to one of four chains, each sourced from `contracts/chain-manifest.json` (canonical). The prose below mirrors the manifest for human readability; the manifest is the source of truth.

#### feature:ui

1. `superpowers:brainstorming`
2. `harness:define-metric`  (drafts metric file, awaits operator approval before advancing)
3. `frontend-design:frontend-design`
4. `ui-ux-pro-max:ui-ux-pro-max`
5. `harness:gstack-design-review`  (PR #120 — advisory until 2026-06-01; wraps `gstack:design-review` for outside-eyes UI/UX critique; auto-skip when gstack absent)
6. `harness:gstack-browse-smoke`  (PR #120 — advisory until 2026-06-01; wraps `gstack:browse` in smoke mode for headless reachability + console/network error check; auto-skip when gstack absent)
7. `superpowers:writing-plans`
8. `superpowers:test-driven-development` (real-data — contract + fixture tests, per `feedback_always_superpowers_and_real_data_tdd`)
9. `superpowers:executing-plans`
10. `ecc:e2e` (Playwright against G2 preview URL)
11. `superpowers:verification-before-completion`
12. `superpowers:requesting-code-review`
13. `harness:trace-audit`  (writes per-session evidence trace; verdict PASS required)
14. `gsd-ship`

#### feature:data

1. `superpowers:brainstorming`
2. `harness:define-metric`  (drafts metric file, awaits operator approval before advancing)
3. `superpowers:writing-plans`
4. `superpowers:test-driven-development` (real-data — contract + fixture tests)
5. `superpowers:executing-plans`
6. `superpowers:verification-before-completion`
7. `superpowers:requesting-code-review`
8. `harness:trace-audit`  (writes per-session evidence trace; verdict PASS required)
9. `gsd-ship`

#### feature:infra

1. `superpowers:brainstorming`
2. `harness:define-metric`  (drafts metric file, awaits operator approval before advancing)
3. `superpowers:writing-plans`
4. `superpowers:executing-plans`
5. `superpowers:requesting-code-review`
6. `harness:trace-audit`  (writes per-session evidence trace; verdict PASS required)
7. `gsd-ship`

#### feature:migration

1. `superpowers:brainstorming`
2. `harness:define-metric`  (drafts metric file, awaits operator approval before advancing)
3. `superpowers:writing-plans`
4. `superpowers:test-driven-development` (fixture tests against recorded pre/post state)
5. `superpowers:executing-plans`
6. `dry-run-on-staging` (shell-action — advisory until `harness migration dry-run` CLI ships)
7. `superpowers:verification-before-completion`
8. `superpowers:requesting-code-review`
9. `harness:trace-audit`  (writes per-session evidence trace; verdict PASS required)
10. `gsd-ship`

Each stage completion MUST be recorded via `harness_record_stage <short_name>` (see `lib/session.sh`). `/pr-review` refuses to approve a PR whose session's `stages_completed` is not equal in length and order to the session's `chain`. Bypass: `chain-bypass:<session_id>:<reason>` PR-body trailer plus a second approver.

`metric-amend:<session_id>:<reason>` is a separate trailer family that allows mid-chain edits to the metric file after the sha is pinned (second approver + 5-business-day follow-up issue, logged to `contracts/bypass-log.md`).

### refactor
1. `/superpowers:writing-plans` — plan the refactor
2. `/superpowers:executing-plans` — implement
3. `/superpowers:requesting-code-review` — self-review
4. Commit and open PR

### question
No superpowers. Read the codebase and respond directly.
No branch, no PR, no session tracking.

### review
1. `/superpowers:requesting-code-review` — review the specified branch
2. Report findings back to user

## Session Updates

Update `.harness/sessions/{id}.json` at each stage transition:
- classifying → brainstorming → planning → executing → reviewing → complete
- On error: status = "failed", error = "<message>"
- On user input needed: status = "waiting-for-input"

## Branch Naming

Format: `harness/<intent>/<short-description>`
Examples:
- `harness/feature/user-preferences-endpoint`
- `harness/bug/dashboard-wrong-totals`
- `harness/refactor/split-user-service`
- `harness/trivial/readme-typo`
