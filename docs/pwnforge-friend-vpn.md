# Let a friend into your PwnForge labs from anywhere

`pwnforge-vpn.sh` gives someone outside your tailnet full network access to your
PwnForge lab subnet — so they can run their *own* `nmap`, `ssh`, Burp or Metasploit
against the targets, exactly like you do — **without** installing Tailscale and
**without** forwarding a port on your router.

It does this by carrying OpenVPN over [Tailscale Funnel](https://tailscale.com/kb/1223/funnel),
which you already have. The friend connects to your host's public `*.ts.net` name;
Tailscale routes it in; the tunnel drops them onto the lab network.

> This is for giving a trusted friend hands-on access to practice boxes. Everything
> on the lab subnet becomes reachable to whoever holds a bundle, so only hand bundles
> to people you'd let onto that network, and run `off` when you're done.

## Why there's a TLS wrapper (stunnel)

Funnel only accepts **TLS** connections and decides where to send each one by the
**SNI** host name inside the TLS handshake. A raw OpenVPN stream carries no SNI, so a
plain `.ovpn` can't travel through Funnel at all — this is the wall you hit otherwise.

So OpenVPN runs on localhost and is wrapped in TLS by `stunnel`:

```
friend:  OpenVPN → stunnel (adds TLS + your ts.net SNI)
                      │  Tailscale Funnel (public :443/:8443/:10000)
                      ▼
host:    stunnel (terminates TLS) → OpenVPN → routed onto the lab subnet
```

The friend's stunnel also pins your host's certificate by hostname, so the outer layer
is authenticated end to end, independent of MagicDNS or `tailscale cert`.

## On the host (as root)

```bash
# 1. Set it up and publish it over Funnel (defaults: public port 10000, lab 10.200.0.0/16)
sudo pwnforge-vpn.sh setup

#    Pick a different public port or lab range if you need to:
sudo pwnforge-vpn.sh setup --port 443 --cidr 10.200.0.0/16

# 2. Make a connection bundle for each friend/device (the name is just a label)
sudo pwnforge-vpn.sh client alice alice-pwnforge.tgz

# 3. Check what's running and what the public endpoint is
sudo pwnforge-vpn.sh status

# 4. Tear it all down when you're finished (stops the services, removes the Funnel, clears the firewall)
sudo pwnforge-vpn.sh off
```

`setup` only needs to run once. Make one bundle per friend (or per device) with
`client`; each bundle gets its own certificate, so you can hand them out and revoke
by running `off`/`setup` to rotate.

Requirements on the host: `tailscale` (already up, with Funnel permitted for the
node in the Tailscale admin console), `openvpn`, `stunnel`, and `openssl`. Public
port must be one Funnel allows: **443**, **8443**, or **10000**.

State, keys and configs live under `/var/lib/pwnforge-funnel-vpn`
(override with `PWNFORGE_VPN_DIR`). Two self-signed PKIs are generated: one for
OpenVPN auth (CA + server/client certs + a `tls-crypt` key) and a separate one for
the outer stunnel TLS (its server cert's SAN is your `ts.net` name, pinned by the
client). Nothing depends on a public CA.

## What the friend does

Send them the `.tgz`. Inside:

- `pwnforge.ovpn` — the OpenVPN profile (CA, their cert/key and `tls-crypt` inlined)
- `stunnel.conf` — the TLS wrapper that points at your `ts.net` endpoint
- `tls-ca.crt` — the cert used to verify your host
- `connect.command` — a double-click launcher (macOS/Linux) that starts stunnel then OpenVPN
- `README.txt` — the same steps, for Windows or doing it by hand

They need an OpenVPN client and `stunnel` installed. Once connected, the lab subnet
is routed to them and they attack targets straight from their own terminal — no
in-app console needed. Targets still can't start outbound connections; only inbound
sessions get through, which is expected.

## How it fits the dashboard

The script is self-contained and usable over SSH on its own. It lives beside
`pwnforge-host.sh` in the dashboard module so the nodeyard dashboard can later drive
`setup`/`status`/`off` and hand out client bundles through the same
`systemd-run`-as-root path the rest of the PwnForge module already uses.
