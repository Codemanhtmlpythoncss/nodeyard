"""yardcode update: bring this copy of yardcode up to date.

Where the new version comes from (first that works):
  1. your nodeyard server (the dashboard's /api/v1/yardcode: the copy installed with nodeyard there, which is what you updated last),
  2. GitHub (the nodeyard repository, branch main),
  3. a folder: yardcode update --from-dir path/to/nodeyard

It replaces this copy in place (the folder it was installed in, or the single-file program). A copy that runs from a git checkout is
left alone: use git pull there.
"""
import hashlib
import io
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

from . import __version__, net, util

REPO = "Codemanhtmlpythoncss/nodeyard"


class UpdateError(Exception):
    pass


def program_files(root):
    out = []
    b = os.path.join(root, "bin", "yardcode")
    if os.path.isfile(b):
        out.append(("bin/yardcode", b))
    for d, dirs, files in os.walk(os.path.join(root, "src", "yardcode")):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__")
        for f in sorted(files):
            if not f.endswith((".pyc", ".pyo")) and not f.startswith("."):
                p = os.path.join(d, f)
                out.append((os.path.relpath(p, root).replace(os.sep, "/"), p))
    return out


def tree_hash(root):
    h = hashlib.sha256()
    for rel, p in program_files(root):
        h.update(rel.encode() + b"\0")
        with open(p, "rb") as f:
            data = f.read()
        if rel == "bin/yardcode":      # the installer may point the first line at a particular python: that isn't a difference
            data = data.split(b"\n", 1)[-1]
        h.update(data + b"\0")
    return h.hexdigest()


def where():
    """How this copy is installed: ("folder", ROOT) | ("zipapp", FILE) | ("checkout", ROOT)."""
    here = os.path.dirname(os.path.abspath(__file__))
    arch = sys.argv[0] if sys.argv and os.path.isfile(sys.argv[0]) and zipfile.is_zipfile(sys.argv[0]) else ""
    if arch and os.path.abspath(here).startswith(os.path.abspath(arch)):
        return "zipapp", os.path.abspath(arch)
    root = os.path.dirname(os.path.dirname(here))          # .../lib/yardcode  (src/yardcode/update.py)
    if not os.path.isfile(os.path.join(root, "bin", "yardcode")):
        raise UpdateError("Can't tell where yardcode is installed (no bin/yardcode next to %s)." % os.path.dirname(here))
    d = root
    for _ in range(6):
        if os.path.exists(os.path.join(d, ".git")):
            return "checkout", root
        if os.path.dirname(d) == d:
            break
        d = os.path.dirname(d)
    return "folder", root


