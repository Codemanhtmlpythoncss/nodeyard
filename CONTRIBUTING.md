# Contributing to nodeyard

Thanks for helping. Bug reports, docs fixes, tests on hardware we don't
have, and code are all welcome.

## Ground rules

- **One command per action.** Every feature is a `nodeyard` command with
  `--help`, `--yes`, `--dry-run` and (where it reports status) `--json`.
  The menu, wizards and (later) the dashboard only call commands, never
  their own code paths.
- **Change the system only through the core helpers**: `ny_write_file`,
  `ny_remove_file`, `ny_ensure_dir`, `ny_run`, `ny_run_undoable`,
  `ny_service_enable`... They make `--dry-run`, demo mode and
  `nodeyard undo` work. Never `echo > /etc/...` directly.
- **Idempotent**: running anything twice must be safe.
- **Never print, log or pass secrets on a command line.** Read them from
  files or stdin and call `ny_secret_register` on them.
- **Plain-English errors with a fix**: `ny_die "what went wrong" "what to do"`.
- **Detect, don't assume**: distro, package manager, network backend and
  firewall come from `lib/core/detect.sh`.

See [docs/architecture.md](docs/architecture.md) for how the pieces fit.

## Setting up

You need bash 4.3+, git, jq, curl, shellcheck and shfmt. On macOS:
`brew install bash jq shellcheck shfmt`. Docker is needed for the
multi-distro harness.

```bash
git clone https://github.com/Codemanhtmlpythoncss/nodeyard.git
cd nodeyard
make deps     # fetches pinned bats, bats-support, bats-assert into .tools/
make lint     # shellcheck + shfmt
make test     # bats unit tests (no root, no hardware)
make harness  # installs and exercises nodeyard in a container per distro
make demo     # try the tool against a simulated cluster
```

## Making a change

1. Open an issue first for anything bigger than a small fix, so we can
   agree on the approach.
2. Write the code and a bats test for it. Most behaviour can be tested in
   demo mode or with `ny_lib_setup` plus per-test command answers
   (`ny_rule`): see `tests/helpers/common.bash`.
3. Run `make lint test`. CI runs the same, plus the harness on every
   supported distribution.
4. Add a line under `## [Unreleased]` in `CHANGELOG.md`.
5. Update the docs page for the feature, and `docs/STATUS.md` if it
   changes what's done or a known limitation.
6. Open a pull request; the template has a short checklist.

## Style

- bash, `set -Eeuo pipefail`, 4-space indents, formatted with
  `shfmt -i 4 -ci`; shellcheck must be clean.
- Core functions are prefixed `ny_`; module functions are prefixed with
  the module name (`k3s_`, `doctor_`...). Globals are UPPER_CASE with the
  same prefix; locals are lower_case and declared `local`.
- A function whose last statement could be false (`[[ x ]] && y`) must end
  with `return 0`, or `set -e` will stop the program.
- Comments explain *why*, not what.

## Commit messages

Short summary line in the imperative ("Add wicked backend for static
IPs"), a blank line, then what and why. Reference issues with `#123`.

## Releases

Maintainers tag `vX.Y.Z`; the release workflow builds the tarballs,
`SHA256SUMS` and signatures and publishes the GitHub release.
