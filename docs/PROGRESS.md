# Work tracker

The living checklist for the current round of Nodeyard work. Each row says what it is, where it lives, how it was
checked and what is still open, so a later session can pick up from the last pushed commit without re-deriving it.

Statuses: **Not started**, **Investigating**, **In progress**, **Implemented** (code done, checks pending),
**Tested** (automated checks pass), **Blocked**, **Needs manual verification** (needs the real cluster, a person, or
macOS permission prompts).

Pushed code, an installed Mac app, an installed yardcode and a deployed cluster are four separate states. A row only
claims the states that were checked.

## Baseline (start of this round)

| Check | Result |
|---|---|
| `make test-py`, dashboard suite | 215 tests passed (with the uncommitted work below applied) |
| `make test-py`, yardcode suite | 155 of 156 passed; `test_lineedit … trailing_backslash` failed once under load, then passed 3 of 3 alone (timing-sensitive pty test, yardcode unchanged) |
| Swift app build (`scripts/build-macos-ai-app.sh` to a scratch path) | built |
| Installed `/Applications/Nodeyard AI.app` | build from 2026-10-08 18:04, bundle id `com.nodeyard.ai`, 1.0.0 (1), not running; older than `main` |
| Installed `yardcode` | `~/.local/bin/yardcode` → `~/.local/lib/yardcode/bin/yardcode`, installed 2026-10-08 09:59 (before the latest commits) |
| Dashboard | answers on port 9092 (`/api/health` ok, `/api/v1/*` 401 without a key) |
| Control node SSH | refused (public key/password); cluster deploys are blocked until the user opens a session |

## Tasks

| # | Task | Status | Files | Checks | Open |
|---|---|---|---|---|---|
| 1 | Mac chat first-token hang ("Thinking…") | Tested | `macos/…/AppState.swift`, `ContentView.swift`, `dashboard/aiapi.py` | Swift build; dashboard suite | Live check against the real model |
| 2 | Mac app native tool calls (web search/page read, Accessibility computer use with per-action approval) | Tested (build + server validation tests) | `macos/…/MacComputerUse.swift`, `AppState.swift`, `NodeyardClient.swift`, `controlapi.py`, `aiapi.py` | `test_ai`, `test_control` | Needs manual verification: Accessibility prompt, live tool calls |
| 3 | Model removal bugs | Not started | | | |
| 4 | Model reloading mid-task / memory pressure | Not started | | | |
| 5 | Automatic model unloading setting (dashboard, Mac app, backend) | Not started | | | |
| 6 | Shared plugin registry + audit log | Not started | | | |
| 7 | Research Mode (sources, citations, report) | Not started | | | |
| 8 | Browser automation plugin | Not started | | | |
| 9 | Desktop computer use plugin (screenshots, windows) | Not started | | | |
| 10 | macOS management app (website parity) | Not started | | | |
| 11 | Device discovery, LAN/Tailscale monitoring | Not started | | | |
| 12 | Setup guides in the app and the dashboard | Not started | | | |
| 13 | App icon from the website logo | In progress | `scripts/build-nodeyard-ai-icon.swift`, `NodeyardMark.swift` | | Follow the macOS icon grid |
| 14 | Debian-1 readiness/recovery investigation | Not started | | | |

## Local install checklist (per change)

- [ ] Repository changes tested
- [ ] Changes committed and pushed to GitHub
- [ ] Nodeyard AI.app rebuilt when affected
- [ ] `/Applications/Nodeyard AI.app` updated when affected
- [ ] Installed macOS app launched and smoke-tested
- [ ] Installed yardcode CLI updated when affected
- [ ] `command -v yardcode` resolves to the intended executable
- [ ] Installed CLI tested from a fresh shell
- [ ] Relevant local services updated and health-checked
- [ ] App, CLI and API compatibility verified where applicable
- [ ] Remaining installation failures documented

## Pushed commits this round

| Commit | What | Tests |
|---|---|---|
