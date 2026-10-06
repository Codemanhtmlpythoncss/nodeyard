## What and why

<!-- What does this change, and what problem does it solve? Link the issue: Fixes #123 -->

## How I tested it

<!-- e.g. bats tests added, demo mode, the harness, or real hardware (say which). -->

## Checklist

- [ ] `make lint test` passes
- [ ] New behaviour is a command with `--help`, `--dry-run`, `--yes` (and `--json` if it reports status)
- [ ] System changes go through the core helpers, so dry-run and `nodeyard undo` work
- [ ] No secrets printed, logged, or put on a command line
- [ ] Docs and `CHANGELOG.md` (Unreleased) updated
