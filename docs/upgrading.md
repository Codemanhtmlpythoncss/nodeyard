# Upgrading

## Upgrading nodeyard

```bash
sudo nodeyard update --check     # is there a newer release?
sudo nodeyard update             # download, verify, show what's new, install
sudo nodeyard update --version 0.2.0
```

`update` downloads the release for your machine from GitHub, checks its
SHA-256 against the release's `SHA256SUMS`, shows the changelog entries
between your version and the new one, and installs after you confirm. It
won't install an older version unless you add `--force`. Running from a
git clone, it uses `git pull` instead.

Re-running the one-line installer does the same thing.

Read the [changelog](../CHANGELOG.md) before upgrading across several
versions: until 1.0, minor versions may change commands or config.

### Upgrading a whole cluster

Update nodeyard on every node. The remote installer in 0.3 adds
`remote-install.sh --update` to do this for every machine in your
inventory at once.

## Upgrading k3s

```bash
sudo nodeyard upgrade --version v1.34.1+k3s1     # or: --channel stable
```

Upgrade one server at a time, then the workers: a worker must never run a
newer k3s than the servers, and Kubernetes supports upgrading one minor
version at a time (1.33 to 1.34, not 1.32 to 1.34). Take a snapshot first
(`sudo nodeyard snapshot save`). Rolling, cluster-wide upgrades come in 0.4.

## From k3s-manager

nodeyard is the successor to the single-file `k3s-manager` 3.2.0 script.

1. Install nodeyard with the one-line installer. If it finds
   `/usr/local/bin/k3s-manager`, it keeps it as `k3s-manager.legacy` and
   makes `k3s-manager` an alias for nodeyard.
2. Your k3s cluster is untouched. nodeyard works out each node's role from
   what's installed. To record it in the config, run
   `sudo nodeyard config set node.$(hostname -s).role server` (or `agent`).
3. Things that work differently:
   - `token` hides the token; add `--reveal` to print it. Join commands
     read the token from a file (`--token-file`) or stdin (`--token-stdin`)
     rather than the command line; `--token` still works but warns.
   - Changing commands ask for confirmation; scripts need `--yes`.
   - `uninstall` asks what to remove. `uninstall k3s` is the old behaviour.
   - `add-node` asks you to confirm the other machine's SSH host key the
     first time (or pass `--host-key`).
   - The distro is detected, not asked for.
   - Settings live in `/etc/nodeyard/cluster.conf`, not
     `/etc/k3s-manager/config.env`. The old file is left in place.
   - Ollama models for `ai deploy` are stored in `/var/lib/nodeyard/ollama`
     (previously `/var/lib/k3s-manager/ollama`); move the directory across
     on each AI node to keep downloaded models.
