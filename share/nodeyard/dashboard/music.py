"""music.py — an optional, self-hosted audio feature:

  library   the server plays the audio files in a folder the admin points it at. Tracks are listed and streamed
            (with HTTP Range, so seeking works) straight from that folder; nothing leaves the server but the audio.
  forward   the host's own audio output is captured with ffmpeg and streamed to the browser as live MP3, so whatever
            the server is playing comes out in the browser. Opt-in, and it degrades clearly when the host has no
            capture source (a headless server usually won't).
  SoundCloud search uses the public sc-pick CLI installed for the terminal user; likes are saved locally here and
  never sent to a SoundCloud account.

Config at <state_dir>/music.json also keeps local SoundCloud likes, listening history, saved playlists and playback position.
The folder is the only place tracks are served from — every streamed path is resolved and checked to be inside it, so
the folder picker can't be turned into a way to read the rest of the disk.
"""
import collections
import concurrent.futures
import glob
import hashlib
import json
import base64
import os
import pwd
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import musicdiscover

AUDIO_EXT = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".ogg": "audio/ogg", ".oga": "audio/ogg",
             ".opus": "audio/ogg", ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".aac": "audio/aac",
             ".wav": "audio/wav", ".aiff": "audio/aiff", ".wma": "audio/x-ms-wma"}
MAX_TRACKS = 3000
DISCOVER_TTL = 6 * 3600       # new picks are looked for again after this (sooner when most have been played)
DISCOVER_KEEP = 40
MAX_DISLIKES = 300
DOWNLOAD_PREFIX = "__nodeyard_downloads__/"
UPLOAD_PREFIX = "__nodeyard_uploads__/"
UPLOAD_RE = re.compile(r"^[0-9a-f]{6,32}$")
UPLOAD_CHUNK_MAX = 3 * 1024 * 1024        # one piece of an upload
UPLOAD_FILE_MAX = 80 * 1024 * 1024        # one song
UPLOAD_LIB_MAX = 4 * 1024 * 1024 * 1024   # all uploaded songs together
UPLOAD_BODY_MAX = UPLOAD_CHUNK_MAX * 4 // 3 + 64 * 1024   # a chunk, as base64 JSON
EQ_MODES = {"flat", "bass", "treble", "rock", "electronic", "vocal", "classical"}
_live_slots = threading.BoundedSemaphore(2)
_sc_slots = threading.BoundedSemaphore(3)

# yt-dlp is a ~100 MB Python program and runs inside the dashboard's memory allowance (MemoryMax in its unit). Six of
# them at once (new songs + details + a search) went over it and the kernel stopped the dashboard itself: "Lost
# contact" every time Music opened, and the search never finished, so it started again on the next visit. Now at most
# two run at once (background lookups one at a time, so a search or play never waits long), at low priority, and they
# are the first thing stopped if memory still runs out, never the dashboard.
_yt_slots = threading.BoundedSemaphore(2)
_yt_bg = threading.BoundedSemaphore(1)
_YT_WRAP = ["sh", "-c", '{ echo 900 > /proc/self/oom_score_adj; } 2>/dev/null; command -v nice >/dev/null 2>&1 && exec nice -n 10 "$@"; exec "$@"', "yt-dlp"]


def _yt_run(argv, slot="user", **kw):
    """subprocess.run for yt-dlp (argv[0] is its path). slot: "user" (a search, playlist or download: one of two at a
    time), "bg" (new-song lookups: one at a time, and only while a user slot is free) or None (playback: never waits)."""
    if slot is None:
        return subprocess.run(_YT_WRAP + list(argv), **kw)
    if slot == "bg":
        with _yt_bg, _yt_slots:
            return subprocess.run(_YT_WRAP + list(argv), **kw)
    with _yt_slots:
        return subprocess.run(_YT_WRAP + list(argv), **kw)


# In-RAM cache of fetched SoundCloud audio (url -> (bytes, content_type)), so a played track loads once, plays
# seekably, and replays instantly. Nothing is written to the music folder unless the user explicitly downloads.
# Small: it lives inside the dashboard's memory allowance; older tracks come back from the disk cache (_cache_path),
# which the kernel can drop when memory is short.
_SC_CACHE_MAX = 40 * 1024 * 1024
_sc_cache = collections.OrderedDict()
_sc_cache_lock = threading.Lock()


def _sc_cache_get(url):
    with _sc_cache_lock:
        if url in _sc_cache:
            _sc_cache.move_to_end(url)
            return _sc_cache[url]
    return None


def _sc_cache_put(url, data, ctype):
    with _sc_cache_lock:
        _sc_cache[url] = (data, ctype)
        _sc_cache.move_to_end(url)
        total = sum(len(v[0]) for v in _sc_cache.values())
        while total > _SC_CACHE_MAX and len(_sc_cache) > 1:
            _, (old, _c) = _sc_cache.popitem(last=False)
            total -= len(old)

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


class MusicError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


