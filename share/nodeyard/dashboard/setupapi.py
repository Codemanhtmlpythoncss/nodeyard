"""Read-only SSH host-key probing for the one-host device setup wizard."""
import ipaddress
import re
import subprocess


TS_NET = ipaddress.ip_network("100.64.0.0/10")
ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"}
FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/=]+$")


class SetupError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def private_ipv4(value):
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        raise SetupError("Use a private LAN IPv4 address or a Tailscale 100.x address.")
    if (address.version != 4 or address.is_loopback or address.is_link_local or address.is_multicast
            or address.is_unspecified or not (address.is_private or address in TS_NET)):
        raise SetupError("The setup wizard only connects to private LAN or Tailscale IPv4 addresses.")
    return str(address)


def parse_key_fingerprints(output):
    found = []
    for line in (output or "").splitlines():
        fields = line.split()
        if len(fields) < 3 or not FINGERPRINT_RE.fullmatch(fields[1]):
            continue
        algorithm = line.rsplit("(", 1)[-1].rstrip(") ").upper()
        found.append({"algorithm": algorithm or "SSH", "fingerprint": fields[1]})
    priority = {"ED25519": 0, "ECDSA": 1, "RSA": 2}
    unique = {item["fingerprint"]: item for item in found}
    return sorted(unique.values(), key=lambda item: (priority.get(item["algorithm"], 9), item["algorithm"], item["fingerprint"]))


def probe_host(host, port=22):
    host = private_ipv4(host)
    if isinstance(port, bool):
        raise SetupError("The SSH port must be between 1 and 65535.")
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise SetupError("The SSH port must be between 1 and 65535.")
    if not 1 <= port <= 65535:
        raise SetupError("The SSH port must be between 1 and 65535.")
    try:
        scanned = subprocess.run(["ssh-keyscan", "-T", "6", "-p", str(port), host],
                                 capture_output=True, text=True, timeout=8, env=ENV)
    except (OSError, subprocess.SubprocessError) as error:
        raise SetupError("Couldn't read the SSH host key: %s" % (getattr(error, "strerror", None) or error), 502)
    if not scanned.stdout.strip():
        raise SetupError("No SSH host key answered at %s:%d. Check that SSH is running and reachable." % (host, port), 502)
    try:
        fingerprints = subprocess.run(["ssh-keygen", "-lf", "-"], input=scanned.stdout,
                                      capture_output=True, text=True, timeout=5, env=ENV)
    except (OSError, subprocess.SubprocessError) as error:
        raise SetupError("Couldn't read the SSH fingerprint: %s" % (getattr(error, "strerror", None) or error), 502)
    options = parse_key_fingerprints(fingerprints.stdout)
    if not options:
        raise SetupError("The SSH server didn't present a key Nodeyard could verify.", 502)
    return {"host": host, "port": port, "fingerprints": options,
            "suggested": next((item["fingerprint"] for item in options if item["algorithm"] == "ED25519"), options[0]["fingerprint"])}


def register(ctx, args):
    def probe(h, body):
        if h._forwarded() is not None:
            return h._json({"ok": False, "error": "Probe devices over Tailscale or your local network, not public Funnel."}, 403)
        if getattr(args, "demo", False):
            return h._json({"ok": False, "error": "Device setup is disabled in the demo."}, 501)
        try:
            result = probe_host(body.get("host"), body.get("port", 22))
            h._json(dict(result, ok=True))
        except SetupError as error:
            h._json({"ok": False, "error": str(error)}, error.code)

    ctx.post_routes["/api/setup/node/probe"] = probe
