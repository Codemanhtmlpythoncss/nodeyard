# Status and roadmap

nodeyard is built in phases; each minor release completes one. This page
says what's done, what's next, and what is known not to work yet, so work
can pick up cleanly at any point.

**Current release: 0.1.0 (Phase 1, foundation).**

## Roadmap

| Phase | Release | Scope | State |
|---|---|---|---|
| 1 | 0.1.0 | Foundation: modules, config, detection, logging, UI and wizards, doctor, demo mode, journal/undo, tests, CI | **done** |
| 2 | 0.2.0 | IP management (static IPs via each network backend with automatic rollback, cluster IP plan, hostnames, mDNS, local DNS, speed tests) and the network device view (ARP/mDNS discovery, history, problem flags, LLDP/SNMP switch ports) | next |
| 3 | 0.3.0 | Remote installer: `remote-install.sh` from a Linux or macOS laptop (bash 3.2), inventory files, preflight table, parallel installs, IP-change reconnect, retry failed | planned |
| 4 | 0.4.0 | High availability: pinned and verified k3s, kube-vip virtual IP, add/remove/promote/maintenance/replace, rolling upgrades, shared storage (Longhorn or NFS), MetalLB, registry mirror, node groups | planned |
| 5 | 0.5.0 | Node agent (Go, mutual TLS) and dashboards: per-node and cluster dashboards with full control, metric history, logins/2FA/audit log, API tokens, `nodeyard top` | planned |
| 6 | 0.6.0 | Website hosting: from a folder, git or an image; Caddy standalone or cluster ingress; Let's Encrypt and a local CA; Tailscale Funnel, Cloudflare Tunnel, port forwarding; uptime checks; app catalogue | planned |
| 7 | 0.7.0 | AI: GPU drivers and detection, OpenAI-compatible router with API keys and usage, model placement and sync, Open WebUI, benchmarks | planned |
| 8 | 0.8.0 | Operations: cluster-wide doctor, scheduled and verified backups with restore wizard, rolling OS updates, alerts, power management, support bundle | planned |
| 9 | 0.9.0 | Security review of everything, with a written report | planned |
| 10 | 1.0.0+ | Extras, once everything above works on real hardware | planned |

## Done in 0.1.0

- Modular layout: `bin/nodeyard`, `lib/core` (shared machinery),
  `lib/modules` (one file per feature), `share/nodeyard` (wizards, pins,
  demo data). See [architecture](architecture.md).
- Every k3s-manager 3.2 command ported, plus `k3s-manager` alias.
- Command registry with `--help`, `--yes`, `--dry-run`, `--json` for all
  commands, and bash/zsh completion.
- Change journal with undo by change, last change, or feature.
- INI cluster config with schema validation, comment-preserving edits,
  export/import, and a drift check for this node.
- Detection: 22 distro releases tested, Raspberry Pi, boot disk type,
  network backend (NetworkManager, netplan, networkd, ifupdown, dhcpcd,
  wicked), firewall.
- Terminal UI with gum/whiptail/dialog/plain backends, wizards (8),
  quick-start, status header.
- doctor with 24 checks and undoable fixes.
- Secret redaction in logs, output and plans; secrets store.
- Demo mode with a simulated four-node cluster.
- install.sh / uninstall.sh, self-update from verified releases.
- 139 bats unit tests, passing on macOS and Linux; multi-distro harness
  passing on Debian 12/13, Ubuntu 22.04/24.04, Fedora 42, Rocky 9,
  AlmaLinux 10, openSUSE Leap 15.6 and Tumbleweed, and Arch (run locally
  on arm64, Arch under amd64 emulation; CI runs every distro on amd64 and
  arm64); shellcheck- and shfmt-clean; CI and signed release workflows.

## Known limitations

- **Not yet verified on real hardware.** Everything above is tested with
  simulated system commands and in containers. Installing k3s, joining
  nodes and the AI commands keep k3s-manager 3.2's tested behaviour, but
  nodeyard 0.1.0 itself hasn't been run against real machines yet. See the
  checklist below.
- The k3s and Ollama installer scripts are downloaded from their official
  URLs but not pinned to a checksum (k3s in 0.4, Ollama in 0.7).
- The firewall helper handles ufw and firewalld; custom nftables/iptables
  rulesets are reported, not changed (0.9).
- `config drift` checks only this node (all nodes in 0.5).
- Snapshot restore is single-server only (full backups in 0.8).
- The interactive menu and wizards are tested with plain prompts; the gum
  and whiptail/dialog backends are exercised only by hand.
- The containers in the harness don't run systemd or k3s, so service
  management and k3s installs are covered only by unit tests with
  simulated commands.

## Verified on hardware

Nothing yet. To help, run this on a spare machine or VM and report back:

1. `curl -fsSL .../install.sh | sudo bash`, then `nodeyard detect`: is
   everything right?
2. `sudo nodeyard doctor`: anything wrong or misleading?
3. `sudo nodeyard install master --worker --dry-run`, then without
   `--dry-run`; `sudo nodeyard status` shows the node Ready.
4. On a second machine, `sudo nodeyard add-node worker --ssh user@host`
   from the first; check both appear in `status`, then run `nettest`.
5. `sudo nodeyard changes`, `sudo nodeyard undo --last`.
6. `sudo nodeyard uninstall everything` leaves the machines as they were.

## Picking up work

- Next phase: 2 (IP management and the network device view).
- Development setup: [development.md](development.md).
- Dev tools used in this repo are pinned in `tests/tools.lock`; runtime
  downloads in `share/nodeyard/versions.lock`.