class Music:
    def __init__(self, state_dir, demo=False, terminal_user=""):
        self.path = os.path.join(state_dir or "/var/lib/nodeyard/dashboard", "music.json")
        self.demo = demo
        self.lock = threading.RLock()
        self.cache_lock = threading.RLock()
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

    def cache_dir(self):
        return os.path.join(os.path.dirname(self.path), "music-cache")

    def download_dir(self):
        return os.path.join(os.path.dirname(self.path), "music-downloads")

    def upload_dir(self):
        return os.path.join(os.path.dirname(self.path), "music-uploads")

    def cache_limit(self):
        try:
            return max(1, min(200, int(self.cfg().get("cache_limit", 20))))
        except (TypeError, ValueError):
            return 20

    def cache_info(self):
        folder = self.cache_dir()
        try:
            count = sum(1 for name in os.listdir(folder) if name.endswith(".mp3") and os.path.isfile(os.path.join(folder, name)))
        except OSError:
            count = 0
        return {"limit": self.cache_limit(), "count": count, "folder": folder}

    def _cache_path(self, url):
        return os.path.join(self.cache_dir(), hashlib.sha256(url.encode("utf-8")).hexdigest() + ".mp3")

    def _prune_cache(self, limit=None):
        folder = self.cache_dir()
        try:
            rows = [(os.path.getmtime(os.path.join(folder, name)), os.path.join(folder, name))
                    for name in os.listdir(folder) if name.endswith(".mp3")]
        except OSError:
            return
        maximum = self.cache_limit() if limit is None else max(1, min(200, int(limit)))
        for _mtime, path in sorted(rows, reverse=True)[maximum:]:
            try:
                os.unlink(path)
            except OSError:
                pass

    def set_cache_limit(self, limit):
        try:
            amount = int(limit)
        except (TypeError, ValueError):
            return False
        if amount < 1 or amount > 200:
            return False
        with self.lock:
            cfg = self.cfg()
            cfg["cache_limit"] = amount
            self._save(cfg)
        with self.cache_lock:
            self._prune_cache(amount)
        return True

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

    def playback(self):
        state = self.cfg().get("playback")
        return state if isinstance(state, dict) else {}

    def set_playback(self, body):
        with self.lock:
            cfg = self.cfg()
            if body.get("clear") is True:
                cfg.pop("playback", None)
                self._save(cfg)
                return True, "", {}
        tracks = body.get("queue")
        if not isinstance(tracks, list) or len(tracks) > MAX_TRACKS:
            return False, "Invalid playback queue.", {}
        queue = []
        for track in tracks:
            if not isinstance(track, dict):
                continue
            url = str(track.get("url") or "").strip()
            if self._valid_sc_url(url):
                try:
                    duration = max(0, min(86_400_000, int(track.get("duration_ms") or 0)))
                except (TypeError, ValueError):
                    duration = 0
                queue.append({"kind": "sc", "url": url[:1000],
                              "title": str(track.get("title") or "Untitled")[:300],
                              "artist": str(track.get("artist") or "SoundCloud")[:200],
                              "duration_ms": duration,
                              "artwork_url": track.get("artwork_url") if self._soundcloud_art_url(track.get("artwork_url")) else ""})
            elif track.get("path") and self.resolve(str(track.get("path"))):
                queue.append({"kind": "local", "path": str(track["path"])[:1000],
                              "name": str(track.get("name") or track["path"])[:300],
                              "folder": str(track.get("folder") or "")[:300]})
        if not queue:
            return False, "The queue has no playable tracks.", {}
        try:
            index = int(body.get("index", 0))
            position = max(0.0, min(86_400.0, float(body.get("position", 0))))
            volume = max(0.0, min(1.0, float(body.get("volume", 1))))
            rate = max(0.5, min(2.0, float(body.get("rate", 1))))
        except (TypeError, ValueError):
            return False, "Invalid playback position, volume or speed.", {}
        index = max(0, min(index, len(queue) - 1))
        repeat = body.get("repeat") if body.get("repeat") in ("off", "all", "one") else "off"
        eq_value = body.get("eq")
        eq = eq_value if isinstance(eq_value, str) and eq_value in EQ_MODES else "flat"
        try:
            history_seconds = max(0, min(86_400, int(float(body.get("history_seconds") or 0))))
        except (TypeError, ValueError):
            history_seconds = 0
        state = {"queue": queue, "index": index, "position": position, "volume": volume, "rate": rate, "eq": eq,
                 "playing": body.get("playing") is True, "shuffle": body.get("shuffle") is True,
                 "repeat": repeat, "history_id": str(body.get("history_id") or "")[:80], "history_seconds": history_seconds,
                 "handoff": body.get("handoff") is True, "updated": time.time()}
        with self.lock:
            cfg = self.cfg()
            cfg["playback"] = state
            self._save(cfg)
        return True, "", state

    def history_event(self, body):
        """Keep a compact, private listening history in the dashboard's music.json."""
        track = body.get("track")
        if not isinstance(track, dict):
            return False, "Choose a track from your music library.", []
        item = self._norm_track(track)
        if not item:
            return False, "That track is no longer available in your music library.", []
        event_id = str(body.get("id") or "")[:80]
        action = str(body.get("action") or "start")
        if not event_id or action not in ("start", "progress", "complete"):
            return False, "Invalid listening history update.", []
        try:
            seconds = max(0, min(86_400, int(float(body.get("seconds") or 0))))
        except (TypeError, ValueError):
            seconds = 0
        key = item.get("url") or item.get("path")
        with self.lock:
            cfg = self.cfg()
            rows = cfg.get("history") if isinstance(cfg.get("history"), list) else []
            row = next((x for x in rows if isinstance(x, dict) and x.get("id") == event_id), None)
            if row is None:
                row = dict(item, id=event_id, key=key, played_at=int(time.time()), seconds=0, completed=False)
                if item.get("kind") == "local":
                    row["library_dir"] = self._base()
                rows.insert(0, row)
            if (row.get("url") or row.get("path")) != key:
                return False, "That listening session does not match the track.", []
            row["seconds"] = max(int(row.get("seconds") or 0), seconds)
            if action == "complete":
                row["completed"] = True
            cfg["history"] = rows[:500]
            self._save(cfg)
        return True, "", len(cfg["history"])

    def ai_library(self, action="overview", query="", offset=0, limit=50):
        """The AI can only see the configured library and music the user has liked or played."""
        cfg = self.cfg()
        likes = self.sc_likes()
        history = cfg.get("history") if isinstance(cfg.get("history"), list) else []
        root = self._base()
        history = [x for x in history if isinstance(x, dict) and self._norm_track(x) and
                   (x.get("kind") != "local" or x.get("library_dir") == root)]
        local = self.tracks() if action in ("overview", "library") else []
        playlists = self._playlists(cfg)
        def public_track(track, kind):
            norm = self._norm_track(dict(track, kind=kind))
            if not norm:
                return None
            for field in ("seconds", "played_at", "completed"):
                if field in track:
                    norm[field] = track[field]
            return norm
        if action == "overview":
            return {"action": "overview", "local_track_count": len(local), "liked_count": len(likes),
                    "history_count": len(history), "playlists": [{"name": p.get("name", "Playlist"), "count": len(p.get("tracks") or [])} for p in playlists[:100]],
                    "recent_history": [row for row in (public_track(x, x.get("kind") or ("sc" if x.get("url") else "local")) for x in history[:10]) if row],
                    "liked_songs": [row for row in (public_track(x, "sc") for x in likes[:10]) if row]}
        rows = likes if action == "liked" else history if action == "history" else local if action == "library" else None
        if rows is None:
            return {"error": "action must be overview, liked, history or library."}
        if action == "library":
            term = str(query or "").strip().casefold()
            if term:
                words = [w for w in re.split(r"\s+", term) if w]
                rows = [x for x in rows if all(w in (x.get("name", "") + " " + x.get("folder", "")).casefold() for w in words)]
        try:
            off = max(0, int(offset))
            size = max(1, min(50, int(limit)))
        except (TypeError, ValueError):
            off, size = 0, 50
        kind = "sc" if action == "liked" else None
        page = []
        for row in rows[off:off + size]:
            row_kind = kind or row.get("kind") or ("sc" if row.get("url") else "local")
            item = public_track(row, row_kind)
            if item:
                page.append(item)
        return {"action": action, "total": len(rows), "offset": off, "next_offset": off + size if off + size < len(rows) else None,
                "tracks": page}

    def create_ai_playlist(self, name, tracks):
        """Save an AI playlist only from tracks already present in the user's library, likes or history."""
        if not isinstance(tracks, list) or not tracks or len(tracks) > 200:
            return False, "Choose between 1 and 200 tracks.", None
        cfg = self.cfg()
        allowed = {}
        for track in self.tracks():
            norm = self._norm_track(dict(track, kind="local"))
            if norm:
                allowed[norm.get("url") or norm.get("path")] = norm
        for track in self.sc_likes() + (self._discover_load().get("tracks") or []):   # (new picks too: the DJ mixes them in)
            norm = self._norm_track(dict(track, kind="sc"))
            if norm:
                allowed[norm.get("url") or norm.get("path")] = norm
        for track in (cfg.get("history") if isinstance(cfg.get("history"), list) else []):
            if isinstance(track, dict) and track.get("kind") == "local" and track.get("library_dir") != self._base():
                continue
            norm = self._norm_track(track)
            if norm:
                allowed[norm.get("url") or norm.get("path")] = norm
        selected = []
        seen = set()
        for track in tracks:
            if not isinstance(track, dict):
                continue
            key = str(track.get("url") or track.get("path") or "")
            if key in allowed and key not in seen:
                selected.append(allowed[key])
                seen.add(key)
        if not selected:
            return False, "The playlist needs tracks from the configured library, liked songs or listening history.", None
        with self.lock:
            cfg = self.cfg()
            playlists = self._playlists(cfg)
            pid = secrets.token_hex(6)
            playlist = {"id": pid, "name": (str(name or "").strip()[:120] or "AI playlist"),
                        "created": int(time.time()), "tracks": selected}
            cfg["playlists"] = [playlist] + playlists[:199]
            self._save(cfg)
        return True, "", playlist

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

    def reset(self):
        """Full refresh: walk the music folder again next time it's asked for."""
        self._tracks_cache = None

    def tracks(self):
        """The library's audio files. Walking a big folder tree takes a while, and the Music page, the DJ, the AI and the
        recommender all ask: the list is kept for 30 s, or until the folder (or the downloads folder) changes."""
        base = self._base()
        roots = [x for x in (base, self.download_dir(), self.upload_dir()) if x]
        try:
            key = (base, tuple(os.stat(x).st_mtime_ns if os.path.isdir(x) else 0 for x in roots))
        except OSError:
            key = None
        hit = getattr(self, "_tracks_cache", None)
        if key is not None and hit and hit[0] == key and time.time() - hit[1] < 30:
            return list(hit[2])
        out = self._scan_tracks(base)
        if key is not None:
            self._tracks_cache = (key, time.time(), out)
        return list(out)

    def _scan_tracks(self, base):
        out = []
        roots = [(base, "")] if base and os.path.isdir(base) else []
        downloaded = self.download_dir()
        if os.path.isdir(downloaded):
            roots.append((downloaded, DOWNLOAD_PREFIX))
        uploaded = self.upload_dir()
        if os.path.isdir(uploaded):
            roots.append((uploaded, UPLOAD_PREFIX))
        for library, prefix in roots:
            for root, dirs, files in os.walk(library):
                depth = root[len(library):].count(os.sep)
                if depth >= 5:
                    dirs[:] = []
                    continue
                dirs.sort()
                for name in sorted(files):
                    ext = os.path.splitext(name)[1].lower()
                    if ext not in AUDIO_EXT:
                        continue
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, library)
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        continue
                    folder = os.path.dirname(rel)
                    if prefix:
                        top = "Uploads" if prefix == UPLOAD_PREFIX else "Downloads"
                        folder = os.path.join(top, folder) if folder else top
                    out.append({"path": prefix + rel, "name": os.path.splitext(name)[0], "folder": folder, "size": size})
                    if len(out) >= MAX_TRACKS:
                        return out
        return out

    def resolve(self, rel):
        """The absolute path for a track, only if it is a real audio file inside the configured folder."""
        rel = str(rel or "")
        base = self._base()
        if rel.startswith(DOWNLOAD_PREFIX):
            base = self.download_dir()
            rel = rel[len(DOWNLOAD_PREFIX):]
        elif rel.startswith(UPLOAD_PREFIX):
            base = self.upload_dir()
            rel = rel[len(UPLOAD_PREFIX):]
        if not base or not rel or ".." in rel.split("/"):
            return None
        base = os.path.realpath(base)
        full = os.path.realpath(os.path.join(base, rel.lstrip("/")))
        if full != base and not full.startswith(base + os.sep):
            return None
        if os.path.splitext(full)[1].lower() not in AUDIO_EXT or not os.path.isfile(full):
            return None
        return full

    # -- uploading your own songs --------------------------------------------

    @staticmethod
    def _safe_name(name, ext):
        """A filename of the user's title plus the real extension, with nothing that could escape the folder."""
        stem = re.sub(r"[^A-Za-z0-9 ._()\-]", "_", os.path.basename(str(name or ""))).strip(" ._") or "track"
        stem = re.sub(r"\s+", " ", stem)[:120]
        if stem.lower().endswith(ext):
            stem = stem[:-len(ext)].strip(" ._") or "track"
        return stem + ext

    def upload(self, body):
        """One ordered piece of a song upload; the last one ("done") checks it and adds it to your library.

        Chunks are base64 in JSON (the dashboard has no multipart parser), assembled into music-uploads/."""
        uid = str(body.get("id", ""))
        if not UPLOAD_RE.match(uid):
            raise MusicError("Bad upload.")
        ext = ("." + re.sub(r"[^a-z0-9]", "", str(body.get("ext", "")).lower().lstrip(".")))[:6]
        if ext not in AUDIO_EXT:
            raise MusicError("That isn't an audio file we can play (MP3, M4A, FLAC, OGG, Opus, WAV, AAC).")
        try:
            data = base64.b64decode(str(body.get("data", "")), validate=True)
            offset = int(body.get("offset", 0))
        except (ValueError, TypeError):
            raise MusicError("Bad upload data.")
        if len(data) > UPLOAD_CHUNK_MAX:
            raise MusicError("Upload pieces are 3 MB at most.", 413)
        up = self.upload_dir()
        tmp = os.path.join(up, ".part-" + uid)
        with self.lock:
            os.makedirs(up, mode=0o755, exist_ok=True)
            if offset == 0:
                for n in os.listdir(up):   # forget pieces nobody finished
                    f = os.path.join(up, n)
                    if n.startswith(".part-") and time.time() - os.path.getmtime(f) > 3600:
                        try:
                            os.remove(f)
                        except OSError:
                            pass
            size = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            if offset != size:
                raise MusicError("The upload got out of step. Try again.", 409)
            if size + len(data) > UPLOAD_FILE_MAX:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise MusicError("That song is too big (80 MB at most).", 413)
            with open(tmp, "ab") as f:
                f.write(data)
            size += len(data)
            if not body.get("done"):
                return {"received": size}
            have = 0
            for n in os.listdir(up):
                if not n.startswith(".part-"):
                    try:
                        have += os.path.getsize(os.path.join(up, n))
                    except OSError:
                        pass
            if have + size > UPLOAD_LIB_MAX:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise MusicError("Your uploaded songs are full (4 GB). Delete some first.", 413)
            final = os.path.join(up, self._safe_name(body.get("name"), ext))
            stem, e = os.path.splitext(final)
            i = 2
            while os.path.exists(final):
                final = "%s (%d)%s" % (stem, i, e)
                i += 1
            os.replace(tmp, final)
            os.chmod(final, 0o644)
        self.reset()
        rel = os.path.relpath(final, up)
        return {"ok": True, "path": UPLOAD_PREFIX + rel, "name": os.path.splitext(os.path.basename(final))[0],
                "folder": "Uploads", "size": os.path.getsize(final)}

    def cancel_upload(self, uid):
        """Remove a partial upload after the browser reports a failed chunk."""
        uid = str(uid or "")
        if not UPLOAD_RE.match(uid):
            raise MusicError("Bad upload.")
        tmp = os.path.join(self.upload_dir(), ".part-" + uid)
        with self.lock:
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass
            except OSError as e:
                raise MusicError("Couldn't clean up the partial upload: %s" % e, 500)
        return {"ok": True}

    def delete_upload(self, rel):
        """Delete one uploaded song (only a file inside music-uploads/)."""
        rel = str(rel or "")
        if rel.startswith(UPLOAD_PREFIX):
            rel = rel[len(UPLOAD_PREFIX):]
        up = os.path.realpath(self.upload_dir())
        if not rel or ".." in rel.split("/"):
            raise MusicError("Unknown song.", 404)
        full = os.path.realpath(os.path.join(up, rel.lstrip("/")))
        if (full != up and not full.startswith(up + os.sep)) or not os.path.isfile(full):
            raise MusicError("Unknown song.", 404)
        with self.lock:
            os.remove(full)
        self.reset()
        return {"ok": True}

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
        return bool(re.match(r"^https?://(www\.|m\.|on\.|api\.)?soundcloud\.com/[^\s]+$", str(url or "").strip(), re.I))

    @staticmethod
    def _soundcloud_art_url(url):
        try:
            url = str(url or "")
            if len(url) > 2000:
                return False
            parsed = urllib.parse.urlparse(url)
            return (parsed.scheme == "https" and parsed.hostname == "i1.sndcdn.com" and not parsed.username and
                    not parsed.password and parsed.port in (None, 443) and len(parsed.path) < 1000)
        except ValueError:
            return False

    def artwork(self, path="", image_url=""):
        """Return an approved SoundCloud cover or a cover embedded in a confined local track."""
        folder = os.path.join(self.cache_dir(), "covers")
        if image_url:
            if not self._soundcloud_art_url(image_url):
                return None, ""
            key = hashlib.sha256(image_url.encode("utf-8")).hexdigest()
            cached = os.path.join(folder, key + ".img")
            typefile = cached + ".type"
            try:
                with open(cached, "rb") as f:
                    data = f.read()
                with open(typefile, "r", encoding="ascii") as f:
                    ctype = f.read(32)
                if data and ctype in ("image/jpeg", "image/png", "image/webp"):
                    return data, ctype
            except OSError:
                pass
            try:
                class SafeRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, request, fp, code, message, headers, new_url):
                        if not Music._soundcloud_art_url(new_url):
                            return None
                        return super().redirect_request(request, fp, code, message, headers, new_url)
                request = urllib.request.Request(image_url, headers={"User-Agent": "Nodeyard Music/1.0"})
                with urllib.request.build_opener(SafeRedirect).open(request, timeout=8) as response:
                    if not self._soundcloud_art_url(response.geturl()):
                        return None, ""
                    ctype = response.headers.get_content_type()
                    data = response.read(2 * 1024 * 1024 + 1)
                if ctype not in ("image/jpeg", "image/png", "image/webp") or len(data) > 2 * 1024 * 1024:
                    return None, ""
                os.makedirs(folder, mode=0o700, exist_ok=True)
                with open(cached, "wb") as f:
                    os.chmod(cached, 0o600)
                    f.write(data)
                with open(typefile, "w", encoding="ascii") as f:
                    os.chmod(typefile, 0o600)
                    f.write(ctype)
                return data, ctype
            except (OSError, ValueError, urllib.error.URLError):
                return None, ""
        full = self.resolve(path)
        if not full:
            return None, ""
        stem, _ext = os.path.splitext(full)
        for ext, ctype in ((".jpg", "image/jpeg"), (".jpeg", "image/jpeg"), (".png", "image/png"), (".webp", "image/webp")):
            try:
                with open(stem + ext, "rb") as f:
                    data = f.read(2 * 1024 * 1024 + 1)
                if data and len(data) <= 2 * 1024 * 1024:
                    return data, ctype
            except OSError:
                pass
        ffmpeg = self.ffmpeg()
        if not ffmpeg:
            return None, ""
        try:
            signature = "%s:%s:%s" % (full, os.path.getmtime(full), os.path.getsize(full))
            cached = os.path.join(folder, hashlib.sha256(signature.encode("utf-8")).hexdigest() + ".jpg")
            with open(cached, "rb") as f:
                data = f.read(2 * 1024 * 1024 + 1)
            if data and len(data) <= 2 * 1024 * 1024:
                return data, "image/jpeg"
        except OSError:
            pass
        try:
            result = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", full, "-map", "0:v:0",
                                     "-frames:v", "1", "-vf", "scale=300:300:force_original_aspect_ratio=decrease",
                                     "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=12)
            if result.returncode or not result.stdout or len(result.stdout) > 2 * 1024 * 1024:
                return None, ""
            os.makedirs(folder, mode=0o700, exist_ok=True)
            with open(cached, "wb") as f:
                os.chmod(cached, 0o600)
                f.write(result.stdout)
            return result.stdout, "image/jpeg"
        except (OSError, subprocess.SubprocessError):
            return None, ""

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

    def sc_is_playable(self, url):
        """True if yt-dlp can actually fetch this track — i.e. it isn't DRM-protected (Go+-only) or private. A quick
        --simulate (resolve only, no download). Used by the 'hide DRM' search filter."""
        yt = self.yt_dlp()
        if not yt or not self._valid_sc_url(url):
            return False
        try:
            r = _yt_run([yt, "-q", "--no-warnings", "--no-playlist", "--simulate", url],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            return r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def sc_search(self, query, nodrm=False):
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
            result = _yt_run([yt, "-q", "--no-warnings", "--flat-playlist", "-J", "scsearch25:" + query],
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
                         "duration_ms": max(0, duration_ms), "url": url[:1000],
                         "artwork_url": e.get("thumbnail") if self._soundcloud_art_url(e.get("thumbnail")) else ""})
        if nodrm and rows:
            # (one batched look-up instead of a yt-dlp per result: eight at once ran the dashboard out of memory)
            playable = self._sc_details([t["url"] for t in rows], slot="user")
            if playable:
                rows = [t for t in rows if self._sc_clean_url(t["url"]) in playable]
        return True, "", rows

    # -- new songs for you (musicdiscover.py does the thinking; this part talks to SoundCloud through yt-dlp) ----------

    def _discover_path(self):
        return os.path.join(os.path.dirname(self.path), "music-discover.json")

    def _discover_load(self):
        try:
            with open(self._discover_path(), "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _discover_save(self, d):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        tmp = self._discover_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, self._discover_path())

    def _heard_urls(self, cfg=None):
        cfg = cfg or self.cfg()
        heard = {x.get("url") for x in (cfg.get("history") or []) if isinstance(x, dict) and x.get("url")}
        heard |= {x.get("url") for x in self.sc_likes()}
        heard |= {x.get("url") for x in (cfg.get("sc_dislikes") or []) if isinstance(x, dict)}
        return heard

    def discover(self, refresh=False):
        """New songs picked for this listener. Answers at once from the last search; looks again in the background when
        asked to, when the picks are old, or when most of them have been played."""
        d = self._discover_load()
        heard = self._heard_urls()
        fresh = [t for t in (d.get("tracks") or []) if t.get("url") not in heard]
        now = time.time()
        age = now - (d.get("updated") or 0)
        # (few picks left only counts once the last search is a while old: an empty result must not search again on every look)
        stale = not d.get("updated") or age > DISCOVER_TTL or (len(fresh) < 8 and age > 900)
        can = self.demo or bool(self.yt_dlp())
        # a look that started and never finished (the dashboard was stopped mid-way) isn't retried by itself for 10 minutes
        tried = d.get("attempt") or 0
        unfinished = tried > (d.get("updated") or 0) and now - tried < 600
        if can and ((stale and not unfinished) or (refresh and (age > 45 or not fresh))):
            self._discover_start()
        return {"tracks": fresh, "updated": d.get("updated") or 0, "building": getattr(self, "_discover_busy", False),
                "error": d.get("error", "") if can else "Finding new songs needs yt-dlp on the server (Settings → Update everything).",
                "seeds": d.get("seeds") or []}

    def _discover_start(self):
        with self.lock:
            if getattr(self, "_discover_busy", False):
                return
            self._discover_busy = True
        threading.Thread(target=self._discover_run, daemon=True, name="music-discover").start()

    def _discover_run(self):
        prev = self._discover_load()
        try:
            self._discover_save(dict(prev, attempt=time.time()))
            cfg = self.cfg()
            history = [x for x in (cfg.get("history") or []) if isinstance(x, dict)]
            dislikes = [x for x in (cfg.get("sc_dislikes") or []) if isinstance(x, dict)]
            p = musicdiscover.profile(self.sc_likes(), history, self.tracks(), dislikes)
            rotation = int(prev.get("rotation") or 0) + 1
            seeds, queries = musicdiscover.plan(p, rotation)
            if not seeds and not queries:
                self._discover_save(dict(prev, updated=time.time(), rotation=rotation, tracks=prev.get("tracks") or [],
                                         error="Like or listen to a few songs first: new picks come from your taste."))
                return
            cands = {}

            def add(row, via):
                url = self._sc_clean_url(row.get("url"))
                if not url:
                    return
                c = cands.setdefault(url, {"title": row.get("title") or "", "artist": row.get("artist") or "", "duration_ms": row.get("duration_ms") or 0,
                                           "artwork_url": row.get("artwork_url") or "", "genres": row.get("genres") or [], "tags": row.get("tags") or [],
                                           "likes": row.get("likes") or 0, "via": []})
                for k in ("artist", "duration_ms", "artwork_url", "genres", "tags", "likes"):
                    if not c.get(k) and row.get(k):
                        c[k] = row[k]
                c["via"].append(via)

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
                related = [ex.submit(self._sc_related, s["url"]) for s in seeds]
                searches = [ex.submit(self._sc_search_rich, q, 15) for q, _kind in queries]
                top = max([s["w"] for s in seeds] or [1.0])
                for s, fut in zip(seeds, related):
                    label = s["title"] or "a song you like"
                    for i, row in enumerate(fut.result()[:25]):
                        add(row, ("seed", label, (s["w"] / top) / (1 + 0.12 * i)))
                for (q, kind), fut in zip(queries, searches):
                    base = 0.5 if kind == "artist" else 0.4
                    for i, row in enumerate(fut.result()):
                        add(row, ("search", q, base / (1 + 0.15 * i)))
            # look the best ones up properly: their artist, length, cover, genre, and that they really play
            first = musicdiscover.rank(cands, p, limit=54, per_artist=3)
            details = self._sc_details([c["url"] for c in first])
            playable = {}
            for url, info in details.items():
                if url in cands:
                    c = cands[url]
                    c.update({k: v for k, v in info.items() if v})
                    playable[url] = c
            picks = musicdiscover.rank(playable, p, limit=DISCOVER_KEEP)
            keep = ("url", "title", "artist", "duration_ms", "artwork_url", "genres", "tags", "likes", "why", "score")
            tracks = [dict({k: c.get(k) for k in keep}, kind="sc", found=int(time.time())) for c in picks]
            if not tracks and prev.get("tracks"):
                tracks = prev["tracks"]
            self._discover_save({"updated": time.time(), "rotation": rotation, "tracks": tracks, "error": "" if tracks else "No new songs turned up this time.",
                                 "seeds": [s["title"] for s in seeds if s.get("title")][:5] + [q for q, _ in queries][:4]})
        except Exception as e:  # noqa: BLE001 -- keep the last good picks, say why
            self._discover_save(dict(prev, updated=time.time(), error="Couldn't look for new songs: %s" % str(e)[:200]))
        finally:
            self._discover_busy = False

    def discover_feedback(self, action, track):
        """"Not for me" on a pick: it goes, and that artist counts against future picks (two strikes leaves them out)."""
        url = self._sc_clean_url((track or {}).get("url"))
        if not url:
            return False, "Choose a SoundCloud track.", None
        with self.lock:
            cfg = self.cfg()
            rows = [x for x in (cfg.get("sc_dislikes") or []) if isinstance(x, dict) and x.get("url") != url]
            if action == "dislike":
                rows.insert(0, {"url": url, "title": str(track.get("title") or "")[:300], "artist": str(track.get("artist") or "")[:200], "at": int(time.time())})
            cfg["sc_dislikes"] = rows[:MAX_DISLIKES]
            self._save(cfg)
        return True, "", self.discover()

    @classmethod
    def _sc_clean_url(cls, url):
        url = str(url or "").strip().split("?", 1)[0].split("#", 1)[0].rstrip("/")
        return url if cls._valid_sc_url(url) else ""

    def _yt_entries(self, target, limit, timeout=45, slot="bg"):
        yt = self.yt_dlp()
        if not yt:
            return []
        try:
            r = _yt_run([yt, "-q", "--no-warnings", "--flat-playlist", "--playlist-end", str(limit), "-J", target], slot=slot,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace", timeout=timeout)
            return [e for e in (json.loads(r.stdout or "{}").get("entries") or []) if isinstance(e, dict)] if r.returncode == 0 else []
        except (OSError, ValueError, subprocess.SubprocessError):
            return []

    def _entry_row(self, e):
        url = self._sc_clean_url(e.get("webpage_url") or e.get("url"))
        if not url:
            return None
        try:
            dur = int(float(e.get("duration") or 0) * 1000)
        except (TypeError, ValueError):
            dur = 0
        thumb = e.get("thumbnail") or ""
        if not thumb:
            for t in reversed(e.get("thumbnails") or []):
                if isinstance(t, dict) and self._soundcloud_art_url(t.get("url")):
                    thumb = t["url"]
                    break
        # (a related list only gives title and link: the artist is in the link until the details arrive)
        slug = urllib.parse.urlparse(url).path.strip("/").split("/")[0]
        return {"url": url, "title": str(e.get("title") or "")[:300], "artist": str(e.get("uploader") or "")[:200] or slug.replace("-", " "),
                "duration_ms": max(0, dur), "artwork_url": thumb if self._soundcloud_art_url(thumb) else "",
                "genres": [str(g)[:40] for g in (e.get("genres") or [])][:5], "tags": [str(t)[:40] for t in (e.get("tags") or [])][:12],
                "likes": int(e.get("like_count") or 0)}

    def _sc_related(self, url, slot="bg"):
        """SoundCloud's own "recommended" list for a track (what its station plays next)."""
        url = self._sc_clean_url(url)
        if self.demo:
            return self._demo_rows("related " + url, 12)
        parts = urllib.parse.urlparse(url).path.strip("/").split("/")
        if len(parts) != 2 or parts[1] in ("sets", "albums", "tracks", "likes", "reposts"):
            return []
        return [r for r in (self._entry_row(e) for e in self._yt_entries(url + "/recommended", 25, slot=slot)) if r]

    def discover_more(self, seeds, exclude):
        """More new songs for the Discover feed as it's scrolled, so it never runs out: SoundCloud's "recommended" lists
        for the songs just shown (what their stations play next), else a search from this listener's taste; minus
        everything they've heard and what the feed already has, checked to play, best match first."""
        exclude = {self._sc_clean_url(u) for u in (exclude or [])[:2000]}
        heard = self._heard_urls() | exclude
        cfg = self.cfg()
        history = [x for x in (cfg.get("history") or []) if isinstance(x, dict)]
        dislikes = [x for x in (cfg.get("sc_dislikes") or []) if isinstance(x, dict)]
        p = musicdiscover.profile(self.sc_likes(), history, self.tracks(), dislikes)
        cands = {}

        def add(row, via):
            if row["url"] in heard:
                return
            c = cands.setdefault(row["url"], dict(row, via=[]))
            c["via"].append(via)
        for i, url in enumerate([u for u in (self._sc_clean_url(x) for x in (seeds or [])) if u][:2]):
            for k, row in enumerate(self._sc_related(url, slot="user")[:25]):
                add(row, ("seed", "a song you just heard", (1.0 if i == 0 else 0.7) / (1 + 0.12 * k)))
        if len(cands) < 8:          # nothing related to go on: a search from their taste, a different one each time
            self._more_rotation = getattr(self, "_more_rotation", 0) + 1
            _seeds, queries = musicdiscover.plan(p, 1000 + self._more_rotation)
            for q, kind in queries[:2]:
                for k, row in enumerate(self._sc_search_rich(q, 15, slot="user")):
                    add(row, ("search", q, (0.5 if kind == "artist" else 0.4) / (1 + 0.15 * k)))
        rows = musicdiscover.rank(cands, p, limit=18, per_artist=3) if cands else []
        if rows and not self.demo:
            playable = self._sc_details([r["url"] for r in rows], slot="user")
            if playable:
                rows = [dict(r, **{k: v for k, v in playable[r["url"]].items() if v}) for r in rows if r["url"] in playable]
        keep = ("url", "title", "artist", "duration_ms", "artwork_url", "genres", "tags", "likes", "why")
        return [dict({k: r.get(k) for k in keep}, kind="sc") for r in rows[:15]]

    def _sc_search_rich(self, query, n=15, slot="bg"):
        if self.demo:
            return self._demo_rows("search " + query, 8)
        return [r for r in (self._entry_row(e) for e in self._yt_entries("scsearch%d:%s" % (n, str(query)[:120]), n, slot=slot)) if r]

    # A DJ request is often an activity or a mood ("vibes to code to"), not words to look for: those become a mood
    # (words to add to a search) and the search is made of the listener's own styles instead.
    DJ_MOODS = [
        ("focus", "code coding coder program programming developer dev hacking work working focus focused study studying homework "
                  "concentrate concentration reading deep grind grinding lock locked productive productivity", ["instrumental", "beats"], "beats to focus to"),
        ("workout", "gym workout working-out lift lifting run running training cardio pump sport sports", ["workout", "hard"], "workout music"),
        ("chill", "chill chilling relax relaxing calm mellow late night evening sleep sleeping sleepy cozy lazy sunday", ["chill", "slowed"], "chill vibes"),
        ("party", "party hype dance dancing club pregame rave turn up", ["party", "mix"], "party mix"),
        ("drive", "drive driving road roadtrip car cruise cruising", ["drive", "night drive"], "night drive music"),
        ("sad", "sad rain rainy emotional heartbreak heartbroken cry crying lonely", ["sad", "slowed"], "sad songs"),
        ("happy", "happy upbeat summer sunny good feel-good cheerful", ["upbeat", "summer"], "feel good music"),
    ]
    DJ_FILLER = set(("a an the me my mine i im i'm ive i've want wanna would like could can you please pls give gimme gimmie lemme get "
                     "make create build put together play some something mix mixes playlist playlists set songs song tracks track music tunes "
                     "of with for to and that this kind sort type bit little vibe vibes vibey mood moody new fresh different discover more "
                     "stuff thing things just maybe really hi hello hey thanks thank ok okay yes yeah while during when im about around "
                     "what do does did think how why who are is it its it's was be that's again now too also so than then up on in at").split())

    def dj_plan(self, text, p):
        """[(query, kind)] for one request: explicit styles as asked (with the mood's words), else the listener's own styles
        in that mood, else the mood's own search."""
        words = [w for w in re.findall(r"[\w'-]+", str(text or "").lower()) if len(w) > 1]
        mood = None
        for name, keys, mods, fallback in self.DJ_MOODS:
            if any(w in keys.split() for w in words):
                mood = (name, mods, fallback)
                break
        mood_words = {k for _n, keys, _m, _f in self.DJ_MOODS for k in keys.split()}
        style = [w for w in words if w not in self.DJ_FILLER and w not in mood_words][:4]
        if style:
            q = " ".join(style)
            out = [(q + " " + mood[1][0], "style")] if mood else []
            return out + [((q + " music") if len(style) == 1 and not mood else q, "style")]
        taste = [w for w, _ in sorted(p["tokens"].items(), key=lambda x: -x[1])
                 if w not in musicdiscover.GENERIC and w not in musicdiscover.VARIANTS and p.get("token_songs", {}).get(w, 0) >= 2][:2]
        artists = [p["artist_names"].get(a, a) for a, wt in sorted(p["artists"].items(), key=lambda x: -x[1]) if wt > 0][:1]
        if mood:
            out = [("%s %s" % (w, mood[1][0]), "word") for w in taste] or [(mood[2], "mood")]
            return out[:2] + ([(artists[0], "artist")] if len(out) < 2 and artists else [])
        return [(w, "word") for w in taste] + [(a, "artist") for a in artists] or [("new music", "mood")]

    def dj_search(self, text, query=""):
        """Songs for one DJ request that this listener has never heard, found from what they asked for and what they like:
        searches from dj_plan, plus SoundCloud's "plays next" for their favourite song when the request is a mood; minus
        their history, likes and "not for me" list; ranked by their taste and checked to play."""
        text = re.sub(r"\s+", " ", str(text or query or "")).strip()[:300]
        if len(text) < 2 or not (self.demo or self.yt_dlp()):
            return []
        cfg = self.cfg()
        history = [x for x in (cfg.get("history") or []) if isinstance(x, dict)]
        dislikes = [x for x in (cfg.get("sc_dislikes") or []) if isinstance(x, dict)]
        p = musicdiscover.profile(self.sc_likes(), history, self.tracks(), dislikes)
        plan = self.dj_plan(text, p)
        heard = self._heard_urls()
        cands = {}

        def add(row, via):
            if row["url"] in heard:
                return
            c = cands.setdefault(row["url"], dict(row, via=[]))
            c["via"].append(via)
        for q, kind in plan[:3]:
            base = {"style": 0.9, "word": 0.6, "artist": 0.6, "mood": 0.5}.get(kind, 0.5)
            for k, row in enumerate(self._sc_search_rich(q, 20, slot="user")):
                add(row, ("search", q, base / (1 + 0.1 * k)))
        if all(kind != "style" for _q, kind in plan) and p["seeds"]:
            for k, row in enumerate(self._sc_related(p["seeds"][0]["url"], slot="user")[:20]):
                add(row, ("seed", p["seeds"][0]["title"] or "a song you like", 0.8 / (1 + 0.12 * k)))
        rows = musicdiscover.rank(cands, p, limit=18, per_artist=2) if cands else []
        if rows and not self.demo:
            playable = self._sc_details([r["url"] for r in rows], slot="user")
            if playable:      # (when the look-up itself fails, the unchecked results still go out; the player skips a dud)
                rows = [dict(r, **{k: v for k, v in playable[r["url"]].items() if v}) for r in rows if r["url"] in playable]
        keep = ("url", "title", "artist", "duration_ms", "artwork_url", "genres", "tags", "likes", "why")
        return [{k: r.get(k) for k in keep} for r in rows[:12]]

    def _sc_details(self, urls, slot="bg"):
        """Full details for each track, in a few batches at once. A track yt-dlp can't play (Go+ only, private, gone) gives
        nothing, so it is never suggested."""
        urls = [u for u in urls if self._valid_sc_url(u)]
        if self.demo:
            return {u: {"duration_ms": 150000 + (len(u) % 7) * 20000} for u in urls}
        yt = self.yt_dlp()
        if not yt or not urls:
            return {}
        out = {}

        def batch(chunk):
            try:
                r = _yt_run([yt, "-q", "--no-warnings", "--ignore-errors", "--no-playlist", "--skip-download", "-j"] + chunk, slot=slot,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="replace", timeout=150)
            except (OSError, subprocess.SubprocessError):
                return
            for line in (r.stdout or "").splitlines():
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                fmts = [str(f.get("format_id") or "") for f in (e.get("formats") or []) if isinstance(f, dict)]
                if fmts and all("preview" in f for f in fmts):
                    continue              # only a 30-second preview plays
                row = self._entry_row(e)
                if row:
                    out[row["url"]] = row
        chunks = [urls[i:i + 18] for i in range(0, len(urls), 18)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            list(ex.map(batch, chunks))
        return out

    @staticmethod
    def _demo_rows(seed, n):
        names = ["Neon Rush", "Midnight Drive", "Gabber Heart", "Hardtekk Dreams", "Slow Burn", "Phonk Garage", "Afterglow", "Bassline Ritual",
                 "Static Love", "Night Shift", "Cold Sun", "Velvet Sirens"]
        h = sum(ord(c) for c in seed)
        rows = []
        for i in range(n):
            k = (h + i * 7) % len(names)
            artist = ["dj ember", "kollapse", "nova tekk", "lowlight", "radiant"][(h + i) % 5]
            rows.append({"url": "https://soundcloud.com/%s/%s-%d" % (artist.replace(" ", "-"), names[k].lower().replace(" ", "-"), (h + i) % 97),
                         "title": names[k] + (" (Hardtekk Remix)" if i % 3 == 0 else ""), "artist": artist, "duration_ms": 120000 + i * 9000,
                         "artwork_url": "", "genres": ["Hardtekk"] if i % 2 else ["Electronic"], "tags": ["remix"], "likes": 50 * (i + 1)})
        return rows

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
               "url": str(track["url"])[:1000],
               "artwork_url": track.get("artwork_url") if self._soundcloud_art_url(track.get("artwork_url")) else "",
               "added": int(time.time())}
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

    def local_likes(self):
        """Saved local tracks, including uploads. Ignore entries whose files were removed."""
        rows = self.cfg().get("local_likes")
        if not isinstance(rows, list):
            return []
        out = []
        for row in rows[:500]:
            track = self._norm_track(row)
            if track and track.get("kind") == "local":
                if row.get("added"):
                    track["added"] = row["added"]
                out.append(track)
        return out

    def likes(self):
        """SoundCloud and local tracks together in the Likes tab, most recently liked first.

        Likes carry an "added" timestamp; the sort is stable, so older likes saved before timestamps
        existed keep their previous order (below anything liked since)."""
        combined = self.sc_likes() + self.local_likes()
        combined.sort(key=lambda t: t.get("added") or 0, reverse=True)
        return combined

    def like(self, track, liked):
        """Save or remove a SoundCloud or local track like."""
        if isinstance(track, dict) and track.get("path") and self.resolve(track.get("path")):
            normalized = self._norm_track({**track, "kind": "local"})
            if not normalized:
                return False, "Choose a track from your music library.", self.likes()
            normalized["added"] = int(time.time())
            with self.lock:
                cfg = self.cfg()
                saved = cfg.get("local_likes")
                saved = saved if isinstance(saved, list) else []
                key = normalized["path"]
                current = [row for row in saved if isinstance(row, dict) and row.get("path") != key]
                if liked:
                    current.insert(0, normalized)
                cfg["local_likes"] = current[:500]
                self._save(cfg)
            return True, "", self.likes()
        ok, error, _ = self.sc_like(track, liked)
        return ok, error, self.likes()

    # -- SoundCloud streaming & playlists (so tracks play in the browser) -----

    def yt_dlp(self):
        """yt-dlp, used to resolve a SoundCloud track's audio URL and list a set's tracks. Wherever sc-pick put it."""
        return self._tool("yt-dlp")

    def stream_ready(self):
        return self.demo or bool(self.yt_dlp() and self.ffmpeg())

    def sc_fetch(self, url):
        """Fetch a SoundCloud track and normalize it to 44.1 kHz stereo MP3 before serving. A single codec and sample
        rate avoids format-dependent browser playback glitches. Cache the result so seeking and resume are reliable;
        this never writes to the music folder (that's only the explicit download)."""
        if not self._valid_sc_url(url):
            return None, "not a SoundCloud URL"
        hit = _sc_cache_get(url)
        if hit:
            try:
                os.utime(self._cache_path(url), None)
            except OSError:
                pass
            return hit[0], hit[1]
        with self.cache_lock:
            cached_path = self._cache_path(url)
            try:
                with open(cached_path, "rb") as f:
                    data = f.read()
                if data:
                    os.utime(cached_path, None)
                    _sc_cache_put(url, data, "audio/mpeg")
                    return data, "audio/mpeg"
            except OSError:
                pass
        yt = self.yt_dlp()
        if not yt:
            return None, "yt-dlp isn't installed"
        ffmpeg = self.ffmpeg()
        if not ffmpeg:
            return None, "ffmpeg isn't installed"
        tmpdir = tempfile.mkdtemp(prefix="nysc.")
        try:
            tmpl = os.path.join(tmpdir, "a.%(ext)s")
            try:
                r = _yt_run([yt, "-q", "--no-warnings", "--no-playlist", "-f", "bestaudio/best", "-o", tmpl, url], slot=None,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", timeout=180)
            except subprocess.TimeoutExpired:
                return None, "the download timed out"
            except OSError as e:
                return None, "couldn't run yt-dlp: %s" % e
            files = [f for f in os.listdir(tmpdir) if os.path.isfile(os.path.join(tmpdir, f))]
            if r.returncode or not files:
                err = r.stderr or ""
                if "DRM" in err:
                    return None, "it's DRM-protected (SoundCloud Go+), so it can't be played"
                last = err.strip().splitlines()[-1:] or ["it may be private or geo-blocked"]
                return None, last[0].replace("ERROR:", "").strip()[:200]
            path = os.path.join(tmpdir, files[0])
            try:
                p = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", path, "-map", "0:a:0",
                                    "-vn", "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "192k",
                                    "-f", "mp3", "pipe:1"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=180)
            except (OSError, subprocess.SubprocessError):
                return None, "couldn't normalize the audio"
            if p.returncode or not p.stdout:
                reason = p.stderr.decode("utf-8", "replace").strip().splitlines()
                return None, (reason[-1][:200] if reason else "couldn't normalize the audio")
            data, ctype = p.stdout, "audio/mpeg"
            _sc_cache_put(url, data, ctype)
            with self.cache_lock:
                try:
                    folder = self.cache_dir()
                    os.makedirs(folder, mode=0o700, exist_ok=True)
                    os.chmod(folder, 0o700)
                    target = self._cache_path(url)
                    tmp = target + ".%s.tmp" % secrets.token_hex(6)
                    with open(tmp, "wb") as f:
                        os.chmod(tmp, 0o600)
                        f.write(data)
                    os.replace(tmp, target)
                    self._prune_cache()
                except OSError:
                    try:
                        if "tmp" in locals() and os.path.exists(tmp):
                            os.unlink(tmp)
                    except OSError:
                        pass
            return data, ctype
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def sc_download(self, url):
        """The explicit 'download' action: save a SoundCloud track into the music folder (disk), mirroring sdownload
        (best audio, metadata + artwork embedded, 'Artist - Title [id]' naming, archive-deduped)."""
        if not self._valid_sc_url(url):
            return False, "Not a SoundCloud URL."
        yt = self.yt_dlp()
        if not yt:
            return False, "yt-dlp isn't installed (Settings → Update everything)."
        base = self.download_dir()
        try:
            os.makedirs(base, mode=0o700, exist_ok=True)
        except OSError as e:
            return False, "Couldn't open Nodeyard's writable music downloads folder: %s" % e
        archive = os.path.join(base, ".sc-archive.txt")
        out = os.path.join(base, "%(uploader|Unknown Artist)s - %(title).200B [%(id)s].%(ext)s")
        cmd = [yt, "-q", "--no-warnings", "--no-playlist", "-f", "bestaudio/best", "--no-overwrites",
               "--embed-metadata", "--download-archive", archive, "-o", out]
        if self.ffmpeg():
            cmd += ["--write-thumbnail", "--embed-thumbnail"]
        cmd.append(url)
        try:
            p = _yt_run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", timeout=300)
        except subprocess.TimeoutExpired:
            return False, "The download took too long."
        except OSError as e:
            return False, "Couldn't run yt-dlp: %s" % e
        tail = "\n".join((p.stdout or "").strip().splitlines()[-6:])
        if p.returncode == 0:
            return True, "Saved in Music → Library → Downloads." + (("\n" + tail) if tail else "")
        return False, tail or "Download failed."

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
            r = _yt_run([yt, "-q", "--no-warnings", "--flat-playlist", "-J", url],
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
                         "duration_ms": max(0, duration_ms), "url": u[:1000],
                         "artwork_url": e.get("thumbnail") if self._soundcloud_art_url(e.get("thumbnail")) else ""})
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
        if self._valid_sc_url(t.get("url")):
            try:
                dur = max(0, min(86_400_000, int(t.get("duration_ms") or 0)))
            except (TypeError, ValueError):
                dur = 0
            return {"kind": "sc", "url": str(t["url"])[:1000], "title": str(t.get("title") or "Untitled")[:300],
                    "artist": str(t.get("artist") or "")[:200], "duration_ms": dur,
                    "artwork_url": t.get("artwork_url") if self._soundcloud_art_url(t.get("artwork_url")) else ""}
        if t.get("path") and self.resolve(t.get("path")):
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
                        "recent_cache": music.cache_info(),
                        "forward": {"available": music.forward_available(), "reason": music.forward_reason(),
                                    "enabled": bool(c.get("forward_enabled")),
                                    "source": c.get("forward_source", ""), "auto_source": music.default_source() if have_ff else "",
                                    "sources": music.sources(), "ffmpeg": have_ff},
                        "likes": music.likes(),
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
        if "cache_limit" in body:
            if not music.set_cache_limit(body.get("cache_limit")):
                return h._json({"ok": False, "error": "Choose a cache size from 1 to 200 songs."}, 400)
            c = music.cfg()
        music._save(c)
        return h._json({"ok": True})

    def _browse(h, q):
        return h._json(music.browse(q.get("path", [""])[0]))

    def _sc_search(h, q):
        nodrm = (q.get("nodrm", [""])[0] in ("1", "true", "yes"))
        ok, error, tracks = music.sc_search(q.get("q", [""])[0], nodrm=nodrm)
        return h._json({"ok": ok, "error": error, "tracks": tracks}, 200 if ok else 400)

    def _playback_get(h, q):
        return h._json({"ok": True, "playback": music.playback()})

    def _playback_post(h, body):
        ok, error, state = music.set_playback(body)
        return h._json({"ok": ok, "error": error, "playback": state}, 200 if ok else 400)

    def _history_post(h, body):
        ok, error, count = music.history_event(body)
        return h._json({"ok": ok, "error": error, "count": count if ok else 0}, 200 if ok else 400)

    def _ai_library(h, q):
        action = str(q.get("action", ["overview"])[0] or "overview")[:20]
        try:
            data = music.ai_library(action, q.get("q", [""])[0], q.get("offset", [0])[0], q.get("limit", [50])[0])
        except (OSError, ValueError) as e:
            return h._json({"ok": False, "error": str(e)}, 400)
        return h._json(dict(data, ok="error" not in data), 200 if "error" not in data else 400)

    def _ai_playlist(h, body):
        if body.get("action") != "create_playlist":
            return h._json({"ok": False, "error": "action must be create_playlist."}, 400)
        ok, error, playlist = music.create_ai_playlist(body.get("name"), body.get("tracks"))
        return h._json({"ok": ok, "error": error, "playlist": playlist}, 200 if ok else 400)

    def _discover_get(h, q):
        return h._json(dict(music.discover(refresh=q.get("refresh", [""])[0] == "1"), ok=True))

    def _discover_post(h, body):
        action = str(body.get("action") or "")
        if action not in ("dislike", "undislike"):
            return h._json({"ok": False, "error": "action must be dislike or undislike."}, 400)
        ok, error, data = music.discover_feedback(action, body.get("track"))
        return h._json(dict(data or {}, ok=ok, error=error), 200 if ok else 400)

    def _sc_like(h, body):
        liked = body.get("liked") is True
        ok, error, likes = music.like(body.get("track"), liked)
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

    def _upload(h, body):
        if demo:
            return h._json({"ok": True, "received": 0, "demo": True})
        try:
            return h._json(dict({"ok": True}, **music.upload(body)))
        except MusicError as e:
            return h._json({"ok": False, "error": str(e)}, e.code)

    def _upload_delete(h, body):
        try:
            return h._json(music.delete_upload(body.get("path")))
        except MusicError as e:
            return h._json({"ok": False, "error": str(e)}, e.code)

    def _upload_cancel(h, body):
        try:
            return h._json(music.cancel_upload(body.get("id")))
        except MusicError as e:
            return h._json({"ok": False, "error": str(e)}, e.code)

    def _sc_playlist(h, q):
        ok, msg, tracks = music.sc_playlist(q.get("url", [""])[0])
        return h._json({"ok": ok, "title": msg if ok else "", "error": "" if ok else msg, "tracks": tracks}, 200 if ok else 400)

    def _art(h, q):
        data, ctype = music.artwork(q.get("path", [""])[0], q.get("url", [""])[0])
        # a song's cover doesn't change: the browser keeps it (and keeps "there is none" for an hour) instead of asking
        # again every time a list is drawn
        if not data:
            return h._send(404, "Artwork not found", "text/plain; charset=utf-8", {"Cache-Control": "private, max-age=3600"})
        return h._send(200, data, ctype, {"Cache-Control": "private, max-age=86400"})

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
        if demo or not music.yt_dlp():
            return h._send(503, "SoundCloud playback needs yt-dlp on the server (Settings → Update everything).",
                           "text/plain; charset=utf-8")
        if not music.ffmpeg():
            return h._send(503, "SoundCloud playback needs ffmpeg on the server (Settings → Update everything).",
                           "text/plain; charset=utf-8")
        hit = _sc_cache_get(url)
        if hit:   # already in memory: no download slot needed (seeking asks for many ranges at once)
            return _stream_bytes(h, hit[0], hit[1])
        if not _sc_slots.acquire(blocking=False):
            return h._send(429, "Too many at once. Try again in a moment.", "text/plain; charset=utf-8")
        try:
            data, info = music.sc_fetch(url)      # buffered into RAM, cached, so it's seekable and resumes correctly
            if data is None:
                return h._send(502, "Couldn't load that track (%s)." % info, "text/plain; charset=utf-8")
            _stream_bytes(h, data, info)
        finally:
            _sc_slots.release()

    def _sc_download(h, body):
        if demo:
            return h._json({"ok": True, "text": "(demo) would save the track to the music folder."})
        ok, msg = music.sc_download(str(body.get("url") or ""))
        return h._json({"ok": ok, "text": msg} if ok else {"ok": False, "error": msg}, 200 if ok else 400)

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

    ctx.music = music
    ctx.get_routes.update({"/api/music": _status, "/api/music/track": _track, "/api/audio/live": _live,
                           "/api/music/browse": _browse, "/api/music/search": _sc_search,
                           "/api/music/playback": _playback_get,
                           "/api/music/ai": _ai_library,
                           "/api/music/sc-stream": _sc_stream, "/api/music/sc-playlist": _sc_playlist,
                           "/api/music/playlists": _playlists, "/api/music/art": _art, "/api/music/discover": _discover_get})
    ctx.post_routes.update({"/api/music/config": _config, "/api/audio/test": _test,
                            "/api/audio/setup-server": _setup_audio, "/api/music/sc-download": _sc_download,
                            "/api/music/playback": _playback_post,
                            "/api/music/history": _history_post, "/api/music/ai": _ai_playlist,
                            "/api/music/like": _sc_like, "/api/music/playlist": _playlist, "/api/music/discover": _discover_post,
                            "/api/music/dj-search": lambda h, b: h._json({"ok": True, "tracks": music.dj_search(b.get("text"), b.get("query"))}),
                            "/api/music/discover-more": lambda h, b: h._json({"ok": True, "tracks": music.discover_more(
                                b.get("seeds") if isinstance(b.get("seeds"), list) else [], b.get("exclude") if isinstance(b.get("exclude"), list) else [])})})
    ctx.post_routes["/api/music/upload"] = _upload
    ctx.post_routes["/api/music/upload-delete"] = _upload_delete
    ctx.post_routes["/api/music/upload-cancel"] = _upload_cancel
    ctx.post_limits["/api/music/upload"] = UPLOAD_BODY_MAX


def _stream_bytes(h, data, ctype):
    """Serve an in-memory audio blob with HTTP Range support, so the browser can seek and pause/resume works."""
    size = len(data)
    start, end, status = 0, size - 1, 200
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
    h.send_header("Cache-Control", "private, max-age=86400")   # (a track's audio never changes: replays come from the browser)
    h.send_header("X-Content-Type-Options", "nosniff")
    h.end_headers()
    if h.command == "HEAD":
        return
    try:
        h.wfile.write(memoryview(data)[start:end + 1])   # (no copy of the whole track for each range)
    except (BrokenPipeError, ConnectionResetError):
        pass


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
    # replays and loops come from the browser's cache for an hour (a file replaced under the same name shows after that)
    h.send_header("Cache-Control", "private, max-age=3600")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.end_headers()
    if h.command == "HEAD":
        return
    with open(full, "rb") as f:
        try:
            # the kernel copies file to socket directly (no reading into Python and writing back out)
            h.connection.sendfile(f, start, length)
            return
        except (BrokenPipeError, ConnectionResetError):
            return
        except (OSError, AttributeError, ValueError):
            pass   # no sendfile here: copy it the plain way
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
