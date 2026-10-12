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
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request

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

    def forward_reason(self):
        """Why live forwarding can't run right now ("" = it can). A headless server usually has no audio stack."""
        if not self.ffmpeg():
            return "ffmpeg isn't installed on the server."
        if self.demo:
            return ""
        if not shutil.which("pactl"):
            return "No PulseAudio/PipeWire (pactl) on the server — there's no audio system to capture."
        if not self.sources():
            return "No capture sources found. An audio server must be running for the dashboard's user (check XDG_RUNTIME_DIR / the PulseAudio socket)."
        return ""

    def forward_available(self):
        return self.forward_reason() == ""

    def default_source(self):
        """The source to capture. Auto (no manual pick needed): the monitor of the default output sink, i.e. whatever
        the server is playing. A source set in Settings overrides it."""
        configured = (self.cfg().get("forward_source") or "").strip()
        if configured:
            return configured
        pactl = shutil.which("pactl")
        if pactl and not self.demo:
            try:
                sink = subprocess.run([pactl, "get-default-sink"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      text=True, timeout=5).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                sink = ""
            if sink:
                return sink + ".monitor"
            for s in self.sources():          # else the first monitor source we can see
                if s.endswith(".monitor"):
                    return s
        return "default"

    def live_command(self):
        return [self.ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "pulse", "-i", self.default_source(),
                "-ac", "2", "-b:a", "128k", "-f", "mp3", "-"]

    def test_forward(self):
        """Capture ~1 second and report success or the actual ffmpeg error, so 'not working' is explainable."""
        if self.demo:
            return True, "(demo) capture not actually run."
        reason = self.forward_reason()
        if reason:
            return False, reason
        source = self.default_source()
        cmd = [self.ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "pulse", "-i", source, "-t", "1", "-f", "mp3", "-"]
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, err = p.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()
            return False, "ffmpeg didn't produce audio within 8 s (the source may be silent or wrong)."
        except OSError as e:
            return False, "Couldn't start ffmpeg: %s" % e
        if out:
            return True, "Capturing from '%s' works (%d bytes in 1 s)." % (source, len(out))
        return False, (err.decode("utf-8", "replace").strip() or "ffmpeg captured no audio from '%s'." % source)

    def browse(self, path):
        """List sub-folders of a path on the server, for the folder picker. Admin-only (the route is auth-gated)."""
        start = os.path.realpath(os.path.expanduser(path)) if path else os.path.expanduser("~")
        if not os.path.isdir(start):
            start = "/"
        try:
            names = []
            for name in sorted(os.listdir(start)):
                if name.startswith("."):
                    continue
                if os.path.isdir(os.path.join(start, name)):
                    names.append(name)
                if len(names) >= 500:
                    break
        except OSError as e:
            return {"ok": False, "error": "Can't open that folder: %s" % e, "path": start}
        audio_here = 0
        try:
            for name in os.listdir(start):
                if os.path.splitext(name)[1].lower() in AUDIO_EXT:
                    audio_here += 1
        except OSError:
            pass
        return {"ok": True, "path": start, "parent": "" if start == "/" else os.path.dirname(start),
                "dirs": names, "audio_here": audio_here,
                "shortcuts": [p for p in [os.path.expanduser("~/Music/soundcloud"), os.path.expanduser("~/Music"),
                                          os.path.expanduser("~"), "/srv", "/mnt", "/media"] if os.path.isdir(p)]}

    # -- SoundCloud via the user's own sc-pick / yt-dlp -----------------------

    @staticmethod
    def yt_dlp():
        """Prefer the yt-dlp that sc-pick installed, then one on PATH."""
        local = os.path.expanduser("~/.local/share/sc-pick/bin/yt-dlp")
        if os.path.isfile(local) and os.access(local, os.X_OK):
            return local
        return shutil.which("yt-dlp")

    def sc_ready(self):
        return bool(self.yt_dlp()) and bool(self._base() and os.path.isdir(self._base()))

    def soundcloud_download(self, url):
        """Download a SoundCloud track or set into the music folder with yt-dlp (what sc-pick uses), skipping anything
        already fetched (a yt-dlp archive, like sc-pick). The caller must have the right to download it."""
        ytdlp = self.yt_dlp()
        if not ytdlp:
            return False, "yt-dlp isn't installed. Run sc-pick once on the server (it installs yt-dlp), then try again."
        base = self._base()
        if not base or not os.path.isdir(base):
            return False, "Set a music folder first."
        if not re.match(r"^https?://(www\.|m\.|on\.)?soundcloud\.com/", url.strip(), re.I):
            return False, "Give a soundcloud.com track or set URL."
        archive = os.path.join(base, ".sc-archive.txt")
        out_tmpl = os.path.join(base, "%(uploader)s - %(title)s.%(ext)s")
        if "/sets/" in url:
            out_tmpl = os.path.join(base, "%(playlist_title)s", "%(uploader)s - %(title)s.%(ext)s")
        cmd = [ytdlp, "-f", "bestaudio/best", "-x", "--audio-format", "mp3", "--no-overwrites",
               "--download-archive", archive, "--no-progress", "-o", out_tmpl, url.strip()]
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
            out, _ = p.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            p.kill()
            return False, "The download took too long (over 10 minutes). Use sc-pick in the terminal for big playlists."
        except OSError as e:
            return False, "Couldn't start yt-dlp: %s" % e
        tail = "\n".join((out or "").strip().splitlines()[-12:])
        if p.returncode == 0:
            return True, tail or "Done."
        return False, tail or ("yt-dlp failed (exit %s)." % p.returncode)

    def sc_setup(self):
        """Set SoundCloud downloading up automatically: fetch the standalone yt-dlp binary if it isn't already there
        (this is what sc-pick does on first run). Returns (ok, message)."""
        if self.demo:
            return True, "(demo) nothing installed."
        if self.yt_dlp():
            return True, "yt-dlp is already installed."
        dest_dir = os.path.expanduser("~/.local/share/sc-pick/bin")
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as e:
            return False, "Couldn't create %s: %s" % (dest_dir, e)
        dest = os.path.join(dest_dir, "yt-dlp")
        try:
            urllib.request.urlretrieve("https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp", dest)  # noqa: S310 fixed URL
            os.chmod(dest, 0o755)
        except (OSError, urllib.error.URLError) as e:
            return False, "Couldn't download yt-dlp: %s" % e
        return (True, "Installed yt-dlp — you can download from SoundCloud now.") if self.yt_dlp() else (False, "The download finished but yt-dlp still isn't runnable.")


def register(ctx, args):
    demo = bool(getattr(args, "demo", False))
    music = Music(getattr(args, "state_dir", "") or "/var/lib/nodeyard/dashboard", demo=demo)
    ctx.music = music

    def _status(h, q):
        c = music.cfg()
        have_ff = bool(music.ffmpeg())
        return h._json({"ok": True, "dir": c.get("dir", ""), "tracks": music.tracks(),
                        "forward": {"available": music.forward_available(), "reason": music.forward_reason(),
                                    "enabled": bool(c.get("forward_enabled")),
                                    "source": c.get("forward_source", ""), "auto_source": music.default_source() if have_ff else "",
                                    "sources": music.sources(), "ffmpeg": have_ff},
                        "sc": {"ready": music.sc_ready(), "yt_dlp": bool(music.yt_dlp())}})

    def _config(h, body):
        c = music.cfg()
        if "dir" in body:
            d = os.path.expanduser(str(body.get("dir") or "").strip())
            if d:
                if not os.path.exists(d):
                    return h._json({"ok": False, "error": "That folder isn't on the server. The dashboard runs on the control node, not your Mac — use Browse to pick a folder that's actually there (e.g. ~/Music/soundcloud)."}, 400)
                if not os.path.isdir(d):
                    return h._json({"ok": False, "error": "That path is a file, not a folder."}, 400)
                if not os.access(d, os.R_OK | os.X_OK):
                    return h._json({"ok": False, "error": "The dashboard can't read that folder (permission). Check who the dashboard runs as."}, 400)
            c["dir"] = d
        if "forward_enabled" in body:
            c["forward_enabled"] = bool(body.get("forward_enabled"))
        if "forward_source" in body:
            c["forward_source"] = str(body.get("forward_source") or "")[:200]
        music._save(c)
        return h._json({"ok": True})

    def _browse(h, q):
        return h._json(music.browse(q.get("path", [""])[0]))

    def _test(h, body):
        ok, msg = music.test_forward()
        return h._json({"ok": ok, "message": msg, "source": music.default_source()})

    def _sc(h, body):
        if demo:
            return h._json({"ok": True, "text": "(demo) would download from SoundCloud into the music folder."})
        ok, msg = music.soundcloud_download(str(body.get("url") or ""))
        return h._json({"ok": ok, "text": msg} if ok else {"ok": False, "error": msg}, 200 if ok else 400)

    def _sc_setup(h, body):
        ok, msg = music.sc_setup()
        return h._json({"ok": ok, "message": msg} if ok else {"ok": False, "error": msg}, 200 if ok else 500)

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

    ctx.get_routes.update({"/api/music": _status, "/api/music/track": _track, "/api/audio/live": _live,
                           "/api/music/browse": _browse})
    ctx.post_routes.update({"/api/music/config": _config, "/api/audio/test": _test,
                            "/api/music/sc": _sc, "/api/music/sc-setup": _sc_setup})


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
