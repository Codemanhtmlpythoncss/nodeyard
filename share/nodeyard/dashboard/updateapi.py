"""Lets yardcode update itself from this server: `yardcode update` downloads the yardcode that was installed with nodeyard here, so
updating the dashboard (and nodeyard) and then running `yardcode update` anywhere brings yardcode to the same version.

  GET /api/v1/yardcode          {"version", "hash", "files"}   what is installed here
  GET /api/v1/yardcode/bundle   the program (bin/ and src/) as one .tar.gz

Protected like the rest of /api/v1: the model's API key, or a signed-in dashboard session.
"""
import hashlib
import io
import os
import tarfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
_cache = {"stamp": None, "data": None, "info": None}
_lock = threading.Lock()


def find_root():
    for cand in (os.path.join(HERE, "..", "..", "..", "yardcode"), os.path.join(HERE, "yardcode")):
        cand = os.path.abspath(cand)
        if os.path.isfile(os.path.join(cand, "bin", "yardcode")) and os.path.isdir(os.path.join(cand, "src", "yardcode")):
            return cand
    return ""


def program_files(root):
    """[(relative path, absolute path)] of what makes up the program, in a fixed order."""
    out = [("bin/yardcode", os.path.join(root, "bin", "yardcode"))]
    base = os.path.join(root, "src")
    for d, dirs, files in os.walk(os.path.join(base, "yardcode")):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__")
        for f in sorted(files):
            if not f.endswith((".pyc", ".pyo")) and not f.startswith("."):
                p = os.path.join(d, f)
                out.append((os.path.relpath(p, root).replace(os.sep, "/"), p))
    return out


def tree_hash(files):
    h = hashlib.sha256()
    for rel, p in files:
        h.update(rel.encode() + b"\0")
        with open(p, "rb") as f:
            h.update(f.read())
        h.update(b"\0")
    return h.hexdigest()


def bundle():
    """(tar.gz bytes, info) of the installed yardcode, rebuilt only when a file changed."""
    root = find_root()
    if not root:
        return None, None
    files = program_files(root)
    stamp = tuple((rel, os.path.getmtime(p), os.path.getsize(p)) for rel, p in files)
    with _lock:
        if _cache["stamp"] == stamp:
            return _cache["data"], _cache["info"]
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for rel, p in files:
                ti = tar.gettarinfo(p, arcname=rel)
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = ""
                ti.mode = 0o755 if rel == "bin/yardcode" else 0o644
                with open(p, "rb") as f:
                    tar.addfile(ti, f)
        version = ""
        try:
            with open(os.path.join(root, "src", "yardcode", "__init__.py"), "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("__version__"):
                        version = line.split("=", 1)[1].strip().strip("\"'")
        except OSError:
            pass
        info = {"version": version, "hash": tree_hash(files), "files": len(files)}
        _cache.update(stamp=stamp, data=buf.getvalue(), info=info)
        return _cache["data"], info


def register(ctx, args):
    def info(h, q):
        data, i = bundle()
        if data is None:
            return h._json({"ok": False, "error": "yardcode isn't installed next to nodeyard on this server."}, 404)
        h._json(dict(i, ok=True, size=len(data)))

    def get_bundle(h, q):
        data, i = bundle()
        if data is None:
            return h._json({"ok": False, "error": "yardcode isn't installed next to nodeyard on this server."}, 404)
        h._send(200, data, "application/gzip", {"Content-Disposition": 'attachment; filename="yardcode.tar.gz"', "X-Yardcode-Hash": i["hash"]})

    ctx.get_routes.update({"/api/v1/yardcode": info, "/api/v1/yardcode/bundle": get_bundle})
