# Changelog

All notable changes to nodeyard are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and nodeyard uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0.0,
each minor release completes one phase of the [roadmap](docs/STATUS.md).

## [Unreleased]

### Added

- `nodeyard worker-info` and a menu entry, **What a worker needs to join**
  (on servers): shows the join address and port (6443), this server's k3s
  version, where the join token is (hidden, with an offer to reveal it in
  the menu), every network port that must be open with whether each is
  open on this server's firewall, what the worker machine needs, and the
  exact commands to add it from here over SSH or on the worker itself.
  `--json` output included.
- When a k3s service won't start after `install` or `upgrade`, nodeyard
  now reads its log, shows the lines that matter and names the likely
  cause with a fix (cloud-setup service, memory cgroup, duplicate node
  name, wrong token, server unreachable, port in use, wrong interface,
  clock trouble, missing firewall tools). The k3s installer no longer
  starts the service itself, so a failed start is explained instead of
  ending in "the installer failed".
- `add-node --become auto|sudo|su`: become root on the new machine with
  sudo, or with su and the root password (for machines without sudo).
  Automatic by default.

### Fixed

- A worker whose first start fails (for example because the cluster still
  lists the node under its old address) is no longer reported as failed
  while systemd is still retrying it: nodeyard waits up to 90 seconds for
  the retry. The failure explanation now shows the decisive log lines
  first, no longer mistakes ordinary cgroup log lines for a missing memory
  cgroup, and recognises "failed to find interface with specified node ip".
- SSH host keys in `add-node`: a saved key is now checked against the key the
  machine presents *now*. A changed key (for example after a reinstall) is
  explained with the old and new fingerprints and needs a deliberate yes;
  an out-of-date key in your own `~/.ssh/known_hosts` is ignored instead of
  being copied back in; a new machine's fingerprint is confirmed by pressing
  Enter. `--yes` no longer skips this at a terminal, so the menu wizard now
  works for machines it hasn't seen (without a terminal, pass `--host-key`).
  `--dry-run` never blocks on it, and login failures get a plain next step
  instead of "check the user name and password".
- `remove-node` now clears the join password k3s stored for the node, so the
  same name can be added again (a reinstalled machine was refused as a
  "duplicate hostname"), and skips the drain for a node that is not Ready.
- The menu header named the default route's interface (e.g. wlan0) next to
  an address that is on another (eth0).
- `add-node` left root-owned files behind in `/tmp` on the new machine
  ("Permission denied" while cleaning up), because it unpacked as root but
  cleaned up as the SSH user. Everything now runs as root in one step in
  its own directory, which removes itself.
- `add-node` now checks that the `--interface` you gave exists on the new
  machine before installing anything, and lists the ones it has.

## [0.1.0] - 2026-10-06

The foundation release. nodeyard is the successor to the single-file
`k3s-manager` 3.2.0 script, rebuilt as a modular tool. Every k3s-manager
command still works (`k3s-manager` is installed as an alias), with the
changes listed under "Changed".

### Added

- **One command, many front ends.** Every action is a `nodeyard` command
  with `--help`, `--yes`, `--dry-run` and `--json`. The interactive menu
  and its guided wizards only ever run those commands.
- **Interactive menu** with a status header (host, address, role, cluster
  health), guided wizards with back/cancel, a summary of exactly what will
  change before anything does, and a first-run quick-start that detects the
  machine and recommends a setup. Uses gum, whiptail or dialog when
  available and plain prompts otherwise; respects `NO_COLOR`.
- **Change journal and undo.** Every system file nodeyard writes is backed
  up first and recorded. `nodeyard changes` lists them and `nodeyard undo`
  reverts one change, the last change, or everything a feature did.
- **`--dry-run` everywhere**, printing each file diff and command instead
  of making changes; with `--json` it returns the plan as data.
- **One cluster config file** (`/etc/nodeyard/cluster.conf`, an INI format
  readable by plain bash) with `config show|get|set|unset|edit|validate|
  export|import|drift`. Validation explains each problem with its line
  number, and checks duplicate addresses, overlapping IP ranges and more.
- **Runtime detection** of distribution, package manager, init system,
  architecture, Raspberry Pi model, boot disk (SD card, eMMC, NVMe, USB),
  network backend (NetworkManager, netplan, systemd-networkd, ifupdown,
  dhcpcd, wicked) and firewall: `nodeyard detect`.
- **Dependency checks** that offer to install what's missing before work
  starts (`nodeyard deps`), and checksum-verified downloads of pinned
  third-party tools (`share/nodeyard/versions.lock`).
- **`doctor` rebuilt**: each check reports ok/warning/problem with a plain
  explanation; problems can be fixed one by one or all at once with
  `--fix`, and every fix can be undone. New checks for required tools,
  config validity and SD-card servers. `--json` and `--strict` added.
- **Demo mode** (`nodeyard --demo`): the whole tool runs against a
  simulated four-node cluster, changing nothing on the computer.
- **Logging with secret redaction**: join tokens, passwords, API keys and
  bearer tokens never reach the log file or the screen.
- **Secrets store** (`/etc/nodeyard/secrets`, root-only) and
  `nodeyard secrets list`.
- **Self-update from GitHub releases** (`nodeyard update`), verifying the
  release checksum and showing the changelog before installing.
- `install.sh` (one-line installer) and `uninstall.sh`; bash and zsh
  completion; bats test suite; multi-distro container test harness; CI and
  release workflows.

### Changed

- The join token is no longer printed by `nodeyard token` unless you add
  `--reveal`, and is never put on a command line: use `--token-file` or
  `--token-stdin` (`--token` still works but warns).
- `install`, `upgrade`, `stop`, `remove-node`, `uninstall` and other
  changing commands now ask for confirmation; scripts should pass `--yes`.
  Without a terminal and without `--yes` they stop with exit code 10.
- `uninstall` asks what to remove; `uninstall k3s` does what
  k3s-manager's `uninstall` did, and `uninstall everything` also reverts
  every recorded change and removes nodeyard.
- `add-node` verifies the other machine's SSH host key (you confirm its
  fingerprint, or pass `--host-key`) instead of accepting it silently.
- The distro is detected automatically instead of asking every time.
- Per-node settings moved from `/etc/k3s-manager/config.env` to the
  cluster config.

### Fixed

- `--dry-run` no longer writes kernel-module, sysctl and config files.
- `upgrade` on a worker no longer reinstalls it as a server (the k3s
  installer is now given the node's existing settings).
- `kubeconfig` no longer writes the admin kubeconfig to a predictable
  file in `/tmp`.
- An external datastore password is passed through k3s's root-only
  environment file instead of the world-readable service file.
- The watchdog no longer counts an HTTP 401 from a healthy API server as
  "unreachable", and installing it twice no longer duplicates settings.

### Security

- Secrets are redacted from logs, `--dry-run` output and JSON plans.
- Join tokens are stored with mode 0600 and passed to k3s by file.
- SSH host keys are verified on first connection.

[Unreleased]: https://github.com/Codemanhtmlpythoncss/nodeyard/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Codemanhtmlpythoncss/nodeyard/releases/tag/v0.1.0
