# The cluster config file

One file, `/etc/nodeyard/cluster.conf`, describes the whole cluster: its
nodes, their roles and addresses, and the network plan. Later releases add
websites, AI and backup settings to the same file. Secrets never go in it.

A commented example is in [`examples/cluster.conf`](../examples/cluster.conf).

## Format

```ini
# A comment. Lines starting with ; are comments too.
[cluster]
	name = homelab              # inline comments need a space before the #
	domain = "home.arpa"        # quotes are optional; use them to keep # or ;

[node "pi-1"]                   # one section per node; the name is its hostname
	address = 192.168.1.10/24
	groups = storage, low-power  # lists: comma-separated...
	groups = gpu                 # ...or repeated keys
```

Section and key names ignore case; node names don't. Indentation doesn't
matter.

## Working with it

```bash
nodeyard config show                       # the whole file
nodeyard config get node.pi-1.address      # one value
sudo nodeyard config set cluster.vip 192.168.1.9
sudo nodeyard config set node.pi-2.groups gpu --add   # add to a list
sudo nodeyard config unset node.pi-2.groups
sudo nodeyard config edit                  # in $EDITOR; validated before saving
nodeyard config validate [FILE]            # problems, with line numbers and fixes
nodeyard config export --out cluster.conf  # e.g. to rebuild elsewhere
sudo nodeyard config import cluster.conf   # validated, shows the diff, asks first
nodeyard config drift                      # does this node match its entry?
```

Every change is backed up and can be undone with `sudo nodeyard undo --last`.

## Settings

### `[cluster]`

| Key | Type | Meaning |
|---|---|---|
| `name` | name | Short name for the cluster, e.g. `homelab` |
| `domain` | hostname | Local DNS domain for node names; `home.arpa` is reserved for this |
| `vip` | IPv4 | Floating virtual IP for the API and dashboard (used from 0.4) |
| `k3s-version` | k3s version | Exact release, e.g. `v1.33.4+k3s1` |
| `k3s-channel` | stable / latest / testing | Channel when no exact version is set |
| `cluster-cidr` | CIDR | Pod network (k3s default `10.42.0.0/16`) |
| `service-cidr` | CIDR | Service network (k3s default `10.43.0.0/16`) |
| `tls-san` | list of hosts | Extra names/addresses for the API certificate |
| `disable` | list | Bundled k3s components to disable, e.g. `traefik` |

### `[network]`

| Key | Type | Meaning |
|---|---|---|
| `subnet` | CIDR | The LAN all nodes share |
| `gateway` | IPv4 | Your router |
| `dns` | list of IPv4 | DNS servers for nodes |
| `dhcp-range` | range | What your router hands out by DHCP (static addresses stay outside it) |
| `node-range` | range | Addresses reserved for nodes |
| `lb-range` | range | Addresses reserved for load-balanced services |
| `interface` | interface | Default ethernet interface for cluster traffic |

Ranges are written `FIRST-LAST`, e.g. `192.168.1.100-192.168.1.250`.

### `[node "NAME"]`

| Key | Type | Meaning |
|---|---|---|
| `host` | host | Address or name used to reach it over SSH |
| `address` | CIDR | Its static address, e.g. `192.168.1.10/24` |
| `interface` | interface | Its cluster network interface |
| `role` | server / agent / standalone | `master` and `worker` are accepted too |
| `init` | bool | `true` on the one server that creates the cluster |
| `server` | URL | The API address it joins, e.g. `https://192.168.1.9:6443` |
| `node-ip` | IPv4 | Address k3s was installed on (recorded automatically) |
| `allow-workloads` | bool | Let pods run on this server |
| `groups` | list | For placing workloads, e.g. `gpu`, `storage`, `low-power` |
| `labels` | list of `key=value` | Extra Kubernetes labels |
| `taints` | list of `key=value:Effect` | Kubernetes taints |
| `features` | list | nodeyard features installed on it |
| `tls-san` | list of hosts | Extra API certificate names (servers) |
| `ssh-user`, `ssh-port` | | How to reach it over SSH |
| `mac` | MAC | Its cluster interface's MAC (for Wake-on-LAN, later) |

### `[ui]`

| Key | Values | Meaning |
|---|---|---|
| `backend` | auto, gum, whiptail, dialog, plain | Terminal interface style |
| `color` | auto, always, never | Colour output |

### `[watchdog]`

Written by `nodeyard watchdog-install`: `master`, `interval` (seconds),
`threshold` (failed checks before it reports).

## What validation checks

- Syntax: every line is a section, a `key = value`, a comment or blank.
- Every value has the right type (with an example of a valid value).
- Unknown sections and keys are reported as warnings (typos).
- No two nodes share an address; at most one node has `init = true`.
- The VIP isn't a node's address and is inside the subnet.
- Node addresses and the DHCP/node/LB ranges are inside the subnet, and
  the ranges don't overlap.

## Which node am I?

A machine finds its own section by the name in `/etc/nodeyard/node-name`,
or its short hostname if that file doesn't exist.
