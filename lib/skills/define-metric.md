---
name: harness:define-metric
description: Drafts a per-session success metric file, sha-pins it on operator approval, and refuses to advance the chain until the metric is locked. Runs immediately after brainstorming on every feature:* chain.
argument-hint: <session_id>
---

# /harness:define-metric

The "define better together" protocol. Every feature session pins to a single
measurable success condition before any planning happens. This skill drafts
that condition, surfaces it to the operator for review, and locks it via a
git-blob-sha on approval.

Source: `docs/superpowers/specs/2026-04-29-define-better-metric-and-trace-audit-design.md` §2 + §3.

## When this fires

Immediately after `superpowers:brainstorming` on every feature chain
(`feature:ui`, `feature:data`, `feature:infra`, `feature:migration`). The
chain-manifest in `contracts/chain-manifest.json` is the source of truth.

`bug` and `refactor` chains do NOT run this skill. Bugs already have a
measurable success condition (the bug stops happening); refactors by definition
preserve behavior.

## Inputs

The skill reads:

- The current session's `brainstorming` output (transcript stored in the session
  record or referenced by path).
- The session's `intent` and `subcategory`.
- The session's branch name and original `request` text.
- The repo's `CLAUDE.md` (for project-level definitions of "good").

## Outputs

- `docs/superpowers/metrics/<session_id>.md` — the structured metric file
  matching the template in the next section.
- A patch to the session JSON adding two fields:
  - `metric_file: "docs/superpowers/metrics/<session_id>.md"`
  - `metric_sha: "<git-blob-sha-after-approval>"`

The canonical template lives in `docs/superpowers/templates/metric-template.md`.
Any future tooling that reads metric files MUST accept that shape; the inline
copy below is a copy-paste convenience for skill authors.

## Protocol — draft and lock

1. **Draft.** Subagent reads the brainstorming output + the original `request`
   text and **drafts** the metric file at `docs/superpowers/metrics/<session_id>.md`.
   The draft commit is allowed automatically because nothing is locked yet.

2. **Notify.** The session helper sets `status: "waiting-for-input"` and emits
   a notification to the operator. The notification path tries, in order:
   - PR comment via `gh pr comment <pr> --body "<metric draft summary + path>"`
     if a draft PR exists.
   - Telegram message via `harness comms send --user <chrys> --text "<summary>"`
     if the channel adapter is configured.
   - Falls back to writing the path + summary to a
     `.harness/sessions/<session_id>.notify` file the operator's terminal hook
     surfaces.

3. **Approve.** Operator runs `harness metric approve <session_id>`. The helper:
   - Computes `git hash-object docs/superpowers/metrics/<session_id>.md`.
   - Writes that sha to the session JSON as `metric_sha`.
   - Sets `status: "running"`.
   - Appends `define-metric` to `stages_completed`.

4. **Edit then approve.** If the operator wants changes, they edit the file
   in-place and re-run `harness metric approve <session_id>`. Same sha-pinning
   rule applies to the edited content.

5. **Reject.** `harness metric reject <session_id> --reason "<why>"` sets the
   session to `status: "needs-human"` and the chain stalls. No `define-metric`
   append happens.

6. **Lock.** Once `metric_sha` is pinned in the session JSON, **the metric is
   immutable for the session's lifetime**. Any subsequent edit to the file
   diverges from `metric_sha`, and `harness metric verify <session_id>` (run by
   `/pr-review` and by `gsd-ship`) reports a mismatch.

## Amendment after lock — `metric-amend:` trailer

If during execution the operator discovers the metric was wrong, the recovery
path mirrors `chain-bypass:`:

```
metric-amend:<session_id>:<reason>
```

In the PR body, combined with a second human approver and a follow-up issue
within 5 business days. `/pr-review` records the amendment in
`contracts/bypass-log.md` (existing log, new trailer family). Without an amend
trailer, an out-of-sha metric file is a hard-fail merge block.

## Operator commands

- `harness metric approve <session_id>` — locks the metric, sha-pins, advances
  the chain by appending `define-metric` to `stages_completed`.
