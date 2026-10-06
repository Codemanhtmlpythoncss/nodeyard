# Troubleshooting

Start with:

```bash
sudo nodeyard doctor      # finds and fixes the common problems below
nodeyard detect           # what nodeyard thinks this machine is
sudo nodeyard logs        # the k3s service log
```

nodeyard's own log is `/var/log/nodeyard/nodeyard.log` (secrets are
redacted). Add `--verbose` to any command for more detail, and `--dry-run`
to see what it would do.

## Installing and joining

**"ssh: connect to host X port 22: No route to host"** (or the same for
port 6443 during a join). `add-node` and `install worker` check
reachability first and print a checklist. If the two machines could ping
each other before, the cause is almost never the address: check, on the
target, that the service is running (`systemctl status ssh` / `k3s`), the
firewall (`sudo nodeyard firewall status`), the interface (`ip -br addr`),
and that both are on the same subnet and cable/switch port.

**`k3s.service` or `k3s-agent.service` fails with "control process exited
with error code"**: it failed immediately. See why:
`sudo systemctl status k3s-agent --no-pager -l && sudo journalctl -xeu k3s-agent --no-pager -n 100`.
Common causes:

- leftovers from an earlier failed install: `sudo nodeyard uninstall k3s`,
  then try again;
- a wrong or expired server address or token;
- the memory cgroup is disabled (Raspberry Pi OS): `sudo nodeyard doctor --fix`,
  then reboot;
- `nm-cloud-setup.service` on some cloud images.

**"This machine is already a k3s server; it can't be turned into a worker
in place"**: k3s keeps different state for servers and workers. Remove k3s
first (`sudo nodeyard uninstall k3s`).

**"Confirmation needed ... Re-run with --yes"** (exit code 10): you ran a
changing command from a script or without a terminal. Add `--yes` (and try
`--dry-run` first).

## A node is NotReady, or pods can't reach each other

**A node never becomes Ready**: run `sudo nodeyard doctor` on it. It checks
swap, kernel modules, IP forwarding, time sync, the memory cgroup, the
firewall and connectivity to the server.

**Pods on one node can't reach anything** (`kubectl top` shows `<unknown>`
for other nodes, DNS times out): another firewall table, such as
miniupnpd's or a custom nftables config, has a forward chain with `policy
drop`. In nftables a drop in any table wins. `doctor` finds it and
`doctor --fix` adds a persistent exception for the pod network only. Check
with `sudo nodeyard nettest`.

**Docker (e.g. Nextcloud AIO) and k3s break each other's networking**:
an iptables `FORWARD` policy of `DROP`. `doctor --fix` inserts an
`ACCEPT` rule ahead of the policy without changing the policy.

**A node shows DiskPressure and pods keep getting evicted, even after you
freed space**: by default k3s starts evicting at 95% full and continues
until 15% of the disk is free, which on a big shared disk can be tens of
GB. `doctor --fix` makes it reclaim only 2 GiB.

## Ports 80/443

**k3s took ports 80/443 I needed**: Traefik and ServiceLB bind them on
every node. `install master` disables them automatically when it finds
the ports in use; to disable them after the fact, reinstall with
`--disable traefik --disable servicelb`, or let `doctor --fix` remove the
Traefik service if Nextcloud is detected.

## AI

**`ai split` is slow to start, or the download crawls**: check the main
node's disk (`lsblk -d -o NAME,ROTA,TRAN,MODEL`; `usb` means a stick or an
enclosure) and its link speed (`cat /sys/class/net/eth0/speed`; `100`
instead of `1000` usually means a bad cable or a 10/100 switch port).

## Distro-specific

**RHEL, Rocky, Alma: `sudo nodeyard` says "command not found"**: their
sudo doesn't search `/usr/local/bin`. The installer adds a
`/usr/bin/nodeyard` link when it detects this; if yours predates that, run
`sudo /usr/local/bin/nodeyard`, or re-run the installer.

**RHEL family and SELinux**: k3s's installer adds the `k3s-selinux`
policy itself. `nodeyard detect` shows the machine's details.

**Arch: "pacman could not install ..."**: your package database is
stale. Run `sudo pacman -Syu`, then try again (nodeyard never runs a
partial upgrade).

## Undoing something

`sudo nodeyard changes` lists what nodeyard changed, and
`sudo nodeyard undo --last` reverts the most recent change. See
[changes and undo](changes-and-undo.md).

## Still stuck?

Open an issue with the output of `nodeyard --version`, `nodeyard detect`,
`sudo nodeyard doctor` and the failing command with `--verbose`:
<https://github.com/Codemanhtmlpythoncss/nodeyard/issues/new/choose>.
