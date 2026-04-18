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
  "confidence": 0.0-1.0,
  "reasoning": "Brief explanation of why this intent was chosen",
  "branch_suggestion": "harness/feature/short-description"
}
```

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

## Confidence Calibration
- 0.9+: Unambiguous, clear signal words, single interpretation
- 0.7-0.9: Likely correct but some ambiguity
- 0.5-0.7: Unclear, should ask for clarification
- < 0.5: Very ambiguous, must ask
