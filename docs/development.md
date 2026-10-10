# Building from source

The repository holds only what nodeyard, its dashboard, yardcode and the Nodeyard AI Mac app need to run and to be
built. (Earlier test suites and CI live in the Git history.)

```bash
make demo                  # the simulated cluster (changes nothing)
make build                 # release tarballs + SHA256SUMS into dist/
make yardcode              # the single-file yardcode program into dist/yardcode
make build-macos-ai-app    # Nodeyard AI.app into dist/ (macOS, Swift 6)
make install-macos-ai-app  # build, check and install it in /Applications
```

Install nodeyard from a checkout with `sudo bash install.sh --from-dir . --force --yes`, and yardcode on its own with
`sh yardcode/install.sh --from-dir . --force`.

## Demo data

`share/nodeyard/demo/` holds the simulated cluster used by `nodeyard --demo` and the dashboard's `--demo` mode: `fs/`
(a sandbox filesystem), `rules` (answers for system commands) and `data/` (longer outputs). If you add a command that
reads something new from the system, add an answer to `rules` so demo mode can run it.
