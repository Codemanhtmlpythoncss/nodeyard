"""Page files kept in memory with their gzip copy and an ETag, shared by the dashboard and the 3D galaxy.

A file is re-read only when its size or modification time changes, so an update to the page files shows at once.
Text is compressed once, at the best level, instead of on every request.
"""
import hashlib
import os
import threading
import zlib

MIME = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml", ".json": "application/json", ".png": "image/png", ".ico": "image/x-icon",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".webmanifest": "application/manifest+json",
    ".woff2": "font/woff2", ".glb": "model/gltf-binary", ".mp3": "audio/mpeg",
}
# Text worth compressing; pictures and audio are compressed already.
COMPRESSIBLE = ("text/", "application/json", "application/javascript", "application/manifest+json", "image/svg+xml", "model/gltf")
GZIP_MIN = 1400            # smaller than one packet: not worth it


def gzip_bytes(body, level=6):
    z = zlib.compressobj(level, zlib.DEFLATED, 31)
    return z.compress(body) + z.flush()


class StaticCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.files = {}

    def get(self, full, ctype=None):
        st = os.stat(full)
        key = (st.st_mtime_ns, st.st_size)
        with self.lock:
            hit = self.files.get(full)
        if hit and hit[0] == key:
            return hit[1]
        with open(full, "rb") as f:
            body = f.read()
        ctype = ctype or MIME.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        gz = gzip_bytes(body, 9) if ctype.startswith(COMPRESSIBLE) and len(body) >= GZIP_MIN else None
        # content-based, so a file copied in unchanged (an update) keeps its cache in every browser
        tag = hashlib.blake2b(body, digest_size=8).hexdigest()
        entry = {"body": body, "gzip": gz if gz and len(gz) < len(body) else None, "ctype": ctype, "etag": '"%s"' % tag, "tag": tag}
        with self.lock:
            self.files[full] = (key, entry)
        return entry

    def warm(self, folder):
        """Read and compress every page file once, so the first visitor after a restart doesn't wait for it."""
        for root, _dirs, names in os.walk(folder):
            for name in names:
                if os.path.splitext(name)[1].lower() in MIME:
                    try:
                        self.get(os.path.join(root, name))
                    except OSError:
                        pass


STATIC = StaticCache()
