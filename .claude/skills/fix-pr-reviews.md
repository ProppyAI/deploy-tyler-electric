---
name: fix-pr-reviews
description: Autonomous PR review fix loop — reads bot reviews, fixes real issues, pushes, loops until the bot has nothing addressable left. Uses mempalace KG to learn noise patterns across sessions.
---

# Fix PR Reviews

Autonomous loop that fixes bot PR review findings until the bot has no new addressable findings.

## Arguments

`$ARGUMENTS` is the PR number (required). Optional flags parsed from the argument string:
- `--repo <owner/repo>` — target repo (default: current repo)

## Instructions

Parse the PR number and optional flags from: $ARGUMENTS

### Setup

1. Determine the repo: if `--repo` was provided, use it. Otherwise run `gh repo view --json nameWithOwner -q .nameWithOwner`.
2. Run `gh pr view $PR_NUMBER --json title,body,headRefName,baseRefName` to get PR context. The **title and body describe the original intent** of the PR. Every fix must preserve this intent. Save this for reference throughout the loop.
3. Checkout the PR branch: `git fetch origin <headRefName> && git checkout <headRefName> && git pull origin <headRefName>`
4. Note the current timestamp as `LOOP_START_TIME`.

### The Loop

Each round follows these steps. The loop has no round limit — it runs until there are no new REAL findings to fix.

**Step 1 — READ the latest review:**

```bash
# Get the most recent review
gh api repos/{owner}/{repo}/pulls/{pr_number}/reviews --jq '.[-1]'

# Get review comments (inline comments on code)
gh api repos/{owner}/{repo}/pulls/{pr_number}/comments --jq 'sort_by(.created_at)'

# Get general PR comments (top-level discussion)
gh api repos/{owner}/{repo}/issues/{pr_number}/comments --jq 'sort_by(.created_at)'
```

For rounds 2+, only look at comments created AFTER the last push timestamp. If no new review comments exist after the last push, the PR is clean — exit the loop.

**Step 2 — RECALL from mempalace:**

Query mempalace for known patterns. Use the MCP tools if available, otherwise fall back to CLI:

```bash
# Search for noise patterns
mempalace search "review noise patterns" --wing harness

# If MCP tools are available, also query:
# mempalace_kg_query entity="bot-reviewer"
# mempalace_kg_query entity="review-noise"
```

Use the results to inform classification. If a finding matches a known noise pattern from the KG (e.g., "theoretical-injection-in-heredocs", "cosmetic-style-issues"), lean toward classifying it as NOISE — but still verify against the actual code.

**Step 3 — CLASSIFY each finding:**

For each review comment, classify as:

**REAL** (will fix):
- Breaks functionality or causes runtime errors
- Actual bugs — logic errors, off-by-one, null dereference
- Missing error handling that would cause crashes in production
- Type errors or interface mismatches
- Security issues that are actually exploitable in context
- Violations of the project's own CLAUDE.md standards

**NOISE** (will skip):
- Cosmetic style preferences not in CLAUDE.md
- Repeated bot fixations on the same pattern across rounds
- LOW/INFO severity findings (except security — see below)
- Suggestions that would add unnecessary complexity (YAGNI)
- Suggestions that would change the PR's original intent
- Findings that match known KG noise patterns (except security — see below)

**SECURITY findings require extra scrutiny.** Never auto-dismiss a security finding solely because it matches a KG noise pattern. For each security finding, verify independently that the mitigation the KG claims exists actually exists in the current code. Only skip security findings when you can point to the specific line that mitigates the issue. When writing security skips to the KG, tag them as `security_skip` so they don't self-reinforce into permanent blindness.

Print the classification clearly:
```
=== Round N Classification ===
REAL (fixing):
  - [file:line] description
  - [file:line] description
NOISE (skipping):
  - [file:line] description — reason: [why this is noise]
```

**Step 4 — EXIT CHECK:**

If there are no REAL findings → exit the loop and proceed to the summary.

If there ARE REAL findings → continue to Step 5.

**Step 5 — FIX real findings:**

Use the `receiving-code-review` skill pattern:
- Fix each REAL finding
- After fixing, verify: run any available tests, lint, type-check for the project
- Do NOT fix NOISE findings — do not touch them
- Do NOT introduce changes beyond what the finding requires
- Do NOT add docstrings, comments, or refactoring beyond the fix
- After every fix, compare against the PR title/body from Setup — if a fix would drift from the original intent, flag it and skip

**Step 6 — PUSH:**

```bash
PUSH_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
git add <specific files that were changed>
git commit -m "fix: address round-$N review — $(brief comma-separated list of fixes)"
git push origin HEAD
```

Note `PUSH_TIME` was captured **before** the push to avoid missing fast bot reviews.

**Step 7 — RECORD to mempalace:**

