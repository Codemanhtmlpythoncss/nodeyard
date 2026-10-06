# k3s clusters and nodes

nodeyard installs [k3s](https://k3s.io), a lightweight Kubernetes, using
k3s's official installer, and handles the setup around it that commonly
goes wrong on homelab machines.

## Creating the cluster

```bash
sudo nodeyard install master [--ha] [--worker] [--interface IFACE] [options]
```

| Option | Meaning |
|---|---|
| `--ha` | Embedded etcd, so more servers can join. Use it if you'll have 3+ servers |
| `--worker` | Run workloads on this server too |
| `--interface IFACE` | Pin `--node-ip`, `--advertise-address` and `--flannel-iface` to this card |
| `--channel C` / `--version V` | k3s channel (`stable`, `latest`, `testing`) or exact version |
| `--tls-san HOST` | Extra API certificate name (repeatable) |
| `--no-auto-tls-san` | Don't add this host's name and Tailscale address automatically |
| `--disable NAME` | Turn off a bundled component, e.g. `traefik` (repeatable) |
| `--keep-ingress` | Keep Traefik/ServiceLB even if ports 80/443 are already taken |
| `--cluster-cidr`, `--service-cidr` | Pod and service networks |
| `--datastore-endpoint DSN` | External datastore (passed through k3s's root-only env file) |
| `--node-label K=V`, `--node-taint K=V:Effect` | Labels and taints (repeatable) |
| `--token-file PATH` / `--token-stdin` | Use your own cluster token |

Before installing, nodeyard:

- installs the packages k3s needs (curl, iptables, conntrack, socat,
  open-iscsi, NFS client) for your distro,
- loads `overlay` and `br_netfilter` and keeps them loaded at boot,
- enables IP forwarding and bridge netfilter,
- leaves swap alone (k3s is fine with it; it offers to disable it only if
  you ask interactively),
- warns if no time-sync service runs,
- makes k3s reclaim only 2 GiB after a low-disk eviction, instead of 10%
  of the disk (which can lock a laptop-sized disk out for good),
- checks ports 80/443: if something already uses them (Nextcloud, Docker,
  another web server), it disables Traefik and ServiceLB so k3s doesn't
  fight it, unless you pass `--keep-ingress`,
- adds this host's name and Tailscale address (if Tailscale is installed)
  to the API certificate, so `kubectl` works over your tailnet.

Every one of those changes can be undone (`nodeyard undo`), and
`--dry-run` shows them all first.

### How many servers?

etcd only works while a majority of servers agree:

| Servers | Survives losing | Recommendation |
|---|---|---|
| 1 | nothing, but simple and reliable | take [snapshots](backups.md) |
| 2 | **nothing**: losing either stops the cluster | avoid; use 1 server + workers |
| 3 | 1 server | the usual HA setup |
| 5 | 2 servers | larger clusters |

Run servers from SSD or NVMe if you can: etcd writes constantly, wears out
SD cards, and slow SD cards cause leader elections.

## Joining machines

As a worker (agent):

```bash
sudo nodeyard install worker --server https://SERVER:6443 --token-file /root/k3s-token [--interface IFACE]
```

As an additional server (HA clusters):

```bash
sudo nodeyard install join-master --server https://SERVER:6443 --token-file /root/k3s-token [--interface IFACE]
```

Without `--interface`, a worker uses the interface that routes to the
server. Use the servers' exact k3s version for workers (`--version`): an
agent must never be newer than the servers.

`sudo nodeyard token` on a server prints ready-to-run commands for each of
its addresses (plus Tailscale). It hides the token itself; add `--reveal`
to print it.

### Adding a machine over SSH

From a server:

```bash
sudo nodeyard add-node worker --ssh admin@192.168.1.11 [--interface eth0] [--port 22]
sudo nodeyard add-node master --ssh admin@192.168.1.12
```

It checks the machine is reachable (and explains "no route to host"),
shows you its SSH host key fingerprint to confirm (compare it with
`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on that machine's
console), checks that the `--interface` you named exists there, copies
nodeyard and the token as private files, then becomes root once and
installs, joins and cleans up. For unattended runs pass
`--host-key SHA256:...`. Running it again on a machine that's already set
up is safe.

To become root it uses `sudo` when your SSH user is allowed to, and
otherwise `su` (you type the root password, as on a Debian install
without sudo). Choose explicitly with `--become sudo` or `--become su`.
Logging in as `root@host` needs neither.

A laptop-friendly installer that sets up several machines at once
(including from macOS) is coming in 0.3.

## Day to day

```bash
sudo nodeyard status            # role, address, k3s version and service; on servers, nodes and failing pods
nodeyard list-nodes [--json]
sudo nodeyard logs [--follow] [--lines N]
sudo nodeyard start|stop|restart
sudo nodeyard enable-boot|disable-boot
sudo nodeyard nettest           # pod-to-pod, DNS and pod-to-kubelet checks across all nodes
sudo nodeyard kubeconfig [--user USER] [--ip ADDRESS] [--merge] [--stdout]
sudo nodeyard remove-node NODE  # drain and remove from the cluster
```

### Upgrading k3s

```bash
sudo nodeyard upgrade [--channel stable | --version vX.Y.Z+k3sN]
```

Upgrade servers first, then workers. The node keeps its existing settings.
(Rolling, cluster-wide upgrades come in 0.4.)

### When the control plane is down

`watchdog-install --master ADDRESS` checks the API on a timer and logs to
the journal (`journalctl -t nodeyard-watchdog`) when it has been
unreachable for a while. It never changes the cluster by itself. With
three servers the cluster keeps working when one fails; automatic failover
of a single address (a virtual IP) arrives in 0.4.

## Removing k3s

```bash
sudo nodeyard uninstall k3s
```

Runs k3s's own uninstaller (removing its containers and data on this
machine) and undoes nodeyard's k3s-related changes: kernel settings,
firewall rules, the watchdog, the stored token. Your cluster config stays.

## Multi-homed machines

`--interface` matters when a machine has more than one network path, for
example a wired card on the cluster switch plus Wi-Fi for the internet.
Without it, k3s uses whichever address the default route prefers, which is
often the Wi-Fi. nodeyard never changes routes or gateways.

## Coexisting with other services

k3s's bundled Traefik ingress and ServiceLB bind ports 80 and 443 on
**every** node. If `install master` finds those ports in use, or finds
Nextcloud (snap, Docker, a web root or a service), it disables both and
tells you. `doctor` also checks for an iptables `FORWARD` policy of
`DROP`, a classic cause of Docker and k3s breaking each other's networking.
