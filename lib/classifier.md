# HARNESS Intent Classifier

Given a plain language request from a developer, classify the intent.

## Input
- **Request text**: The developer's message
- **Repo context**: CLAUDE.md contents, top-level file listing, recent 10 git commits

## Output
Return JSON:
```json
{
  "intent": "feature|bug|trivial|refactor|question|review",
  "subcategory": "ui|data|infra|migration",
  "subcategory_confidence": 0.0-1.0,
  "confidence": 0.0-1.0,
  "reasoning": "Brief explanation of why this intent/subcategory was chosen",
  "branch_suggestion": "harness/feature/short-description"
}
```

`subcategory` and `subcategory_confidence` are REQUIRED when `intent == "feature"`. They are ABSENT for all other intents.

## Classification Rules

**trivial** (confidence usually > 0.9):
- Typo fixes, formatting changes
- Single-line config changes
- Updating a version number
- Adding/removing a comment
- Signal words: "fix typo", "update version", "change the name of"

**bug** (confidence varies):
- Something that used to work is broken
- Error messages, crashes, wrong output
- Signal words: "broken", "crash", "error", "not working", "wrong", "fails"
- If unclear WHICH bug, confidence should be low

**feature** (confidence varies):
- New functionality that doesn't exist yet
- Signal words: "add", "create", "build", "implement", "new"
- Complex requests with multiple components = feature
- If unclear WHAT to build, confidence should be low

**refactor** (confidence usually > 0.8):
- Restructuring without changing behavior
- Signal words: "split", "rename", "move", "reorganize", "clean up", "extract"

**question** (confidence usually > 0.9):
- Asking about how something works
- Signal words: "how does", "what is", "where is", "explain", "why does"
- No imperative verbs (no "fix", "add", "create")

**review** (confidence usually > 0.9):
- Asking for code review on existing work
- Signal words: "review", "check", "look at branch", "feedback on"
- References a specific branch or PR

## Feature Subcategory Rules

When `intent == "feature"`, also emit `subcategory` and `subcategory_confidence`.

### feature:ui

Request touches or implies touching any user-visible surface:

- Files under `client-app/src/app/dashboard/**`, `app/dashboard/**` (module repos), `login`, `auth/**`, landing sites, marketing pages.
- Signal words: "page", "button", "form", "screen", "dashboard", "login", "sign in", "modal", "redesign", "UI", "frontend".
- Any change matching the `contracts/customer-facing-changes.md` scope section.
- Tenant-visible outbound messages (Telegram templates, SOP message formatters, email templates) that ship visible text to a customer.

### feature:data

Backend-only work:

- API routes under `api/**` where the response is not rendered by dashboard code.
- Pure library code under `lib/**` with no route.
- Server-side data transforms, workers, background jobs.
- Signal words: "endpoint", "handler", "service", "worker", "library", "aggregator", "dedupe", "batch".

### feature:infra

CI/CD, hooks, deploy config, backbone contracts:

- `.github/workflows/**`, `bin/**`, `.harness/**` tooling.
- `contracts/**` (backbone), `templates/repo-bootstrap/**`.
- Git hooks, launchd agents, scheduled tasks.
- Signal words: "workflow", "CI", "hook", "deploy", "contract", "backbone", "template", "bootstrap".

### feature:migration

Schema changes and data transforms that modify persisted state:

- `supabase/migrations/**`, Alembic migrations, any `ALTER TABLE`/`CREATE TABLE` statement.
- One-off backfills and data-normalization scripts.
- Signal words: "migration", "schema", "column", "backfill", "ALTER", "add table".

## Subcategory Confidence Calibration

- **0.9+**: Unambiguous file path or signal word; single interpretation.
- **0.7-0.9**: Dominant signal with minor ambiguity.
- **0.5-0.7**: Unclear — classifier MUST ask operator inline, no silent default.
- **< 0.5**: Very ambiguous — classifier MUST ask operator and ALSO downgrade `confidence` on the outer intent.

## Confidence Gate

If `subcategory_confidence < 0.7`, the classifier MUST return the top-2 candidates ranked and surface an inline question to the operator (or the invoking harness caller). NEVER silently default. Rationale: a wrong chain runs to completion before the mistake is caught, and retro-fitting chain is expensive.

## Mixed-Shape Requests

A single PR that touches UI + backend + migration routes as `feature:ui` — the most-restrictive chain wins. Consistent with `contracts/customer-facing-changes.md` mixed-PR rule.

## Confidence Calibration
- 0.9+: Unambiguous, clear signal words, single interpretation
- 0.7-0.9: Likely correct but some ambiguity
- 0.5-0.7: Unclear, should ask for clarification
- < 0.5: Very ambiguous, must ask