Write to the knowledge graph after each round. Use MCP tools if available, otherwise note for manual entry:

For each REAL finding that was fixed:
```
KG add: subject="PR-{number}" predicate="round_{N}_fixed" object="{brief description}" valid_from="{today}"
```

For each NOISE finding that was skipped:
```
KG add: subject="PR-{number}" predicate="round_{N}_skipped" object="{brief description}: {reason}" valid_from="{today}"
```

**Step 8 — WAIT for re-review:**

Wait for the bot to re-review after push. **Important:** Wait for CI checks to complete, not just for new comments — comments from a *previous* CI run may arrive after the push and cause false detection.

1. Poll `gh pr checks` until Code Review and Security Review are no longer "pending" (run via Bash with `run_in_background`):

```bash
TIMED_OUT=1
for i in $(seq 1 20); do
  sleep 30
  CHECKS=$(gh pr checks {pr_number} 2>&1)
  PENDING=$(echo "$CHECKS" | grep -c "pending" || true)
  if [ "$PENDING" -eq "0" ]; then
    echo "All checks completed after $((i * 30)) seconds"
    TIMED_OUT=0
    break
  fi
  echo "Waiting for checks... ($((i * 30))s) — $PENDING still pending"
done
if [ "$TIMED_OUT" -eq 1 ]; then
  echo "TIMEOUT — checks did not complete within 10 minutes"
fi
```

If the poll times out → exit the loop and proceed to the summary (let the user decide).

2. Read new comments (only after checks complete):

```bash
PUSH_TIME="{push_time_from_step_6}"
NEW_INLINE=$(gh api repos/{owner}/{repo}/pulls/{pr_number}/comments \
  --jq "[.[] | select(.created_at > \"$PUSH_TIME\")] | length")
NEW_ISSUE=$(gh api repos/{owner}/{repo}/issues/{pr_number}/comments \
  --jq "[.[] | select(.created_at > \"$PUSH_TIME\")] | length")
echo "New comments: $NEW_INLINE inline, $NEW_ISSUE top-level"
```

3. If NEW_INLINE = 0, also read the latest top-level summary comment for HIGH+ items:

```bash
PUSH_TIME="{push_time_from_step_6}"
gh api repos/{owner}/{repo}/issues/{pr_number}/comments \
  --jq "[.[] | select(.created_at > \"$PUSH_TIME\") | select(.body | test(\"Findings Require Attention\"))] | .[-1].body"
```

**Decision logic** (you make this decision, not bash):
- If new inline comments exist (NEW_INLINE > 0) → **continue to next round**
- If no new inline comments but the summary table has HIGH or CRITICAL items → **read the full summaries**, classify each, and fix real ones. Continue to next round.
- If no new inline comments and summary table has only MEDIUM/LOW → **classify them**. If any are REAL, fix and continue. If all are NOISE, exit the loop.
- If no new inline or top-level comments at all → **exit the loop** (PR is clean)

→ Go back to Step 1 for the next round.

### Summary

After exiting the loop (for any reason):

1. **Record final outcome to KG:**
```
KG add: subject="PR-{number}" predicate="resolved_in" object="{N} rounds" valid_from="{today}"
KG add: subject="PR-{number}" predicate="outcome" object="{clean|timeout|needs_human}" valid_from="{today}"
```

2. **File a mempalace drawer** in the `harness` wing / `general` room with the full narrative:
- PR number and title
- Number of rounds
- For each round: what was fixed, what was skipped and why
- Final outcome and recommendation
- Any patterns worth remembering for future runs

3. **Write a reviewer diary entry:**
```
diary_write agent="reviewer" entry="PR-{number}:{title}|{N}rounds|fixed:{fixed_count}|skipped:{skipped_count}|outcome:{outcome}" topic="pr-review"
```

4. **Print summary:**
```
====================================
PR #{number}: {title}
====================================
Outcome: {clean / timeout / needs human}
Rounds:  {N}
Fixed:   {count} findings
Skipped: {count} findings (noise)

Recommendation: {MERGE — all real issues resolved / REVIEW — remaining findings listed below / WAIT — bot did not re-review}

{If remaining findings exist, list them here}
====================================
```

## Key Rules

1. **Intent preservation is sacred.** The PR exists for a reason. Every fix must serve that reason, not the reviewer's tangential suggestions.
2. **NOISE stays unfixed.** Do not touch it. Do not acknowledge it. The KG remembers it so future runs skip it faster.
3. **Convergence over completeness.** A PR with only cosmetic findings is ready to merge. Don't let perfect be the enemy of shipped.
4. **Record everything.** Every fix, every skip, every reason. The KG is how this system gets smarter.
5. **No artificial limits.** Keep turning until the bot has nothing real left. The loop ends when you run out of addressable findings, not when a counter expires.
