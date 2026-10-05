# nodeyard status

Successor to k3s-manager 3.2.0. Built in phases; see the plan in the
project conversation. Decisions: name nodeyard, MIT, repo
Codemanhtmlpythoncss/nodeyard, versions 0.N.0 per phase (1.0.0 once
verified on hardware), config = git-config-style INI, Go agent + no-build
web UI (phase 5), built-in AI router (phase 7), default site exposure =
Tailscale Funnel (custom free names also supported).

## Phase 1 (foundation): IN PROGRESS

Done (written, syntax-checked; `nodeyard help` / `--version` / command
`--help` work):
- lib/core: base (paths, sandbox root), term (NO_COLOR, width), log
  (secret redaction), json, errors (fix hints, exit codes, --json errors),
  journal (backup + undo per change/feature), run (dry-run plan, atomic
  file writes, services), validate (+ IPv4 math), config (INI parse/edit/
  validate/schema), detect (distro/pkg/init/arch/hw/boot disk/net backend/
  firewall), deps (+ pinned verified downloads, versions.lock), secrets,
  ui (gum/whiptail/dialog/plain, spinner, progress, tables), registry
  (dispatch, help, completion, --json wrapping), wizard (JSON specs),
  kube, ssh (host-key verification), demo (sandbox + command shim)
- lib/modules: menu (header, quick-start), info, host, firewall, k3s,
  cluster, ai, ai_split, doctor, backup, config_cmd, changes, tool
- share/nodeyard: wizards/*.json, demo/shim.sh, versions.lock (gum pinned)
- bin/nodeyard, lib/nodeyard.sh
- Legacy fixes: dry-run honoured, tokens via files/stdin (never argv),
  real SSH host-key checks, no /tmp kubeconfig, agent upgrade no longer
  reinstalls as server, watchdog treats 401 as reachable.

Not done yet (next steps, in order):
1. Demo fixtures: share/nodeyard/demo/{rules,data/,fs/} (simulated
   3-node cluster) and run every command under --demo.
2. completions/nodeyard.bash and completions/_nodeyard (call
   `nodeyard __complete`).
3. install.sh (--from-dir, release download + SHA256SUMS, k3s-manager
   alias, /usr/bin link when sudo secure_path lacks /usr/local/bin),
   uninstall.sh.
4. tests: helpers + shim rules; bats for redaction, json, config,
   detection per distro (os-release fixtures), journal/undo, dry-run,
   registry, k3s command construction, doctor, wizard (plain UI);
   tests/harness (systemd containers per distro); tests/tools.lock with
   pins (bats 1.14.0 / support 0.3.0 / assert 2.2.4, shellcheck 0.11.0,
   shfmt 3.14.1; sha256s are in the session log).
5. shellcheck + shfmt -i 4 -ci across everything; fix findings.
6. Repo files: README, LICENSE (MIT), CHANGELOG, CONTRIBUTING, SECURITY,
   CODE_OF_CONDUCT (original text), .gitignore, .editorconfig,
   .shellcheckrc, Makefile (lint/test/build/release/deps/harness/demo),
   .github (ci.yml, release.yml, issue/PR templates; actions pinned by
   SHA: checkout 3d3c42e5..., upload-artifact 043fb46d...,
   download-artifact 3e5f45b2..., setup-go b7ad1dad...,
   cosign-installer 6f9f1778...), examples/cluster.conf, docs pages.
7. Commit "Phase 1: foundation" and tag v0.1.0.

## Known limitations (so far)
- k3s installer (get.k3s.io) and Ollama installer are not yet pinned or
  checksum-verified; planned for phases 4 and 7.
- Firewall: ufw/firewalld only; custom nftables/iptables are reported, not
  changed (phase 9 reconcile).
- Nothing is verified on real hardware yet.

## Dev machine notes
Homebrew tools installed for this project, to remove when done:
go, bats-core, gum, shfmt (bash was already installed; keep it).
