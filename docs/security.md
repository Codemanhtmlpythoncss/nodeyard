# Security

nodeyard runs as root and changes networking, firewalls and the cluster,
so it is built to be careful. This page describes what it does today and
what's planned. To report a problem, see [SECURITY.md](../SECURITY.md).

## Secrets

- Join tokens, passwords and API keys are stored one per file in
  `/etc/nodeyard/secrets/` (directory 0700, files 0600, root only).
- They are passed to programs through files (`--token-file`,
  `K3S_TOKEN_FILE`) or stdin, never on a command line where other users
  could see them in `ps`.
- They are never printed. `nodeyard token` hides the join token unless
  you ask with `--reveal`.
- Every message, log line, `--dry-run` diff and JSON plan passes through
  redaction: secrets nodeyard has read are replaced by `[REDACTED]`, and
  so is anything that looks like one (`password=...`, `--token VALUE`,
  `Authorization: Bearer ...`, k3s join tokens). Files with mode 0600 never
  have their contents shown in a dry run.
- An external datastore DSN (which usually contains a password) is passed
  to k3s through its root-only environment file, not its world-readable
  service file.

## Downloads

- Everything is fetched over HTTPS only (plain HTTP is refused).
- Pinned third-party tools are listed in `share/nodeyard/versions.lock`
  with their SHA-256, and nodeyard refuses a download whose checksum
  doesn't match.
- nodeyard's own releases ship a `SHA256SUMS` file, signed with Sigstore
  (keyless, tied to this repository's release workflow); `install.sh` and
  `nodeyard update` verify the checksum.
- Known gap: k3s's and Ollama's installer scripts are fetched from their
  official URLs but are not yet pinned to a checksum. Pinning k3s (binary
  plus its published checksums) comes in 0.4, Ollama in 0.7.

## SSH

`add-node` never disables host-key checking. On first connection it shows
the machine's key fingerprint for you to confirm (or accepts a
`--host-key` you pass), stores it in nodeyard's own known_hosts, and then
requires it to match. Passwords are typed into ssh and sudo themselves;
nodeyard never sees or stores them.

## Changes to your system

Every system file is backed up before nodeyard changes it, and every
change is recorded and can be undone (see
[changes and undo](changes-and-undo.md)). `--dry-run` shows changes
before they happen.

## Firewall

nodeyard opens only the ports a role needs (k3s: 6443, 2379-2380 on
servers, 8472/udp, 51820-51821/udp, 10250) on ufw or firewalld. It never
rewrites custom nftables/iptables rulesets; it tells you what to allow.
`firewall disable` exists for troubleshooting and asks first.

## Planned (see the roadmap)

- **0.5 agent and dashboard**: mutual TLS between nodes with a cluster
  certificate authority; the agent accepts only a fixed list of nodeyard
  commands, never arbitrary shell; dashboard logins with admin and
  read-only roles, rate limiting, optional two-factor, CSRF protection,
  HTTPS only, an audit log; dashboards stay on your local network unless
  you choose otherwise.
- **0.9 security review**: firewall rules restricted to the cluster
  subnet on ufw, firewalld and nftables; an SSH hardening wizard (key-only
  login, applied only after key login is confirmed to work); secret
  rotation (including the k3s token); optional VPN (Tailscale or
  WireGuard); a written review of everything built.
