"""music.py — an optional, self-hosted audio feature:

  library   the server plays the audio files in a folder the admin points it at. Tracks are listed and streamed
            (with HTTP Range, so seeking works) straight from that folder; nothing leaves the server but the audio.
  forward   the host's own audio output is captured with ffmpeg and streamed to the browser as live MP3, so whatever
            the server is playing comes out in the browser. Opt-in, and it degrades clearly when the host has no
            capture source (a headless server usually won't).
  SoundCloud search uses the public sc-pick CLI installed for the terminal user; likes are saved locally here and
  never sent to a SoundCloud account.

Config at <state_dir>/music.json: {"dir": "...", "forward_enabled": bool, "forward_source": "...", "sc_likes": []}.
The folder is the only place tracks are served from — every streamed path is resolved and checked to be inside it, so
the folder picker can't be turned into a way to read the rest of the disk.
"""
import glob
import json
import os
import pwd
import re
import secrets
import shutil
import subprocess
import threading
import time

AUDIO_EXT = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".ogg": "audio/ogg", ".oga": "audio/ogg",
             ".opus": "audio/ogg", ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".aac": "audio/aac",
             ".wav": "audio/wav", ".aiff": "audio/aiff", ".wma": "audio/x-ms-wma"}
MAX_TRACKS = 3000
_live_slots = threading.BoundedSemaphore(2)
_sc_slots = threading.BoundedSemaphore(3)

# Best-effort one-click to give a headless control node an audio stack, so live forwarding has something to capture:
# install PulseAudio, run it in system mode with a null sink (default) whose .monitor the dashboard can capture, and
# expose an anonymous unix socket at /run/pulse/native. A server-side player (e.g. sc-pick's mpv) then plays into the
# null sink and the dashboard forwards it to any browser. The dashboard unit sets PULSE_SERVER to this socket.
SERVER_AUDIO_SCRIPT = r"""set -e
echo '== install PulseAudio =='
if command -v apt-get >/dev/null 2>&1; then apt-get update && apt-get install -y pulseaudio pulseaudio-utils
elif command -v pacman >/dev/null 2>&1; then pacman -Sy --noconfirm pulseaudio libpulse
elif command -v dnf >/dev/null 2>&1; then dnf install -y pulseaudio pulseaudio-utils
else echo 'no known package manager'; fi
echo '== config: anonymous socket + null sink =='
mkdir -p /etc/pulse
cat > /etc/pulse/nodeyard-system.pa <<'PA'
load-module module-native-protocol-unix auth-anonymous=1 socket=/run/pulse/native
load-module module-null-sink sink_name=nodeyard sink_properties=device.description=NodeyardOutput
set-default-sink nodeyard
load-module module-always-sink
PA
echo '== service =='
cat > /etc/systemd/system/pulseaudio-system.service <<'SVC'
[Unit]
Description=PulseAudio system mode for nodeyard audio forwarding
After=network.target
[Service]
ExecStart=/usr/bin/pulseaudio --system --disallow-exit --exit-idle-time=-1 --file=/etc/pulse/nodeyard-system.pa
Restart=on-failure
RuntimeDirectory=pulse
RuntimeDirectoryMode=0755
SVC
systemctl daemon-reload
systemctl enable --now pulseaudio-system.service || echo 'could not start the service'
sleep 2
PULSE_SERVER=unix:/run/pulse/native pactl info 2>&1 | head -3 || true
echo '== done =='
"""