def _extract(data, into):
    """Unpack a downloaded .tar.gz into INTO and return the folder holding bin/ and src/."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            name = m.name.lstrip("/")
            if ".." in name.split("/") or m.issym() or m.islnk() or m.isdev():
                raise UpdateError("The download contains something unsafe (%s), so nothing was changed." % m.name[:60])
        try:
            tar.extractall(into, filter="data")      # (Python 3.12+)
        except TypeError:
            tar.extractall(into)
    for cand in [into] + [os.path.join(into, d) for d in sorted(os.listdir(into))] + [os.path.join(into, d, "yardcode") for d in sorted(os.listdir(into))]:
        if os.path.isfile(os.path.join(cand, "bin", "yardcode")) and os.path.isdir(os.path.join(cand, "src", "yardcode")):
            return cand
    raise UpdateError("The download doesn't contain yardcode (no bin/yardcode and src/yardcode).")


def from_server(modelapi):
    if modelapi is None or not modelapi.available:
        raise UpdateError("no nodeyard server is set (yardcode login)")
    from .modelapi import ModelAPIError
    import http.client
    import urllib.parse
    try:
        info = modelapi.request("GET", "/yardcode", timeout=30)
        u = urllib.parse.urlsplit(modelapi.base)
        port = u.port or (443 if u.scheme == "https" else 80)
        conn = (http.client.HTTPSConnection(u.hostname, port, timeout=60, context=net.ssl_context()) if u.scheme == "https" else http.client.HTTPConnection(u.hostname, port, timeout=60))
        conn.request("GET", u.path.rstrip("/") + "/api/v1/yardcode/bundle", headers={"Authorization": "Bearer " + modelapi.key, "X-Nodeyard": "1"})
        resp = conn.getresponse()
        data = resp.read(64 * 1024 * 1024)
        conn.close()
        if resp.status != 200:
            raise UpdateError("the server answered %d" % resp.status)
    except ModelAPIError as e:
        raise UpdateError(str(e))
    except OSError as e:
        raise UpdateError("can't reach the server (%s)" % e)
    return data, "your nodeyard server (%s)" % modelapi.base, info.get("version", "")


def from_github(ref="main"):
    url = "https://codeload.github.com/%s/tar.gz/%s" % (REPO, ref)
    try:
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=net.ssl_context()))
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "yardcode"}), timeout=60) as r:
            data = r.read(128 * 1024 * 1024)
    except (OSError, ValueError) as e:
        raise UpdateError("can't download from GitHub (%s)" % e)
    return data, "GitHub (%s, %s)" % (REPO, ref), ""


def from_dir(path):
    d = os.path.abspath(os.path.expanduser(path))
    for cand in (os.path.join(d, "yardcode"), d):
        if os.path.isfile(os.path.join(cand, "bin", "yardcode")) and os.path.isdir(os.path.join(cand, "src", "yardcode")):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                for rel, p in program_files(cand):
                    ti = tar.gettarinfo(p, arcname=rel)
                    ti.uid = ti.gid = 0
                    ti.uname = ti.gname = ""
                    with open(p, "rb") as f:
                        tar.addfile(ti, f)
            return buf.getvalue(), "the folder %s" % d, ""
    raise UpdateError("%s doesn't contain yardcode (no bin/yardcode and src/yardcode)." % path)


def _replace_folder(new, root):
    launcher = os.path.join(root, "bin", "yardcode")
    first = ""
    try:
        with open(launcher, "r", encoding="utf-8", errors="replace") as f:
            first = f.readline()
    except OSError:
        pass
    stage = root + ".new.%d" % os.getpid()
    old = root + ".old.%d" % os.getpid()
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(os.path.join(stage, "bin"))
    shutil.copy2(os.path.join(new, "bin", "yardcode"), os.path.join(stage, "bin", "yardcode"))
    shutil.copytree(os.path.join(new, "src", "yardcode"), os.path.join(stage, "src", "yardcode"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for extra in ("README.md",):
        if os.path.isfile(os.path.join(new, extra)):
            shutil.copy2(os.path.join(new, extra), os.path.join(stage, extra))
    if first.startswith("#!") and "env python3" not in first:       # keep the python this copy was installed with
        with open(os.path.join(stage, "bin", "yardcode"), "r", encoding="utf-8") as f:
            rest = f.read().split("\n", 1)[1]
        with open(os.path.join(stage, "bin", "yardcode"), "w", encoding="utf-8") as f:
            f.write(first.rstrip("\n") + "\n" + rest)
    os.chmod(os.path.join(stage, "bin", "yardcode"), 0o755)
    os.rename(root, old)
    try:
        os.rename(stage, root)
    except OSError:
        os.rename(old, root)
        raise
    shutil.rmtree(old, ignore_errors=True)


def _replace_zipapp(new, target):
    import zipapp
    stage = tempfile.mkdtemp(prefix="yardcode-zip-")
    try:
        shutil.copytree(os.path.join(new, "src", "yardcode"), os.path.join(stage, "yardcode"), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        with open(os.path.join(stage, "__main__.py"), "w") as f:
            f.write("import sys\n\nfrom yardcode.cli import main\n\nsys.exit(main())\n")
        tmp = target + ".new.%d" % os.getpid()
        zipapp.create_archive(stage, tmp, interpreter="/usr/bin/env python3", compressed=True)
        os.chmod(tmp, 0o755)
        os.replace(tmp, target)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def run_update(modelapi=None, source="auto", ref="main", directory="", check=False, force=False, say=print):
    """Returns 0 (done or already current), 1 (failed). `say` receives each message line."""
    try:
        mode, root = where()
    except UpdateError as e:
        say(str(e))
        return 1
    if mode == "checkout" and not force:
        say("This copy of yardcode runs from a git checkout (%s). Update it there with: git pull" % root)
        return 0
    target = root
    if mode != "zipapp" and not os.access(os.path.dirname(root) if not os.path.isdir(root) else root, os.W_OK) and not check:
        say("Can't write to %s. Run it as the account that installed yardcode, or: sudo yardcode update" % root)
        return 1
    order = {"auto": ["server", "github"], "server": ["server"], "github": ["github"], "dir": ["dir"]}[source if not directory else "dir"]
    errors, got = [], None
    for how in order:
        try:
            say("Looking for a newer yardcode on %s..." % {"server": "your nodeyard server", "github": "GitHub", "dir": directory}[how])
            got = from_server(modelapi) if how == "server" else from_github(ref) if how == "github" else from_dir(directory)
            break
        except UpdateError as e:
            errors.append("%s: %s" % (how, e))
            say("  not that way (%s)" % e)
    if got is None:
        say("Couldn't get a newer yardcode (%s)." % "; ".join(errors))
        return 1
    data, label, _version = got
    tmp = tempfile.mkdtemp(prefix="yardcode-update-")
    try:
        try:
            new = _extract(data, tmp)
        except (tarfile.TarError, OSError, EOFError) as e:
            say("The download was damaged (%s), so nothing was changed." % e)
            return 1
        except UpdateError as e:
            say(str(e))
            return 1
        mine = tree_hash(root) if mode != "zipapp" else ""
        theirs = tree_hash(new)
        new_version = ""
        try:
            with open(os.path.join(new, "src", "yardcode", "__init__.py"), "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("__version__"):
                        new_version = line.split("=", 1)[1].strip().strip("\"'")
        except OSError:
            pass
        if mine and mine == theirs:
            say("yardcode %s is already up to date (same as %s)." % (__version__, label))
            return 0
        if check:
            say("A different yardcode is available from %s (version %s; you have %s). Run: yardcode update" % (label, new_version or "?", __version__))
            return 0
        if mode == "zipapp":
            _replace_zipapp(new, root)
        else:
            _replace_folder(new, root)
        say("Updated yardcode %s -> %s from %s. Restart yardcode to use it." % (__version__, new_version or "?", label))
        return 0
    except (OSError, shutil.Error) as e:
        say("The update failed half way (%s). The old copy was kept if it could be." % e)
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_ = util
