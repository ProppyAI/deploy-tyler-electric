# Metric Template (canonical)

> Source of truth for `harness:define-metric`. Any future tooling that reads metric files must accept this shape. See `docs/superpowers/specs/2026-04-29-define-better-metric-and-trace-audit-design.md` §3 for rationale.

```markdown
# Metric — <one-sentence success statement>

**Session:** <session_id>
**Branch:** <branch>
**Drafted by:** harness:define-metric subagent (<model>) at <iso8601>
**Approved by:** <git config user.name> at <iso8601 UTC>

> The pinned `metric_sha` lives in the session JSON (`session.metric_sha`), accessible via `./bin/harness metric verify <session_id>`. The sha is intentionally NOT embedded in this file to avoid a chicken-and-egg with `git hash-object` (the file content includes the certificate, which means the sha is computed AFTER the certificate is written; embedding it would change the sha again).

## Success metric

<One sentence. A single condition that will be either true or false at end of chain. Must be machine-checkable OR have an explicit human-evaluation step named below.>

## Measurement method

<How we will determine truth. Names a specific test file, query, manual click-path, or human review step. If "manual" — name the human and the screen.>

## Baseline

<What's true today, before this change. Numeric where possible. "Currently the discovery checkboxes render disabled when supported=[]" not "the UI is broken.">

## Target

<What will be true after the change. Same units as baseline. "All discovery checkboxes render enabled when supported is empty" not "the UI works.">

## Gaming pre-mortem

<Three sentences MIN. Question Nate frames: how would a passing implementation actually be wrong? Name at least one false-positive shape — what does an agent gaming the metric look like, indistinguishable from the real fix? Examples:
  - "An agent could hard-code supported=['QBO_CUSTOMERS','QBO_INVOICES'] in the client to make checkboxes render enabled. Pre-mortem catch: the e2e fixture must include a tenant with empty supported and assert the empty-unexpected fallback message renders, not the enabled checkboxes."
  - "An agent could short-circuit the API to always return non-empty. Pre-mortem catch: a contract test asserts the API returns the response received from QBO without transformation."
This section is the single most important field. A short or hand-wavy gaming pre-mortem is grounds for rejection at approval time.>

## Out-of-scope (NOT in this metric)

<Bulleted list. Things the operator might worry about but is explicitly NOT asking the chain to solve here. Prevents scope creep mid-chain. Recovery path: if scope creep is necessary, file a metric-amend trailer with a new metric scope.>
```

## Required sections (in order)

1. `## Success metric`
2. `## Measurement method`
3. `## Baseline`
4. `## Target`
5. `## Gaming pre-mortem`
6. `## Out-of-scope (NOT in this metric)`

Missing any section causes auto-rejection at `harness metric verify` time.

## See also

- Skill: `lib/skills/define-metric.md`
- Spec §3: `docs/superpowers/specs/2026-04-29-define-better-metric-and-trace-audit-design.md`
- Sister template: `docs/superpowers/templates/trace-template.md`