class Music:
    def __init__(self, state_dir, demo=False, terminal_user=""):
        self.path = os.path.join(state_dir or "/var/lib/nodeyard/dashboard", "music.json")
        self.demo = demo
        self.lock = threading.RLock()
        self.home = ""
        if terminal_user:
            try:
                self.home = pwd.getpwnam(terminal_user).pw_dir
            except KeyError:
                pass
        if not self.home:
            try:
                self.home = pwd.getpwuid(os.getuid()).pw_dir
            except (KeyError, OSError):
                self.home = "/root"

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

    def _expand(self, path):
        path = str(path or "").strip()
        if path == "~":
            return self.home
        if path.startswith("~/"):
            return os.path.join(self.home, path[2:])
        return os.path.expanduser(path)

    # -- library -------------------------------------------------------------

    def _base(self):
        d = (self.cfg().get("dir") or "").strip()
        return os.path.realpath(self._expand(d)) if d else ""

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
        start = os.path.realpath(self._expand(path)) if path else self.home
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
        shortcuts = []
        homes = [self.home]
        try:
            homes.extend(os.path.join("/home", n) for n in sorted(os.listdir("/home")))
        except OSError:
            pass
        seen = set()
        for home in homes:
            for candidate in (os.path.join(home, "Music", "soundcloud"), os.path.join(home, "Music"), home):
                candidate = os.path.realpath(candidate)
                if candidate not in seen and os.path.isdir(candidate):
                    seen.add(candidate)
                    shortcuts.append(candidate)
        for candidate in ("/srv", "/mnt", "/media"):
            if os.path.isdir(candidate) and candidate not in seen:
                seen.add(candidate)
                shortcuts.append(candidate)
        return {"ok": True, "path": start, "parent": "" if start == "/" else os.path.dirname(start),
                "dirs": names, "audio_here": audio_here, "shortcuts": shortcuts}

    # -- SoundCloud search and local likes via sc-pick ------------------------

    @staticmethod
    def _valid_sc_url(url):
        return bool(re.match(r"^https?://(www\.|m\.)?soundcloud\.com/[^\s]+$", str(url or "").strip(), re.I))

    def _tool(self, name):
        """Find a helper sc-pick installed, wherever it was run from. The dashboard runs as a different user (often
        root) than the person who ran sc-pick, so look in every home's sc-pick bin and /usr/local/bin, not just ours."""
        homes = [self.home, "/root"] + sorted(glob.glob("/home/*"))
        seen = set()
        for h in homes:
            if not h or h in seen:
                continue
            seen.add(h)
            c = os.path.join(h, ".local", "share", "sc-pick", "bin", name)
            if os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        return shutil.which(name) or ""

    def soundcloud_cli(self):
        """The public-search CLI installed by sc-pick (or system-wide by 'Update everything')."""
        return self._tool("soundcloud")

    def sc_search(self, query):
        query = str(query or "").strip()[:200]
        if len(query) < 2:
            return False, "Enter at least two characters to search.", []
        if self.demo:
            return True, "", [{"title": "Demo track", "artist": "Demo artist", "duration_ms": 185000,
                                "url": "https://soundcloud.com/demo/demo-track"}]
        yt = self.yt_dlp()
        if not yt:
            return False, "yt-dlp isn't installed on the server yet. Use Settings → Update everything (it installs yt-dlp), then search again.", []
        try:
            result = subprocess.run([yt, "-q", "--no-warnings", "--flat-playlist", "-J", "scsearch25:" + query],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", timeout=40)
        except subprocess.TimeoutExpired:
            return False, "SoundCloud search took too long. Try a shorter search.", []
        except OSError as e:
            return False, "Couldn't run the search: %s" % e, []
        if result.returncode:
            message = (result.stderr or result.stdout or "Search failed.").strip().splitlines()
            return False, "\n".join(message[-5:])[:600], []
        try:
            data = json.loads(result.stdout or "{}")
        except ValueError:
            return False, "Couldn't read the search results.", []
        rows = []
        for e in (data.get("entries") or []):
            if not isinstance(e, dict):
                continue
            url = e.get("webpage_url") or e.get("url") or ""
            if not self._valid_sc_url(url):
                continue
            dur = e.get("duration")
            try:
                duration_ms = int(float(dur) * 1000) if dur else 0
            except (TypeError, ValueError):
                duration_ms = 0
            rows.append({"title": str(e.get("title") or "Untitled")[:300],
                         "artist": str(e.get("uploader") or e.get("channel") or e.get("uploader_id") or "Unknown artist")[:200],
                         "duration_ms": max(0, duration_ms), "url": url[:1000]})
        return True, "", rows

    def sc_likes(self):
        rows = self.cfg().get("sc_likes")
        if not isinstance(rows, list):
            return []
        return [row for row in rows[:500] if isinstance(row, dict) and self._valid_sc_url(row.get("url"))]

    def sc_like(self, track, liked):
        if not isinstance(track, dict) or not self._valid_sc_url(track.get("url")):
            return False, "Choose a SoundCloud track from the search results.", []
        try:
            duration_ms = int(track.get("duration_ms") or 0)
        except (TypeError, ValueError):
            duration_ms = 0
        row = {"title": str(track.get("title") or "Untitled")[:300],
               "artist": str(track.get("artist") or "Unknown artist")[:200],
               "duration_ms": max(0, min(86_400_000, duration_ms)),
               "url": str(track["url"])[:1000]}
        with self.lock:
            cfg = self.cfg()
            saved = cfg.get("sc_likes")
            if not isinstance(saved, list):
                saved = []
            current = [x for x in saved if isinstance(x, dict) and self._valid_sc_url(x.get("url"))]
            current = [x for x in current if x.get("url") != row["url"]]
            if liked:
                current.insert(0, row)
            cfg["sc_likes"] = current[:500]
            self._save(cfg)
        return True, "", current[:500]

    # -- SoundCloud streaming & playlists (so tracks play in the browser) -----

    def yt_dlp(self):
        """yt-dlp, used to resolve a SoundCloud track's audio URL and list a set's tracks. Wherever sc-pick put it."""
        return self._tool("yt-dlp")

    def stream_ready(self):
        return self.demo or bool(self.yt_dlp() and self.ffmpeg())

    def sc_stream_url(self, url):
        """The direct audio URL for a SoundCloud track (yt-dlp -g). ffmpeg then transcodes it to MP3 for the browser."""
        yt = self.yt_dlp()
        if not yt or not self._valid_sc_url(url):
            return None
        try:
            r = subprocess.run([yt, "-q", "--no-warnings", "-f", "bestaudio/best", "-g", url],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
        return lines[0].strip() if lines else None

    def sc_playlist(self, url):
        """List the tracks of a SoundCloud playlist/set (or a user's tracks), so they can be played or saved."""
        if not self._valid_sc_url(url):
            return False, "Give a soundcloud.com playlist, set or user URL.", []
        if self.demo:
            return True, "Demo set", [{"title": "Demo track %d" % i, "artist": "Demo artist", "duration_ms": 180000,
                                       "url": "https://soundcloud.com/demo/track-%d" % i} for i in range(1, 6)]
        yt = self.yt_dlp()
        if not yt:
            return False, "yt-dlp isn't installed on the server. Use Settings → Update everything, then try again.", []
        try:
            r = subprocess.run([yt, "-q", "--no-warnings", "--flat-playlist", "-J", url],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", timeout=60)
        except subprocess.TimeoutExpired:
            return False, "Reading that playlist took too long.", []
        except OSError as e:
            return False, "Couldn't start yt-dlp: %s" % e, []
        if r.returncode:
            return False, ("\n".join((r.stderr or "").strip().splitlines()[-4:])[:400] or "Couldn't read that playlist."), []
        try:
            data = json.loads(r.stdout or "{}")
        except ValueError:
            return False, "Couldn't parse that playlist.", []
        entries = data.get("entries") if isinstance(data, dict) else None
        if entries is None:
            entries = [data] if isinstance(data, dict) else []
        rows = []
        for e in entries[:300]:
            if not isinstance(e, dict):
                continue
            u = e.get("webpage_url") or e.get("url") or ""
            if not self._valid_sc_url(u):
                continue
            dur = e.get("duration")
            try:
                duration_ms = int(float(dur) * 1000) if dur else 0
            except (TypeError, ValueError):
                duration_ms = 0
            rows.append({"title": str(e.get("title") or "Untitled")[:300],
                         "artist": str(e.get("uploader") or e.get("channel") or e.get("uploader_id") or "")[:200],
                         "duration_ms": max(0, duration_ms), "url": u[:1000]})
        title = str((data.get("title") if isinstance(data, dict) else "") or "Playlist")[:200]
        if not rows:
            return False, "No playable tracks found at that URL.", []
        return True, title, rows

    # -- local playlists (your own, saved in music.json) ---------------------

    @staticmethod
    def _playlists(cfg):
        pls = cfg.get("playlists")
        return [p for p in pls if isinstance(p, dict) and p.get("id")] if isinstance(pls, list) else []

    def playlists(self):
        return [{"id": p["id"], "name": p.get("name", "Playlist"), "count": len(p.get("tracks") or []),
                 "created": p.get("created")} for p in self._playlists(self.cfg())]

    def playlist_get(self, pid):
        return next((p for p in self._playlists(self.cfg()) if p["id"] == pid), None)

    def _norm_track(self, t):
        if not isinstance(t, dict):
            return None
        if t.get("kind") == "sc" and self._valid_sc_url(t.get("url")):
            try:
                dur = max(0, min(86_400_000, int(t.get("duration_ms") or 0)))
            except (TypeError, ValueError):
                dur = 0
            return {"kind": "sc", "url": str(t["url"])[:1000], "title": str(t.get("title") or "Untitled")[:300],
                    "artist": str(t.get("artist") or "")[:200], "duration_ms": dur}
        if t.get("kind") == "local" and t.get("path") and self.resolve(t.get("path")):
            return {"kind": "local", "path": str(t["path"])[:1000], "name": str(t.get("name") or t["path"])[:300],
                    "folder": str(t.get("folder") or "")[:300]}
        return None

    def playlist_op(self, action, pid=None, name=None, track=None, index=None):
        with self.lock:
            cfg = self.cfg()
            pls = self._playlists(cfg)
            if action == "create":
                pid = secrets.token_hex(6)
                pls.insert(0, {"id": pid, "name": (str(name or "").strip()[:120] or "New playlist"),
                               "created": int(time.time()), "tracks": []})
            else:
                p = next((x for x in pls if x["id"] == pid), None)
                if not p:
                    return False, "Playlist not found.", None
                if action == "rename":
                    p["name"] = str(name or p.get("name") or "Playlist").strip()[:120] or "Playlist"
                elif action == "delete":
                    pls = [x for x in pls if x["id"] != pid]
                elif action == "add":
                    tr = self._norm_track(track)
                    if not tr:
                        return False, "Can't add that track (is the music folder set, or the SoundCloud link valid?).", None
                    tracks = p.get("tracks") or []
                    key = tr.get("url") or tr.get("path")
                    if not any((x.get("url") or x.get("path")) == key for x in tracks):
                        tracks.append(tr)
                    p["tracks"] = tracks[:1000]
                elif action == "remove":
                    tracks = p.get("tracks") or []
                    try:
                        i = int(index)
                    except (TypeError, ValueError):
                        i = -1
                    if 0 <= i < len(tracks):
                        tracks.pop(i)
                    p["tracks"] = tracks
                else:
                    return False, "Unknown action.", None
            cfg["playlists"] = pls[:200]
            self._save(cfg)
        return True, "", pid


def register(ctx, args):
    demo = bool(getattr(args, "demo", False))
    music = Music(getattr(args, "state_dir", "") or "/var/lib/nodeyard/dashboard", demo=demo,
                  terminal_user=getattr(args, "terminal_user", ""))
    ctx.music = music

    def _status(h, q):
        c = music.cfg()
        have_ff = bool(music.ffmpeg())
        return h._json({"ok": True, "dir": c.get("dir", ""), "tracks": music.tracks(),
                        "forward": {"available": music.forward_available(), "reason": music.forward_reason(),
                                    "enabled": bool(c.get("forward_enabled")),
                                    "source": c.get("forward_source", ""), "auto_source": music.default_source() if have_ff else "",
                                    "sources": music.sources(), "ffmpeg": have_ff},
                        "sc": {"search_ready": demo or bool(music.yt_dlp()), "stream_ready": music.stream_ready(),
                               "likes": music.sc_likes()},
                        "playlists": music.playlists()})

    def _config(h, body):
        c = music.cfg()
        if "dir" in body:
            d = music._expand(body.get("dir") or "")
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

    def _sc_search(h, q):
        ok, error, tracks = music.sc_search(q.get("q", [""])[0])
        return h._json({"ok": ok, "error": error, "tracks": tracks}, 200 if ok else 400)

    def _sc_like(h, body):
        liked = body.get("liked") is True
        ok, error, likes = music.sc_like(body.get("track"), liked)
        return h._json({"ok": ok, "error": error, "likes": likes}, 200 if ok else 400)

    def _test(h, body):
        ok, msg = music.test_forward()
        return h._json({"ok": ok, "message": msg, "source": music.default_source()})

    def _setup_audio(h, body):
        if demo:
            return h._json({"ok": True, "message": "(demo) would install PulseAudio and a null sink on the server."})
        try:
            subprocess.Popen(["systemd-run", "--unit", "nodeyard-audio-setup", "--collect", "--setenv=HOME=/root",
                              "/bin/sh", "-c", SERVER_AUDIO_SCRIPT], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            return h._json({"ok": False, "error": "Couldn't start audio setup: %s" % e}, 500)
        return h._json({"ok": True, "message": "Setting up server audio (PulseAudio + a null sink). When it finishes, "
                        "redeploy (Settings → Update everything) so the dashboard connects to it, then play something on "
                        "the server and press Test capture. Watch: journalctl -u nodeyard-audio-setup -f"})

    def _sc_playlist(h, q):
        ok, msg, tracks = music.sc_playlist(q.get("url", [""])[0])
        return h._json({"ok": ok, "title": msg if ok else "", "error": "" if ok else msg, "tracks": tracks}, 200 if ok else 400)

    def _playlists(h, q):
        pid = q.get("id", [""])[0]
        if pid:
            p = music.playlist_get(pid)
            return h._json({"ok": bool(p), "playlist": p}, 200 if p else 404)
        return h._json({"ok": True, "playlists": music.playlists()})

    def _playlist(h, body):
        ok, msg, pid = music.playlist_op(str(body.get("action") or ""), pid=body.get("id"), name=body.get("name"),
                                         track=body.get("track"), index=body.get("index"))
        return h._json({"ok": ok, "error": msg, "id": pid, "playlists": music.playlists()}, 200 if ok else 400)

    def _sc_stream(h, q):
        url = q.get("url", [""])[0]
        if not music._valid_sc_url(url):
            return h._send(404, "Not found", "text/plain; charset=utf-8")
        yt, ff = music.yt_dlp(), music.ffmpeg()
        if demo or not yt or not ff:
            return h._send(503, "SoundCloud streaming needs yt-dlp and ffmpeg on the server (Settings → Update everything).",
                           "text/plain; charset=utf-8")
        if not _sc_slots.acquire(blocking=False):
            return h._send(429, "Too many streams at once. Try again in a moment.", "text/plain; charset=utf-8")
        proc = None
        try:
            media = music.sc_stream_url(url)
            if not media:
                return h._send(502, "Couldn't play that track (it may be private or download-only).", "text/plain; charset=utf-8")
            proc = subprocess.Popen([ff, "-hide_banner", "-loglevel", "error", "-i", media, "-vn", "-b:a", "192k",
                                     "-f", "mp3", "pipe:1"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
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
            _sc_slots.release()

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
                           "/api/music/browse": _browse, "/api/music/search": _sc_search,
                           "/api/music/sc-stream": _sc_stream, "/api/music/sc-playlist": _sc_playlist,
                           "/api/music/playlists": _playlists})
    ctx.post_routes.update({"/api/music/config": _config, "/api/audio/test": _test,
                            "/api/audio/setup-server": _setup_audio,
                            "/api/music/like": _sc_like, "/api/music/playlist": _playlist})


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
