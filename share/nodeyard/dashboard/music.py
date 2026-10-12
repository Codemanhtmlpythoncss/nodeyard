"""music.py — an optional, self-hosted audio feature, all server-side so a browser that can reach only the dashboard
can listen:

  library   the server plays the audio files in a folder the admin points it at. Tracks are listed and streamed
            (with HTTP Range, so seeking works) straight from that folder; nothing leaves the server but the audio.
  forward   the host's own audio output is captured with ffmpeg and streamed to the browser as live MP3, so whatever
            the server is playing comes out in the browser. Opt-in, and it degrades clearly when the host has no
            capture source (a headless server usually won't).

Config at <state_dir>/music.json: {"dir": "...", "forward_enabled": bool, "forward_source": "..."}. The folder is the
only place tracks are served from — every streamed path is resolved and checked to be inside it, so the folder picker
can't be turned into a way to read the rest of the disk.
"""
import json
import os
import shutil
import subprocess
import threading

AUDIO_EXT = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".ogg": "audio/ogg", ".oga": "audio/ogg",
             ".opus": "audio/ogg", ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".aac": "audio/aac",
             ".wav": "audio/wav", ".aiff": "audio/aiff", ".wma": "audio/x-ms-wma"}
MAX_TRACKS = 3000
_live_slots = threading.BoundedSemaphore(2)


class Music:
    def __init__(self, state_dir, demo=False):
        self.path = os.path.join(state_dir or "/var/lib/nodeyard/dashboard", "music.json")
        self.demo = demo

    def cfg(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                c = json.load(f)
            return c if isinstance(c, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, c):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(c, f)
        os.replace(tmp, self.path)

    # -- library -------------------------------------------------------------

    def _base(self):
        d = (self.cfg().get("dir") or "").strip()
        return os.path.realpath(d) if d else ""

    def tracks(self):
        base = self._base()
        if not base or not os.path.isdir(base):
            return []
        out = []
        for root, dirs, files in os.walk(base):
            depth = root[len(base):].count(os.sep)
            if depth >= 5:
                dirs[:] = []
                continue
            dirs.sort()
            for name in sorted(files):
                ext = os.path.splitext(name)[1].lower()
                if ext not in AUDIO_EXT:
                    continue
                full = os.path.join(root, name)
                rel = os.path.relpath(full, base)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    continue
                folder = os.path.dirname(rel)
                out.append({"path": rel, "name": os.path.splitext(name)[0], "folder": folder, "size": size})
                if len(out) >= MAX_TRACKS:
                    return out
        return out

    def resolve(self, rel):
        """The absolute path for a track, only if it is a real audio file inside the configured folder."""
        base = self._base()
        if not base or not rel or ".." in rel.split("/"):
            return None
        full = os.path.realpath(os.path.join(base, rel.lstrip("/")))
        if full != base and not full.startswith(base + os.sep):
            return None
        if os.path.splitext(full)[1].lower() not in AUDIO_EXT or not os.path.isfile(full):
            return None
        return full

    # -- audio forwarding ----------------------------------------------------

    @staticmethod
    def ffmpeg():
        return shutil.which("ffmpeg")

    def sources(self):
        """Capture sources the host offers (PulseAudio/PipeWire), for the picker; the .monitor ones are the output."""
        pactl = shutil.which("pactl")
        if not pactl or self.demo:
            return []
        try:
            out = subprocess.run([pactl, "list", "short", "sources"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        names = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[1]:
                names.append(parts[1])
        return names

    def forward_available(self):
        return bool(self.ffmpeg()) and (self.demo or bool(self.sources()))

    def live_command(self):
        c = self.cfg()
        source = (c.get("forward_source") or "default").strip()
        return [self.ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "pulse", "-i", source,
                "-ac", "2", "-b:a", "128k", "-f", "mp3", "-"]


def register(ctx, args):
    demo = bool(getattr(args, "demo", False))
    music = Music(getattr(args, "state_dir", "") or "/var/lib/nodeyard/dashboard", demo=demo)
    ctx.music = music

    def _status(h, q):
        c = music.cfg()
        return h._json({"ok": True, "dir": c.get("dir", ""), "tracks": music.tracks(),
                        "forward": {"available": music.forward_available(), "enabled": bool(c.get("forward_enabled")),
                                    "source": c.get("forward_source", ""), "sources": music.sources(),
                                    "ffmpeg": bool(music.ffmpeg())}})

    def _config(h, body):
        c = music.cfg()
        if "dir" in body:
            d = str(body.get("dir") or "").strip()
            if d and not os.path.isdir(os.path.expanduser(d)):
                return h._json({"ok": False, "error": "That folder doesn't exist on the server."}, 400)
            c["dir"] = os.path.expanduser(d)
        if "forward_enabled" in body:
            c["forward_enabled"] = bool(body.get("forward_enabled"))
        if "forward_source" in body:
            c["forward_source"] = str(body.get("forward_source") or "")[:200]
        music._save(c)
        return h._json({"ok": True})

    def _track(h, q):
        rel = (q.get("path", [""])[0])
        full = music.resolve(rel)
        if not full:
            return h._send(404, "Not found", "text/plain; charset=utf-8")
        _stream_file(h, full, AUDIO_EXT.get(os.path.splitext(full)[1].lower(), "application/octet-stream"))

    def _live(h, q):
        c = music.cfg()
        if not c.get("forward_enabled"):
            return h._send(403, "Audio forwarding is off.", "text/plain; charset=utf-8")
        if demo or not music.ffmpeg():
            return h._send(503, "Live audio isn't available on this server.", "text/plain; charset=utf-8")
        if not _live_slots.acquire(blocking=False):
            return h._send(429, "Too many listeners.", "text/plain; charset=utf-8")
        proc = None
        try:
            proc = subprocess.Popen(music.live_command(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            h.send_response(200)
            h.send_header("Content-Type", "audio/mpeg")
            h.send_header("Cache-Control", "no-store")
            h.send_header("Connection", "close")
            h.end_headers()
            if h.command == "HEAD":
                return
            while True:
                chunk = proc.stdout.read(16384)
                if not chunk:
                    break
                h.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            if proc is not None:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.SubprocessError:
                    pass
            _live_slots.release()

    ctx.get_routes.update({"/api/music": _status, "/api/music/track": _track, "/api/audio/live": _live})
    ctx.post_routes["/api/music/config"] = _config


def _stream_file(h, full, ctype):
    """Serve a file with HTTP Range support, so the browser's audio player can seek."""
    size = os.path.getsize(full)
    start, end = 0, size - 1
    status = 200
    rng = h.headers.get("Range", "")
    if rng.startswith("bytes="):
        try:
            s, _, e = rng[6:].partition("-")
            if s:
                start = int(s)
            if e:
                end = int(e)
            if start > end or start >= size:
                h.send_response(416)
                h.send_header("Content-Range", "bytes */%d" % size)
                h.end_headers()
                return
            end = min(end, size - 1)
            status = 206
        except ValueError:
            start, end, status = 0, size - 1, 200
    length = end - start + 1
    h.send_response(status)
    h.send_header("Content-Type", ctype)
    h.send_header("Accept-Ranges", "bytes")
    h.send_header("Content-Length", str(length))
    if status == 206:
        h.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
    h.send_header("Cache-Control", "no-store")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.end_headers()
    if h.command == "HEAD":
        return
    with open(full, "rb") as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(65536, remaining))
            if not chunk:
                break
            try:
                h.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break
            remaining -= len(chunk)
