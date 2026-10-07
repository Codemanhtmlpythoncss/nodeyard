"""Small helpers shared by every part: paths, files, sizes, token estimates."""
import json
import os
import re
import shutil
import tempfile
import time


def _xdg(var, fallback):
    base = os.environ.get(var) or os.path.join(os.path.expanduser("~"), fallback)
    return base


def config_dir():
    return os.environ.get("YARDCODE_HOME") or os.path.join(_xdg("XDG_CONFIG_HOME", ".config"), "yardcode")


def data_dir():
    return os.environ.get("YARDCODE_DATA") or os.path.join(_xdg("XDG_DATA_HOME", ".local/share"), "yardcode")


def ensure_dir(path, mode=0o755):
    os.makedirs(path, mode=mode, exist_ok=True)
    return path


def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {} if default is None else default


def atomic_write(path, text, mode=None):
    """Write a whole file at once (a crash never leaves half of it), keeping the mode of an existing file."""
    d = os.path.dirname(os.path.abspath(path))
    ensure_dir(d)
    if mode is None:
        try:
            mode = os.stat(path).st_mode & 0o7777
        except OSError:
            mode = 0o644
    fd, tmp = tempfile.mkstemp(prefix=".yc-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path, data, mode=0o600):
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode)


def est_tokens(text):
    """A rough token count (about 3.6 characters each); real counts come back from the server."""
    if not text:
        return 0
    return int(len(text) / 3.6) + 1


def truncate_middle(text, limit, note="chars"):
    """Keep the start and the end of a long text, with a marker for what was left out."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = int(limit * 0.65)
    tail = limit - head
    cut = len(text) - head - tail
    return "%s\n\n... [%d %s left out of the middle] ...\n\n%s" % (text[:head], cut, note, text[-tail:] if tail else "")


def human_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def human_tokens(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return "%.1fM" % (n / 1_000_000)
    if n >= 10_000:
        return "%dk" % round(n / 1000)
    if n >= 1000:
        return "%.1fk" % (n / 1000)
    return str(n)


def human_duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%ds" % seconds
    m, s = divmod(seconds, 60)
    if m < 60:
        return "%dm %02ds" % (m, s)
    h, m = divmod(m, 60)
    return "%dh %02dm" % (h, m)


def which(cmd):
    return shutil.which(cmd)


def now():
    return time.time()


def iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts or time.time()))


def slug(text, limit=40):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text or "").strip("-").lower()
    return s[:limit] or "x"


def is_binary(data):
    return b"\0" in data[:8192]


def shorten_path(path, cwd=None):
    """~/x or ./x instead of an absolute path, for display."""
    home = os.path.expanduser("~")
    cwd = cwd or os.getcwd()
    if path == cwd:
        return "."
    if path.startswith(cwd + os.sep):
        return path[len(cwd) + 1:]
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path
