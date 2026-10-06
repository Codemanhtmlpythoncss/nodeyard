# Health checks: `doctor`

```bash
sudo nodeyard doctor            # check, then offer to fix each problem
sudo nodeyard doctor --fix      # fix everything that can be fixed, without asking
sudo nodeyard doctor --json     # for scripts and the (upcoming) dashboard
sudo nodeyard doctor --strict   # exit 1 if problems remain (for monitoring)
sudo nodeyard doctor --only br-netfilter,time-sync
```

Each check is **ok**, a **warning** (worth knowing, nothing to fix), a
**problem** (with a fix), or skipped when it doesn't apply to this machine.
Every fix is an ordinary nodeyard change: it shows up in
`nodeyard changes` and can be undone with `nodeyard undo`. Fixes that need
a reboot say so.

## What it checks

| ID | Checks | Fix |
|---|---|---|
| `deps` | jq, curl, ip, flock, openssl, tar are installed | installs them |
| `config` | the cluster config is valid | (shows the problem) |
| `k3s-binary` | k3s is installed on a node configured for it | (reinstall) |
| `k3s-enabled` | the k3s service starts at boot | enables it |
| `k3s-active` | the k3s service is running | restarts it |
| `swap` | reports swap (fine with k3s) | |
| `br-netfilter`, `overlay` | kernel modules loaded | loads them now and at boot |
| `ip-forward` | IP forwarding on | sets it persistently |
| `time-sync` | a time-sync service runs | enables systemd-timesyncd or installs chrony |
| `var-space` | more than 1 GiB free in /var | (free some space) |
| `eviction` | a nearly-full disk can't lock the node out | makes k3s reclaim only 2 GiB |
| `boot-disk` | warns about USB sticks and SD-card servers | |
| `memory-cgroup` | the memory cgroup exists (Raspberry Pi) | edits the kernel command line (reboot) |
| `interface` | the cluster interface exists, is up, has an address | brings it up |
| `api-port` | port 6443 answers locally (servers with a firewall) | opens the k3s ports |
| `forward-policy` | iptables FORWARD doesn't drop everything | adds an ACCEPT rule ahead of the policy |
| `foreign-nft` | no other nftables table drops pod traffic | lets only the pod network through |
| `nextcloud` | no port 80/443 clash between Nextcloud and Traefik | removes k3s's Traefik service |
| `server-reachable` | a worker can reach its server | |
| `api-healthz`, `node-ready` | the API answers; this node is Ready | |
| `cluster-nodes` | every node Ready, none under disk/memory pressure | |
| `pods` | every pod running | |

Later releases add checks for time drift between nodes, certificates,
etcd health, version mismatches and config drift across the cluster.
