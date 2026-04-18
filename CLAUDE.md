# Project Standards

This project uses the [HARNESS](https://github.com/ProppyAI/HARNESS) backbone for all CI/CD, code review, and maintenance workflows.

## Workflow

All code changes flow through the HARNESS pipeline:
1. Create a branch and implement changes with TDD
2. Open a PR — the self-healing pipeline runs automatically
3. Security review, code review, and doc updates happen in parallel
4. Critical findings get one auto-fix pass, then a structured comment for human action
5. Only clean PRs are presented for human review

## Code Standards

- Write tests before implementation (TDD)
- All existing tests must pass before a PR is opened
- No secrets or credentials in code — use environment variables
- Follow existing naming conventions in this repo
- Keep PRs focused — one concern per PR

## Superpowers Workflow

When receiving a task, use the appropriate superpowers skill based on complexity:

- **Simple fixes** (typos, config changes): Just do it. No planning needed.
- **Bugs**: Use `/superpowers:systematic-debugging` to find root cause, then `/superpowers:verification-before-completion` to confirm the fix.
- **New features**: Use `/superpowers:brainstorming` to explore design, then `/superpowers:writing-plans` and `/superpowers:executing-plans` to implement.
- **Refactoring**: Use `/superpowers:writing-plans` to plan, then `/superpowers:executing-plans` to implement.
- **Before merging**: Always use `/superpowers:verification-before-completion`.

Use your judgment — not every task needs the full cycle. Simple tasks should be fast.

## Specialist Skills (Everything Claude Code)

Language-specific and domain-specific skills are available for deeper analysis:

- **Code review**: Use the language-specific reviewer for this repo (e.g., `/everything-claude-code:python-review`, `/everything-claude-code:typescript-review`, `/everything-claude-code:rust-review`)
- **Testing**: Use `/everything-claude-code:tdd` for test-driven development, or language-specific test skills
- **Research**: Use `/everything-claude-code:docs` to look up library documentation before guessing, `/everything-claude-code:search-first` to check for existing solutions
- **Security**: Use `/everything-claude-code:security-review` when touching authentication, user input, API endpoints, or secrets
- **Build errors**: Use the language-specific build resolver (e.g., `/everything-claude-code:rust-build`, `/everything-claude-code:go-build`)

These complement the Superpowers workflow — use them within any workflow stage where they add value.

## HARNESS Skills

- `/fix-pr-reviews <PR#>` — Autonomous PR review fix loop
  - Reads bot PR reviews, classifies findings as REAL (actual bugs) vs NOISE (theoretical/cosmetic)
  - Fixes REAL findings only, preserving original PR intent
  - Pushes, polls for bot re-review, keeps turning until nothing addressable remains
  - Writes to mempalace KG after each round — system learns noise patterns over time
  - Options: `--repo owner/repo` (default current)

## Backbone Updates

- `harness update [--force] [--dry-run]` — pull latest backbone files from HARNESS
- Version tracked in `.harness-version` (short SHA of latest release)
- Backbone files listed in `backbone-manifest.json` — auto-generated, never manually edited
- SessionStart hook checks for updates automatically on every session
- Daily maintenance CI opens a PR if the repo is stale
- Never overwrites: `harness.json` (deployment config), `module.harness.json`, `.harness/`
- Module manifests re-fetched and mempalace re-mined on each update

## Module Type

<!-- Uncomment the module this repo belongs to -->
<!-- module: ops -->
<!-- module: frontend -->
<!-- module: backend -->
<!-- module: data -->
<!-- module: content -->
<!-- module: social -->
<!-- module: ml -->
<!-- module: llm -->
<!-- module: security -->
<!-- module: analytics -->
<!-- module: scientific -->
<!-- module: comms -->