- `harness metric reject <session_id> --reason "<why>"` — rejects the draft;
  session goes to `needs-human`; chain stalls.
- `harness metric verify <session_id>` — internal; sha-checks the file against
  `metric_sha`; used by `/pr-review` and `gsd-ship`.

A draft is implicit — the skill writes the draft as part of step 1 above; there
is no separate `harness metric draft` operator command (it exists internally as
`harness metric draft <session_id>`, invoked by this skill).

## Metric file template

The skill writes a draft matching this exact shape. Missing sections cause
auto-rejection at `harness metric verify` time.

```markdown
# Metric — <one-sentence success statement>

**Session:** <session_id>
**Branch:** <branch>
**Drafted by:** harness:define-metric subagent (<model>) at <iso8601>
**Approved by:** <git config user.name> at <iso8601> (sha <metric_sha>)

## Success metric

<One sentence. A single condition that will be either true or false at end of
chain. Must be machine-checkable OR have an explicit human-evaluation step
named below.>

## Measurement method

<How we will determine truth. Names a specific test file, query, manual
click-path, or human review step. If "manual" — name the human and the
screen.>

## Baseline

<What's true today, before this change. Numeric where possible. "Currently
the discovery checkboxes render disabled when supported=[]" not "the UI is
broken.">

## Target

<What will be true after the change. Same units as baseline. "All discovery
checkboxes render enabled when supported is empty" not "the UI works.">

## Gaming pre-mortem

<Three sentences MIN. Question Nate frames: how would a passing implementation
actually be wrong? Name at least one false-positive shape — what does an agent
gaming the metric look like, indistinguishable from the real fix? Examples:
  - "An agent could hard-code supported=['QBO_CUSTOMERS','QBO_INVOICES'] in
    the client to make checkboxes render enabled. Pre-mortem catch: the e2e
    fixture must include a tenant with empty supported and assert the
    empty-unexpected fallback message renders, not the enabled checkboxes."
  - "An agent could short-circuit the API to always return non-empty.
    Pre-mortem catch: a contract test asserts the API returns the response
    received from QBO without transformation."
This section is the single most important field. A short or hand-wavy gaming
pre-mortem is grounds for rejection at approval time.>

## Out-of-scope (NOT in this metric)

<Bulleted list. Things the operator might worry about but is explicitly NOT
asking the chain to solve here. Prevents scope creep mid-chain. Recovery
path: if scope creep is necessary, file a metric-amend trailer with a new
metric scope.>
```

## Authoring guidance for the drafting subagent

- Read the brainstorming transcript first. Identify the *one* thing the
  operator actually cared about and write it as a single sentence in
  `## Success metric`.
- Refuse to write a vague metric. "The UI works" is rejected; "All discovery
  checkboxes render enabled when supported is empty" is accepted. Ask for
  human input if the brainstorming output does not yield a falsifiable
  condition.
- The gaming pre-mortem is the load-bearing section. If you cannot enumerate
  at least one realistic path where a passing implementation is still wrong,
  the metric is too weak and you must request a tighter brainstorming round
  before drafting.
- For `feature:infra`, lightweight metrics are acceptable — "the new workflow
  runs on PR push and posts a comment" with a one-line gaming pre-mortem
  ("an agent could mock the workflow runner; pre-mortem catch: assert a real
  PR triggers an actual run") is fine. If observed friction stays high after
  5 infra chains we revisit.

## Acceptance criteria for the skill author

- Draft committed with all six required sections in the documented order.
- Notification fired through one of the three channels.
- Session JSON gains `metric_file` immediately; `metric_sha` only on operator
  approval.
- The chain does NOT advance past `define-metric` until `metric_sha` is set.

## See also

- Spec §2 + §3: `docs/superpowers/specs/2026-04-29-define-better-metric-and-trace-audit-design.md`
- Canonical template: `docs/superpowers/templates/metric-template.md`
- Chain manifest: `contracts/chain-manifest.json`
- Sister skill: `lib/skills/trace-audit.md` — verifies metric satisfaction at end of chain
- Trailer family precedent: `chain-bypass:` (router-extension), `contract-bypass:` (gates)
