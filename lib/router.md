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
1. `/superpowers:brainstorming` — explore design (hybrid mode: assume reasonable defaults, ask only if genuinely ambiguous)
2. `/superpowers:writing-plans` — create implementation plan from design
3. `/superpowers:executing-plans` — implement the plan
4. `/superpowers:requesting-code-review` — self-review before PR
5. Commit and open PR

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
