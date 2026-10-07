"""TLS settings shared by the model client and the web tools.

Python from python.org on a Mac often has no certificate bundle at all, and a company proxy's own
certificate lives only in the system keychain. So when the default store is empty (or a bundle is named in
the environment) this looks for the system's certificates before giving up.
"""
import os
import ssl
import subprocess
import sys

from . import util

BUNDLES = ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt", "/etc/ssl/ca-bundle.pem",
           "/opt/homebrew/etc/ca-certificates/cert.pem", "/opt/homebrew/etc/openssl@3/cert.pem", "/usr/local/etc/openssl@3/cert.pem",
           "/usr/local/etc/openssl/cert.pem", "/usr/local/share/certs/ca-root-nss.crt")
_cache = {}


def _keychain_bundle():
    """On macOS: the keychain's certificates as a PEM file (cached for a day)."""
    path = os.path.join(util.data_dir(), "cacert-keychain.pem")
    try:
        import time
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < 86400:
            return path
        pem = ""
        for kc in ("/System/Library/Keychains/SystemRootCertificates.keychain", "/Library/Keychains/System.keychain",
                   os.path.expanduser("~/Library/Keychains/login.keychain-db")):
            if os.path.exists(kc):
                r = subprocess.run(["security", "find-certificate", "-a", "-p", kc], capture_output=True, text=True, timeout=20)
                pem += r.stdout
        if "BEGIN CERTIFICATE" in pem:
            util.atomic_write(path, pem, 0o600)
            return path
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def ssl_context(verify=True):
    key = bool(verify)
    if key in _cache:
        return _cache[key]
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        named = os.environ.get("YARDCODE_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
        ctx = ssl.create_default_context()
        if named and os.path.exists(named):
            ctx.load_verify_locations(named)
        else:
            if ctx.cert_store_stats().get("x509_ca", 0) == 0:
                for p in BUNDLES:
                    if os.path.exists(p):
                        try:
                            ctx.load_verify_locations(p)
                            break
                        except (ssl.SSLError, OSError):
                            continue
            if sys.platform == "darwin":
                # A school or company web filter re-signs HTTPS with its own certificate, which macOS trusts through the
                # keychain only: always add what the keychains hold, or every https request fails.
                p = _keychain_bundle()
                if p:
                    try:
                        ctx.load_verify_locations(p)
                    except (ssl.SSLError, OSError):
                        pass
    _cache[key] = ctx
    return ctx
