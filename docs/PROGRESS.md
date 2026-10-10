# Work tracker

The living checklist for the current round of Nodeyard work. Each row says what it is, where it lives, how it was
checked and what is still open, so a later session can pick up from the last pushed commit without re-deriving it.

Statuses: **Not started**, **Investigating**, **In progress**, **Implemented** (code done, checks pending),
**Tested** (automated checks pass), **Blocked**, **Needs manual verification** (needs the real cluster, a person, or
macOS permission prompts).

Pushed code, an installed Mac app, an installed yardcode and a deployed cluster are four separate states. A row only
claims the states that were checked.

On 2026-10-10 the test suites, CI and dev tooling were removed from the repository at the owner's request (the
repository keeps only what the apps need). The checks named below ran before that and are in the Git history
(`git show 7d8cc76:tests/...`).

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
| Bats (`make test`) | 267 passed |
| `make lint` | ok |

## Tasks

| # | Task | Status | Files | Checks | Open |
|---|---|---|---|---|---|
| 1 | Mac chat first-token hang ("Thinking…") | Tested | `macos/…/AppState.swift`, `ContentView.swift`, `dashboard/aiapi.py` | Swift build; dashboard suite | Live check against the real model |
| 2 | Mac app native tool calls (web search/page read, Accessibility computer use with per-action approval) | Tested (build + server validation tests) | `macos/…/MacComputerUse.swift`, `AppState.swift`, `NodeyardClient.swift`, `controlapi.py`, `aiapi.py` | `test_ai`, `test_control` | Needs manual verification: Accessibility prompt, live tool calls |
| 3 | Model removal bugs | Tested | `lib/modules/ai_split.sh` (`split_delete_files`, `ai_split_rm`), `lib/modules/ai.sh` (`ai_model_rm_cmd`), `dashboard/aiapi.py` (`_forget_models_cache`, `_model_job_done`) | `tests/unit/split.bats`, `tests/unit/ai_model.bats`, `test_ai.ModelInventory` | Root causes found in code: split rm reported "Deleted" even when a Ready node's helper failed (file reappeared on the next scan); Ollama rm deleted loaded models (memory stayed used, no Unload button) and returned success when every node failed. Not reproduced on the live cluster (no access) |
| 4 | Model reloading mid-task / memory pressure | Tested (code), Needs manual verification (cluster) | `dashboard/lifecycle.py`, `aiapi.py`, `web/ai.js`, `kube.py`, `analysis.py`, `ai_split.sh` | `test_lifecycle`, `test_dashboard`, `split.bats` | Causes fixed: Ollama's `/v1` endpoint ignores `keep_alive`, so chats reset "keep loaded" models to 5 min; a stale browser model pick switched the cluster back after another client changed models; 1 s readiness timeout flapped the split model NotReady mid-answer. Evidence added: OOMKilled restarts now raise an alert. GPU/RPC memory exhaustion is plausible but unconfirmed without cluster access |
| 5 | Automatic model unloading setting (dashboard, Mac app, backend) | Tested (backend + dashboard), Mac app pending | `dashboard/lifecycle.py`, `server.py`, `web/ai.js`, `docs/ai.md` | 32 lifecycle tests; demo dashboard in the browser: toggle, presets, invalid value (400), automatic unload end to end | Mac app settings UI; live cluster check of `/slots` |
| 6 | Shared plugin registry + audit log | Not started | | | |
| 7 | Research Mode (sources, citations, report) | Not started | | | |
| 8 | Browser automation plugin | Not started | | | |
| 9 | Desktop computer use plugin (screenshots, windows) | Not started | | | |
| 10 | macOS management app (website parity) | Implemented; rendered with live demo data | `macos/…/ManagementClient.swift`, `ManagementState.swift`, `ManagementViews.swift`, `ModelsManageView.swift`, `JSONValue.swift` | Swift build (0 warnings); every Manage screen rendered against the demo dashboard (test mode + `NODEYARD_AI_SNAPSHOT_DIR`) | Live sign-in on the real dashboard (needs your password); Terminal/Commands/Settings pages not ported (see parity table) |
| 11 | Device discovery, LAN/Tailscale monitoring | Not started | | | |
| 12 | Setup guides in the app and the dashboard | In progress (app guide done) | `macos/…/SetupGuideView.swift` | Rendered | Dashboard guide |
| 13 | App icon from the website logo | Tested | `scripts/build-nodeyard-ai-icon.swift` | Iconset rendered and checked: the dashboard favicon on the macOS 824/1024 grid | |
| 14 | Debian-1 readiness/recovery investigation | Not started | | | |
| 15 | AI section debugging pass (user priority) | In progress | `web/ai.js`, `web/app.js`, `agentapi.py`, `aiapi.py` | Browser on the demo dashboard with injected failures (HTTP 502, dropped connection, mid-stream error, prompt progress); `test_agentapi`, `test_ai` | Found and fixed: skills chat ignored the chosen Ollama model; job completion callbacks skipped when the progress window was closed (deleted models came back); no first-token feedback; raw "Failed to fetch" errors |

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

## Website ↔ Mac app parity

| Website section | Mac app (Manage) | Backend | Status |
|---|---|---|---|
| Overview | Overview: totals, AI model, alerts, events | `/api/state` | Done |
| Nodes (+ Devices sidebar restart/reboot) | Nodes: table, details, Restart Kubernetes, Reboot every machine | `/api/state`, `/api/run` | Done |
| Processes | Not ported | `/api/agents` | Gap |
| Pods (with logs) | Pods: filter, details, logs | `/api/state`, `/api/logs` | Done |
| Workloads | Workloads | `/api/state` | Done |
| Network | Network (services) | `/api/state` | Done (no ingress list yet) |
| Storage | Storage: node disks, volumes | `/api/state` | Done |
| Hardware | Hardware: CPU, memory, temperature, GPUs, OOM kills | `/api/state` (agents) | Done (no speed-test button yet) |
| AI › Chat | Chat workspace | `/api/v1/chat/completions` | Done (separate client, synced chats) |
| AI › Models | AI models: split controls, automatic unloading, downloads, run/switch (keeps old file unless asked), delete, Ollama, free space | `/api/ai/*`, `/api/run`, `/api/v1/lifecycle` | Done |
| AI › Find models (Hugging Face search, download) | Not ported | `/api/ai/search`, `/api/ai/files` | Gap |
| AI › API examples | Not ported | | Gap |
| Events | Events (warnings filter) | `/api/state` | Done |
| Alerts | Alerts | `/api/state` | Done |
| Doctor | Doctor: checks, fix one, fix all | `/api/doctor`, `/api/run` | Done |
| Commands | Not ported (use the website) | `/api/commands`, `/api/run` | Gap |
| Terminal | Not ported (needs a PTY over WebSocket) | `terminal.py` | Gap |
| Settings | Only the app's own settings and automatic unloading | `/api/settings/*` | Gap (passwords, keys, public access stay on the website) |
