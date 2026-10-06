# Architecture

nodeyard is a Bash tool with one entry point, `bin/nodeyard`, split into a
small core and one module per feature area. This page explains how the
pieces fit and the rules they follow.

## Layout

```
bin/nodeyard              entry point: checks bash, finds itself, loads lib/, runs ny_main
lib/nodeyard.sh           loads every core library and module (in order)
lib/core/                 shared machinery, no features
  base.sh                 version, paths (sandbox-aware), flags, small helpers, version compare
  term.sh  log.sh         colour/width/NO_COLOR; logging with secret redaction
  errors.sh               ny_die (message + fix + exit code), --json errors, ERR trap
  journal.sh  run.sh      change journal + undo; the only ways to change the system
  validate.sh             value checks (IPv4, CIDR, hostnames, labels...) and IPv4 maths
  config.sh               the INI cluster config: parse, edit (comment-preserving), validate
  detect.sh               distro, package manager, init, arch, hardware, disk, network backend, firewall
  deps.sh                 command -> package mapping, install offers, pinned verified downloads
  secrets.sh              root-only secret files, always registered for redaction
  ui.sh                   gum / whiptail / dialog / plain prompts, spinner, progress, tables
  registry.sh             command registry, dispatcher, help, completion, global flags
  wizard.sh               JSON wizard engine
  kube.sh  ssh.sh         kubectl access; SSH with host-key verification
  demo.sh                 demo-mode sandbox
lib/modules/              one file per feature: k3s, cluster, host, firewall, ai, ai_split,
                          doctor, backup, config_cmd, changes, info, menu, tool
share/nodeyard/           data: wizards/*.json, versions.lock, demo/ (sandbox, shim, rules)
completions/              bash and zsh completion (they ask nodeyard for candidates)
install.sh  uninstall.sh  installer and remover
tests/                    bats unit tests, fixtures, multi-distro harness
```

## Rules every feature follows

**One command per action.** Each action is registered with `ny_cmd` and has
`--help`, `--yes`, `--dry-run`, and `--json` where it reports status. The
menu runs commands; wizards build a command line and run it; the dashboard
(coming in 0.5) will call the same commands through an agent that only
accepts a fixed list. So the interfaces can't behave differently.

**All changes go through the core.** Modules never write system files or run
state-changing programs directly. They call:

| Helper | Does | Undo |
|---|---|---|
| `ny_write_file PATH [MODE]` | atomic write; identical content is a no-op | restores the backup or removes the new file |
| `ny_remove_file PATH` | removes after backing up | restores it |
| `ny_ensure_dir PATH` | creates missing directories | removes them if empty |
| `ny_run CMD...` | runs a command | (none) |
| `ny_run_undoable UNDO -- CMD` | runs CMD, records UNDO | runs UNDO |
| `ny_service_enable UNIT` | enables a unit if it isn't already | disables it |

Each one honours `--dry-run` (prints a diff or the command and adds a step
to the plan instead), never executes anything in demo mode, and records
what it did in the change journal.

**Detect, don't assume.** `ny_detect_*` reads the machine: `/etc/os-release`,
the package manager, `/run/systemd/system`, `uname -m`, the device tree
(Raspberry Pi), the boot disk, which service owns networking, which
firewall is active.

**Errors explain themselves.** `ny_die "what went wrong" "how to fix it" CODE`.
Exit codes: 1 failed, 2 bad command line, 3 a precondition is missing (not
root, missing tool, wrong kind of node), 4 partly done, 10 needs
confirmation (re-run with `--yes`), 130 cancelled.

**Secrets never leak.** Secrets live in `/etc/nodeyard/secrets` (0700/0600),
are passed to programs through files or stdin, and are registered with
`ny_secret_register` so `ny_redact` scrubs them from every message, log
line, dry-run plan and JSON result. Pattern-based redaction also catches
`password=...`, `--token VALUE`, bearer tokens and k3s join tokens.

## The sandbox root

Every system path goes through `ny_path` (for example
`ny_path /etc/os-release`). Normally that's the path itself. When
`NODEYARD_ROOT` is set, the whole tool reads and writes inside that
directory instead. Demo mode and the tests use this, together with a
command shim on `PATH` that answers `ip`, `systemctl`, `kubectl`... from a
rules file. That's how the same code runs against a simulated cluster.

## The change journal

`/var/lib/nodeyard/journal/journal.jsonl` has one JSON line per change:
transaction (one per command run), time, command, feature, operation,
path, whether the file existed, where its backup is, and SHA-256 before and
after. Undo walks a transaction (or a feature) backwards. It refuses to
overwrite a file that something else edited after nodeyard wrote it, unless
`--force` is given. `uninstall everything` undoes every remaining
transaction before removing nodeyard.

## Wizards

A wizard is a JSON file in `share/nodeyard/wizards/`: a title, the command
it runs, and steps (choose, input, secret, yes/no). Each answer becomes a
flag. Steps can depend on earlier answers, choices can come from another
command's `--json` output, and secrets go to the command on stdin. Before
anything changes, the wizard runs the command with `--dry-run --json` and
shows its plan. Applying runs the very same command line with `--yes`.

## The config file

`/etc/nodeyard/cluster.conf` is git-config-style INI, chosen because plain
bash (even bash 3.2 on a Mac, for the upcoming remote installer) can read
it, and people can comment it. Edits through `nodeyard config set` keep
comments and layout, are validated against a schema, and are journaled.
See [configuration](configuration.md).

## What's coming

- **Agent and dashboard (0.5)**: a small Go program on each node that
  serves a dashboard and runs a fixed list of nodeyard commands for the
  cluster dashboard, over mutual TLS. Metrics stay on each node.
- **Virtual IP (0.4)**: kube-vip holds one address for the API and the
  dashboard, whichever server is up.

See [STATUS.md](STATUS.md) for the full roadmap.
