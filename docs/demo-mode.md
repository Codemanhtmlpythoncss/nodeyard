# Demo mode

```bash
nodeyard --demo                 # the menu, against a simulated cluster
nodeyard --demo status
nodeyard --demo doctor
nodeyard --demo upgrade --version v1.34.1+k3s1 --dry-run
nodeyard --demo config set node.yard-4.groups gpu && nodeyard --demo changes && nodeyard --demo undo --last --yes
```

Demo mode runs the real nodeyard code against a simulated cluster: four
nodes on `192.168.1.0/24` (two Raspberry Pi 5s and a mini PC as servers,
plus a Raspberry Pi 4 worker), with k3s and Ollama running. You are
"logged in" to `yard-1`, the first server. Nothing on your computer is changed, and it needs no root.

How it works:

- Every system path points into a sandbox, a copy of
  `share/nodeyard/demo/fs`, kept in `~/.cache/nodeyard-demo` (or
  `$NODEYARD_DEMO_DIR`).
- Commands such as `ip`, `systemctl` and `kubectl` are answered by a shim
  from `share/nodeyard/demo/rules`, instead of the real programs.
- Commands that would change the system are only simulated; file changes
  land in the sandbox, so `changes` and `undo` work as they would for real.

Reset it with `nodeyard --demo demo reset`.

## Running it on a Mac

nodeyard itself needs bash 4.3 or newer; macOS ships bash 3.2. Install a
newer bash and jq with Homebrew (`brew install bash jq`), then run
`bin/nodeyard --demo` from a clone. Or use Docker:

```bash
docker run --rm -it -v "$PWD":/src debian:12 bash -c \
  'apt-get update -qq && apt-get install -yqq jq >/dev/null && /src/bin/nodeyard --demo'
```

The [screenshots in the README](../README.md) are generated from demo mode
by `make screenshots`.
