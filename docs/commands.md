# Command reference

`nodeyard help` lists every command; `nodeyard COMMAND --help` explains
one in full. This page is an overview.

Global options work anywhere on the command line:

| Option | Effect |
|---|---|
| `--yes`, `-y` | Answer yes to confirmations. Needed when not at a terminal. |
| `--dry-run` | Show every file change and command, change nothing |
| `--json` | Machine-readable output (with `--dry-run`: the plan) |
| `--verbose`, `-v` | More detail |
| `--no-color` | No colour (`NO_COLOR` is respected too) |
| `--demo` | Run against the simulated cluster |
| `--help`, `-h` | Help |

Exit codes: 0 ok, 1 failed, 2 bad command line, 3 something needed is
missing (not root, a missing tool, the wrong kind of node), 4 partly
done, 10 needs confirmation (re-run with `--yes`), 130 cancelled.

## Start here

| Command | |
|---|---|
| `nodeyard` / `nodeyard menu` | The interactive menu |
| `quickstart` | Detect this machine and recommend a setup |
| `wizard NAME` | Run one guided wizard |
| `detect [--json]` | What nodeyard sees about this machine |

## Cluster (k3s)

| Command | |
|---|---|
| `install master [--ha] [--worker] [--interface I] ...` | First server |
| `install join-master --server URL --token-file F` | Additional server |
| `install worker --server URL --token-file F` | Worker |
| `token [--reveal] [--json]` | How to join other machines |
| `status [--json]` | This node and the cluster |
| `start`, `stop`, `restart`, `enable-boot`, `disable-boot` | The k3s service |
| `kubeconfig [--user U] [--ip A] [--merge] [--stdout]` | kubectl access |
| `upgrade [--channel C \| --version V]` | Upgrade k3s on this node |
| `uninstall k3s` | Remove k3s from this node |

## Nodes

| Command | |
|---|---|
| `list-nodes [--json]` | The cluster's nodes |
| `worker-info [--json]` | What a worker needs to join: address, port 6443, ports to open, commands |
| `add-node worker\|master --ssh USER@HOST` | Set up another machine over SSH |
| `remove-node NODE` | Drain and remove a node |

## AI

| Command | |
|---|---|
| `ai install`, `ai uninstall` | Ollama on this machine |
| `ai deploy`, `ai undeploy` | Ollama across the cluster |
| `ai status`, `ai nodes` | What's running where |
| `ai model install\|list\|rm MODEL [--node N]` | Models |
| `ai split plan\|deploy\|status\|test\|undeploy` | One model across nodes (experimental) |

## Network and health

| Command | |
|---|---|
| `network-info`, `sysinfo` | Interfaces, addresses, routes; hardware summary |
| `doctor [--fix] [--json] [--strict]` | Check and fix common problems |
| `nettest` | Pod networking between all nodes |
| `logs [--follow] [--lines N]` | k3s service log |
| `firewall status\|open\|disable` | The host firewall |
| `watchdog-install --master A`, `watchdog-uninstall`, `promote` | Control-plane watchdog |

## Backups and updates

| Command | |
|---|---|
| `snapshot save\|list\|restore` | Cluster-state snapshots |
| `update [--check] [--version V]` | Update nodeyard |

## Settings and the tool

| Command | |
|---|---|
| `config show\|get\|set\|unset\|edit\|validate\|export\|import\|drift\|path` | The cluster config |
| `changes [--feature F]`, `changes show ID`, `undo ID\|--last\|--feature F` | What changed, and undo |
| `secrets list` | Stored secrets (names only) |
| `deps [--feature F] [--install]` | Required tools |
| `ui install-gum` | The nicest terminal interface |
| `uninstall`, `uninstall everything` | Remove things |
| `completion bash\|zsh` | Shell completion script |
| `version`, `help` | |

## Compatibility with k3s-manager

`k3s-manager` is installed as an alias, and every k3s-manager 3.2 command
and flag still works, including the old aliases (`join-info`,
`get-nodes`, `net-test`, `install agent`). See
[upgrading](upgrading.md#from-k3s-manager) for what changed.
