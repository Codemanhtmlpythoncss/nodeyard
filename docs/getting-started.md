# Getting started

This walks through installing nodeyard and building a first cluster. Every
step can also be done from the menu (`sudo nodeyard`); the commands are
shown so you can see exactly what happens.

## 1. What you need

- Two or more Linux machines (one works too) on the same network, ideally
  wired to one switch. Raspberry Pi 4/5 (64-bit OS), mini PCs and old
  laptops all work. See [supported distros and hardware](distros-and-hardware.md).
- `sudo` on each machine, and internet access (to download k3s).
- About 2 GB of RAM for a server node; workers can be smaller.

Want to look around first? `nodeyard --demo` runs everything against a
simulated cluster, see [demo mode](demo-mode.md).

## 2. Install nodeyard

On each machine:

```bash
curl -fsSL https://raw.githubusercontent.com/Codemanhtmlpythoncss/nodeyard/main/install.sh | sudo bash
```

The installer downloads the latest release, checks its SHA-256 against the
release's `SHA256SUMS`, installs to `/usr/local/lib/nodeyard`, links
`/usr/local/bin/nodeyard`, and offers to install the few packages nodeyard
needs (jq, curl...). Prefer to read it first? Download it, read it, then
run `sudo bash install.sh`.

Check the machine:

```bash
nodeyard detect          # what nodeyard sees: distro, hardware, network
sudo nodeyard doctor     # common problems, with fixes
```

## 3. Create the cluster

Pick the machine with the most memory and, ideally, an SSD (not an SD
card) to be the first server. Then either run `sudo nodeyard` and choose
**Create a cluster**, or:

```bash
# Three or more machines: use embedded etcd so the cluster survives one failing.
sudo nodeyard install master --ha --worker --interface eth0

# One or two machines: a single server is simpler and just as reliable.
sudo nodeyard install master --worker --interface eth0
```

- `--worker` lets workloads run on the server too (good for small clusters).
- `--interface` pins cluster traffic to your wired card. Find its name with
  `nodeyard network-info` (often `eth0`, `end0` or `enp1s0`).
- Add `--dry-run` first to see every file and command it would change.

Why not two servers? etcd needs a majority of servers to agree, so with
two, losing *either* stops the cluster. One server plus regular snapshots
is more reliable, and three servers can lose one.

## 4. Join the other machines

From the first server, nodeyard can set up the others over SSH:

```bash
sudo nodeyard add-node worker --ssh admin@192.168.1.11
sudo nodeyard add-node master --ssh admin@192.168.1.12   # an extra server (HA clusters)
```

You'll be shown each machine's SSH key fingerprint to confirm the first
time, and asked for its SSH and sudo passwords if it needs them. The join
token is copied as a root-only file; it never appears on a command line.

Or join from each machine itself:

```bash
sudo nodeyard token --reveal          # on the server: shows the token
# on the new machine:
sudo install -m 600 /dev/stdin /root/k3s-token    # paste the token, then Ctrl-D
sudo nodeyard install worker --server https://192.168.1.10:6443 --token-file /root/k3s-token
```

## 5. Check it

```bash
sudo nodeyard status       # this node and every node in the cluster
sudo nodeyard nettest      # pod networking between all nodes
sudo nodeyard kubeconfig   # set up kubectl for your user
```

## Next steps

- Run AI models across the cluster: [AI workloads](ai.md)
- Take snapshots: [Snapshots and backups](backups.md)
- See what nodeyard changed, and undo it: [Changes and undo](changes-and-undo.md)
- Coming next: static IPs and a view of every device on your network,
  a remote installer for your laptop, a floating virtual IP, dashboards,
  website hosting. See the [roadmap](STATUS.md).

## The terminal AI agent

nodeyard installs `yardcode` too: `yardcode login`, then `yardcode`. See [yardcode](../yardcode/README.md).
