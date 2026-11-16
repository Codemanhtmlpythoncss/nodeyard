#!/usr/bin/env python3
"""arpwho.py IFACE ADDRESS [SECONDS]

Asks the wired network "who has ADDRESS?" (an ARP request) and prints the
hardware address of every machine that answers, one per line, first answer
first. Two lines mean two machines claim the same IP address, which makes a
node come and go (the server talks to whichever answered last).

Needs root (it sends raw Ethernet frames) and Linux. Prints nothing and exits
1 when it can't ask (no permission, no such interface, not Linux).
"""
import socket
import struct
import sys
import time

ETH_P_ARP = 0x0806
SIOCGIFADDR = 0x8915


def request(src_mac, src_ip, dst_ip):
    """A broadcast ARP request for dst_ip (all arguments as raw bytes)."""
    eth = b"\xff" * 6 + src_mac + struct.pack("!H", ETH_P_ARP)
    arp = struct.pack("!HHBBH6s4s6s4s", 1, 0x0800, 6, 4, 1, src_mac, src_ip, b"\x00" * 6, dst_ip)
    return eth + arp


def reply_mac(frame, want_ip):
    """The sender's hardware address if frame is an ARP reply from want_ip, else None."""
    if len(frame) < 42 or frame[12:14] != struct.pack("!H", ETH_P_ARP):
        return None
    _htype, ptype, hlen, plen, op, sha, spa, _tha, _tpa = struct.unpack("!HHBBH6s4s6s4s", frame[14:42])
    if op != 2 or ptype != 0x0800 or hlen != 6 or plen != 4 or spa != want_ip:
        return None
    return ":".join("%02x" % b for b in sha)


def iface_mac(iface):
    with open("/sys/class/net/%s/address" % iface) as f:
        return bytes.fromhex(f.read().strip().replace(":", ""))


def iface_ip(iface):
    """This interface's IPv4 address as bytes, or 0.0.0.0 (an ARP probe) if it has none."""
    import fcntl
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = fcntl.ioctl(s.fileno(), SIOCGIFADDR, struct.pack("256s", iface.encode()[:15]))
        return packed[20:24]
    except OSError:
        return b"\x00" * 4
    finally:
        s.close()


def ask(iface, address, seconds=2.0):
    dst = socket.inet_aton(address)
    src_mac, src_ip = iface_mac(iface), iface_ip(iface)
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ARP))
    try:
        s.bind((iface, 0))
        frame = request(src_mac, src_ip, dst)
        found, sends = [], 3
        deadline = time.monotonic() + seconds
        next_send = 0.0
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            if sends and now >= next_send:
                s.send(frame)
                sends -= 1
                next_send = now + seconds / 3
            s.settimeout(max(0.05, min(deadline, next_send if sends else deadline) - now))
            try:
                got = s.recv(2048)
            except socket.timeout:
                continue
            mac = reply_mac(got, dst)
            if mac and mac != ":".join("%02x" % b for b in src_mac) and mac not in found:
                found.append(mac)
        return found
    finally:
        s.close()


def main(argv):
    if len(argv) < 3 or not hasattr(socket, "AF_PACKET"):
        return 1
    try:
        seconds = float(argv[3]) if len(argv) > 3 else 2.0
        macs = ask(argv[1], argv[2], max(0.5, min(seconds, 10.0)))
    except (OSError, ValueError):
        return 1
    for m in macs:
        print(m)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
