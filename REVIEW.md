# Code Review Guidelines

These rules are read by Claude during automated code reviews via the HARNESS pipeline.

## Always check
- New API endpoints have corresponding tests
- Database migrations are backward-compatible
- Error messages don't leak internal details to users
- No secrets, credentials, or API keys in code
- Input validation at system boundaries (user input, external APIs)
- No SQL injection, command injection, or XSS vectors

## Style
- Prefer early returns over deeply nested conditionals
- Use structured logging, not string interpolation in log calls
- Keep functions focused — one responsibility per function

## Skip
- Formatting-only changes
- Auto-generated files
- Lock file changes
- Dependency version bumps with no code changes
