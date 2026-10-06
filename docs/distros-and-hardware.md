# Supported distros and hardware

nodeyard runs on Linux machines with systemd, on amd64 (x86-64) or arm64.
It detects the distribution, package manager, init system and network
setup itself; check what it sees with `nodeyard detect`.

## Distributions

| Distribution | Versions | Package manager | Status |
|---|---|---|---|
| Debian | 12, 13 | apt | supported, tested in CI |
| Ubuntu Server/Desktop | 22.04, 24.04, 26.04 LTS | apt | supported, tested in CI (22.04, 24.04) |
| Raspberry Pi OS (64-bit) | Bookworm and later | apt | supported |
| Fedora | 42 and later | dnf | supported, tested in CI |
| RHEL, Rocky Linux, AlmaLinux | 8, 9, 10 | dnf | supported, tested in CI (Rocky 9, Alma 10) |
| Arch Linux | rolling | pacman | supported, tested in CI (amd64) |
| openSUSE Leap | 15.6, 16.0 | zypper | supported, tested in CI (15.6) |
| openSUSE Tumbleweed | rolling | zypper | supported, tested in CI |
| Linux Mint, Pop!_OS, Manjaro, other derivatives | | | best effort: works if the parent distro does, not tested |
| Debian 11, Ubuntu non-LTS, Fedora < 42 | | | best effort |
| Raspberry Pi OS 32-bit | | | best effort; k3s and AI need the 64-bit edition |
| Alpine, other non-systemd distros | | | unsupported (basic k3s commands may work) |
| RHEL/CentOS 7 and older | | | unsupported (end of life) |

"Tested in CI" means the [harness](development.md#the-multi-distro-harness)
installs nodeyard in a clean container of that distro on every push and
exercises its commands. Containers can't test everything real hardware
does (static IPs, failover, GPUs); see
[what needs hardware](STATUS.md#verified-on-hardware).

Needs: bash 4.3+ (every supported distro has it), systemd, `sudo`, and
internet access for installs. nodeyard installs what else it needs (jq,
curl, iproute2...) with your package manager after asking.

## Hardware

Anything that runs one of the distros above: Raspberry Pi 4 and 5 (on a
64-bit OS), mini PCs (Intel N100, Lenovo Tiny, HP EliteDesk...), old
laptops and desktops, and VMs.

| | Minimum | Recommended |
|---|---|---|
| k3s server | 2 GB RAM, 2 cores | 4 GB+ RAM, SSD/NVMe |
| k3s worker | 1 GB RAM | 2 GB+ RAM |
| AI nodes | 4 GB RAM for small models | 8-16 GB+, or a GPU (from 0.7) |

### Disks

`nodeyard detect` shows what the system disk is: SD card, eMMC, NVMe, SSD,
HDD or USB.

- **SD cards** wear out and are slow at the small synchronous writes etcd
  makes. Fine for workers; for servers, an SSD or NVMe (a Pi 5 NVMe HAT,
  or a USB SSD) is far more reliable. `doctor` warns about SD-card servers.
- **USB flash sticks** can freeze the machine during long writes. A USB
  SSD in an enclosure is fine.
- SD cards don't report wear; nodeyard can't measure it (eMMC, SSD and
  NVMe can, from 0.5's dashboards).

### Raspberry Pi notes

- Use the 64-bit Raspberry Pi OS (or Ubuntu Server for Pi).
- Raspberry Pi OS ships with the memory cgroup disabled, which makes k3s
  fail at start. `sudo nodeyard doctor --fix` adds
  `cgroup_memory=1 cgroup_enable=memory` to `/boot/firmware/cmdline.txt`;
  reboot afterwards.
- The wired port is usually `eth0` (`end0` on some images).

### Networks

Put every node on the same wired switch if you can. Wi-Fi works but is
slower and less reliable for cluster traffic. A machine with both can keep
Wi-Fi as its internet route and a way in if the wired side breaks; use
`--interface` to pin cluster traffic to the wired card.

nodeyard reads the network config from whichever tool owns it:
NetworkManager, netplan, systemd-networkd, ifupdown, dhcpcd or wicked
(openSUSE Leap 15). Changing it (static IPs) arrives in 0.2.
