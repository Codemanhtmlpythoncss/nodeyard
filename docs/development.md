# Development and testing

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the ground rules and style.
This page explains how nodeyard is tested.

## Quick reference

```bash
make deps      # pinned bats, bats-support, bats-assert (+ shellcheck, shfmt on Linux) into .tools/
make lint      # shellcheck and shfmt on every shell file; wizard JSON validity
make test      # bats unit tests
make harness   # every supported distro, in containers (needs Docker)
make demo      # the simulated cluster
make build     # release tarballs + SHA256SUMS into dist/
```

## Unit tests

`tests/unit/*.bats` run without root or hardware, on Linux and macOS (with
Homebrew's bash). Two helpers in `tests/helpers/common.bash` set them up:

- `ny_lib_setup` sources the libraries into the test, with every system
  path redirected into a private directory (`NODEYARD_ROOT`) and system
  commands answered by the demo shim. Use it to test functions directly.
- `ny_cmd_setup` + `ny_cmd_run ARGS` run `bin/nodeyard --demo` in a
  private demo sandbox (`$DEMO_ROOT`). Use it to test whole commands.

Either way, `ny_rule 'GLOB' EXIT_CODE 'OUTPUT'` overrides what one system
command answers in this test, for example
`ny_rule "systemctl is-active --quiet k3s" 3` to simulate k3s being down.
`$BATS_TEST_TMPDIR/calls.log` records every simulated command, so a test
can assert what would have been run.

What's covered: JSON output, secret redaction (including the log file),
version comparison, every validator, the config parser and editor
(comments preserved, journaled, batched), detection for 22 distro
releases plus Raspberry Pi, boot disks, network backends and firewalls,
the change journal and undo, the dispatcher, help and completion, tables
and plain prompts, wizard specs, k3s command construction (including that
tokens and passwords never reach output, logs or plans), doctor checks and
fixes, the other commands end to end, and install/uninstall.

## The multi-distro harness

`tests/harness/run.sh [DISTRO...]` starts a clean container for each
distro in `tests/harness/distros.txt`, mounts the source tree, and runs
`tests/harness/in-container.sh`, which:

1. runs `install.sh --from-dir` (installing jq and friends with that
   distro's package manager),
2. checks `detect` (package manager, support level, architecture),
   `deps`, and `doctor --json`,
3. re-runs the installer (must be a no-op),
4. changes the config, checks the journal, and undoes it,
5. checks `install master --dry-run` changes nothing under `/etc`,
6. runs `uninstall.sh` and checks nothing is left.

CI runs it for every distro on amd64 and arm64 runners (Arch on amd64
only). Logs land in `tests/harness/logs/`.

Containers have limits: no systemd as PID 1, no real network
configuration, no k3s. Those parts are covered by unit tests with
simulated commands, and need real hardware for full confidence; see
[STATUS.md](STATUS.md#verified-on-hardware).

## Demo data

`share/nodeyard/demo/` holds the simulated cluster: `fs/` (a sandbox
filesystem), `rules` (answers for system commands) and `data/` (longer
outputs). If you add a command that reads something new from the system,
add an answer to `rules` so demo mode and the tests can run it.

## Screenshots

`make screenshots` runs commands in demo mode and renders their coloured
output to SVG in `docs/media/` (via `scripts/screenshots.sh` and
`scripts/ansi2svg.py`). Re-run it when the output changes.

## Releasing

1. Move the `[Unreleased]` changelog entries under a new
   `## [X.Y.Z] - DATE` heading and set `NY_VERSION` in `lib/core/base.sh`.
2. `make release VERSION=X.Y.Z` checks everything and creates the tag.
3. `git push origin vX.Y.Z`. The release workflow builds the tarballs,
   `SHA256SUMS` and its Sigstore signature, and publishes the release.
