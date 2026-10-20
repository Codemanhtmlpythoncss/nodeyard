"""video.py — an optional, self-hosted movie and series player.

You upload films and TV episodes to the dashboard; they are stored in <state_dir>/video-uploads/ and streamed back to
the browser with HTTP Range, so seeking, pause/resume and the browser's own player all work. Nothing leaves the server
but the video you are watching.

The library has two kinds of item: a **movie** (one file) and a **series** (a title with episodes, each tagged with a
season and episode number). Playback position is remembered per item: for a series that means which episode you were on
and how far into it, so "Continue watching" and auto-resume land you exactly where you left off.

State at <state_dir>/video.json keeps the library and the progress. Every streamed path is resolved and checked to be
inside the uploads folder, so a crafted path can't be turned into a way to read the rest of the disk.
"""
import base64
import json
import os
import re
import secrets
import shutil
import tarfile
import threading
import time
import zipfile

VIDEO_EXT = {
    ".mp4": "video/mp4", ".m4v": "video/x-m4v", ".webm": "video/webm", ".ogv": "video/ogg",
    ".mkv": "video/x-matroska", ".mov": "video/quicktime", ".avi": "video/x-msvideo", ".ts": "video/mp2t",
}
UPLOAD_RE = re.compile(r"^[0-9a-f]{6,32}$")
# Archives of episodes: .zip, .tar and the compressed tars (.tar.gz/.tgz, .tar.bz2/.tbz2, .tar.xz/.txz). The exact
# compression is auto-detected from the file's contents, so the stored extension only has to be in this set.
ARCHIVE_EXT = {".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".tbz", ".tbz2", ".txz"}
UPLOAD_CHUNK_MAX = 4 * 1024 * 1024               # one piece of an upload
UPLOAD_FILE_MAX = 16 * 1024 * 1024 * 1024        # one movie or episode (16 GB)
ZIP_FILE_MAX = 48 * 1024 * 1024 * 1024            # a season archive can hold several episodes (48 GB)
UPLOAD_LIB_MAX = 512 * 1024 * 1024 * 1024         # everything uploaded together (512 GB)
UPLOAD_BODY_MAX = UPLOAD_CHUNK_MAX * 4 // 3 + 64 * 1024   # a chunk, as base64 JSON
DISK_HEADROOM = 512 * 1024 * 1024                # keep this much of the disk free
MAX_ITEMS = 2000
MAX_EPISODES = 2000

# Pull a season/episode out of a filename: S01E02, 1x02, "episode 3", or a trailing number.
_SxE = re.compile(r"[sS](\d{1,3})[\s._\-]*[eE](\d{1,4})")
_NxN = re.compile(r"(?:^|[^\d])(\d{1,3})\s*[xX]\s*(\d{1,4})(?:[^\d]|$)")
_EP = re.compile(r"(?:[eE]p(?:isode)?|[Pp]art|[#\-])\s*(\d{1,4})")
_TRAIL = re.compile(r"(\d{1,4})\s*$")


class VideoError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def _new_id():
    return secrets.token_hex(5)


class Video:
    def __init__(self, state_dir, demo=False):
        self.path = os.path.join(state_dir or "/var/lib/nodeyard/dashboard", "video.json")
        self.demo = demo
        self.lock = threading.RLock()

    # -- state ---------------------------------------------------------------
    def upload_dir(self):
        return os.path.join(os.path.dirname(self.path), "video-uploads")

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

    @staticmethod
    def _library(c):
        lib = c.get("library")
        return lib if isinstance(lib, list) else []

    # -- the library the browser sees ----------------------------------------
    def library(self):
        c = self.cfg()
        progress = c.get("progress") if isinstance(c.get("progress"), dict) else {}
        out = []
        for item in self._library(c):
            if not isinstance(item, dict):
                continue
            pr = progress.get(item.get("id")) if isinstance(progress.get(item.get("id")), dict) else {}
            eps_pr = pr.get("eps") if isinstance(pr.get("eps"), dict) else {}
            if item.get("kind") == "series":
                episodes = []
                for ep in item.get("episodes", []):
                    if not isinstance(ep, dict):
                        continue
                    episodes.append({
                        "id": ep.get("id"), "season": ep.get("season", 1), "episode": ep.get("episode", 1),
                        "title": ep.get("title") or "Episode", "path": ep.get("path"), "size": ep.get("size", 0),
                        "progress": eps_pr.get(ep.get("id")) or {},
                    })
                episodes.sort(key=lambda e: (e["season"], e["episode"]))
                out.append({"id": item.get("id"), "kind": "series", "title": item.get("title") or "Series",
                            "added": item.get("added", 0), "episodes": episodes,
                            "last_episode": pr.get("last_episode")})
            else:
                out.append({"id": item.get("id"), "kind": "movie", "title": item.get("title") or "Movie",
                            "added": item.get("added", 0), "path": item.get("path"), "size": item.get("size", 0),
                            "progress": eps_pr.get("movie") or {}})
        out.sort(key=lambda i: i.get("added", 0), reverse=True)
        return out

    # -- resolving a file to stream ------------------------------------------
    def resolve(self, rel):
        """Absolute path for a video, only if it is a real video file inside the uploads folder."""
        rel = str(rel or "")
        base = self.upload_dir()
        if not base or not rel or ".." in rel.split("/"):
            return None
        base = os.path.realpath(base)
        full = os.path.realpath(os.path.join(base, rel.lstrip("/")))
        if full != base and not full.startswith(base + os.sep):
            return None
        if os.path.splitext(full)[1].lower() not in VIDEO_EXT or not os.path.isfile(full):
            return None
        return full

    # -- uploading -----------------------------------------------------------
    @staticmethod
    def _safe_name(name, ext):
        stem = re.sub(r"[^A-Za-z0-9 ._()\-]", "_", os.path.basename(str(name or ""))).strip(" ._") or "video"
        stem = re.sub(r"\s+", " ", stem)[:160]
        if stem.lower().endswith(ext):
            stem = stem[:-len(ext)].strip(" ._") or "video"
        return stem + ext

    def _disk_free(self):
        try:
            return shutil.disk_usage(os.path.dirname(self.path)).free
        except OSError:
            return None

    def upload(self, body):
        """One ordered piece of an upload; the last one ("done") stores it. Chunks are base64 in JSON (the dashboard
        has no multipart parser), assembled into video-uploads/. The file is added to the library by /api/video/add."""
        uid = str(body.get("id", ""))
        if not UPLOAD_RE.match(uid):
            raise VideoError("Bad upload.")
        ext = ("." + re.sub(r"[^a-z0-9]", "", str(body.get("ext", "")).lower().lstrip(".")))[:5]
        is_archive = ext in ARCHIVE_EXT
        if ext not in VIDEO_EXT and not is_archive:
            raise VideoError("That isn't a video (MP4, M4V, WebM, MKV, MOV, OGV, AVI, TS) or a .zip/.tar of episodes.")
        file_max = ZIP_FILE_MAX if is_archive else UPLOAD_FILE_MAX
        try:
            data = base64.b64decode(str(body.get("data", "")), validate=True)
            offset = int(body.get("offset", 0))
        except (ValueError, TypeError):
            raise VideoError("Bad upload data.")
        if len(data) > UPLOAD_CHUNK_MAX:
            raise VideoError("Upload pieces are 4 MB at most.", 413)
        up = self.upload_dir()
        tmp = os.path.join(up, ".part-" + uid)
        with self.lock:
            os.makedirs(up, mode=0o755, exist_ok=True)
            if offset == 0:
                for n in os.listdir(up):   # forget pieces nobody finished
                    f = os.path.join(up, n)
                    if n.startswith(".part-") and time.time() - os.path.getmtime(f) > 24 * 3600:
                        try:
                            os.remove(f)
                        except OSError:
                            pass
            size = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            if offset != size:
                raise VideoError("The upload got out of step. Try again.", 409)
            if size + len(data) > file_max:
                self._discard(tmp)
                raise VideoError("That file is too big (%d GB at most)." % (file_max // 1024 ** 3), 413)
            free = self._disk_free()
            if free is not None and free - len(data) < DISK_HEADROOM:
                self._discard(tmp)
                raise VideoError("The server's disk is nearly full. Free some space first.", 413)
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
                self._discard(tmp)
                raise VideoError("Your uploaded videos are full. Delete some first.", 413)
            final = os.path.join(up, self._safe_name(body.get("name"), ext))
            stem, e = os.path.splitext(final)
            i = 2
            while os.path.exists(final):
                final = "%s (%d)%s" % (stem, i, e)
                i += 1
            os.replace(tmp, final)
            os.chmod(final, 0o644)
        rel = os.path.relpath(final, up)
        return {"ok": True, "path": rel, "name": os.path.splitext(os.path.basename(final))[0],
                "size": os.path.getsize(final)}

    @staticmethod
    def _discard(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass

    def cancel_upload(self, uid):
        uid = str(uid or "")
        if not UPLOAD_RE.match(uid):
            raise VideoError("Bad upload.")
        with self.lock:
            self._discard(os.path.join(self.upload_dir(), ".part-" + uid))
        return {"ok": True}

    # -- the library (add / delete / rename) ---------------------------------
    @staticmethod
    def _clean_title(name, fallback):
        t = re.sub(r"\s+", " ", str(name or "").strip())[:200]
        return t or fallback

    @staticmethod
    def _num(value, default, lo, hi):
        try:
            return max(lo, min(hi, int(value)))
        except (TypeError, ValueError):
            return default

    def add(self, body):
        """Add an uploaded file to the library as a movie, or as an episode of a new or existing series."""
        path = str(body.get("path") or "")
        if not self.resolve(path):
            raise VideoError("That video isn't on the server. Upload it first.")
        size = os.path.getsize(self.resolve(path))
        kind = "series" if body.get("kind") == "series" else "movie"
        with self.lock:
            c = self.cfg()
            lib = self._library(c)
            if len(lib) >= MAX_ITEMS:
                raise VideoError("Your library is full. Delete something first.", 413)
            if self._path_in_library(lib, path):
                raise VideoError("That file is already in your library.")
            if kind == "movie":
                item = {"id": _new_id(), "kind": "movie", "title": self._clean_title(body.get("title"), "Untitled movie"),
                        "path": path, "size": size, "added": int(time.time())}
                lib.append(item)
                added_id = item["id"]
            else:
                series = self._find_series(lib, body)
                if series is None:
                    series = {"id": _new_id(), "kind": "series",
                              "title": self._clean_title(body.get("series_title") or body.get("title"), "Untitled series"),
                              "episodes": [], "added": int(time.time())}
                    lib.append(series)
                if len(series["episodes"]) >= MAX_EPISODES:
                    raise VideoError("That series has too many episodes.", 413)
                ep = {"id": _new_id(), "season": self._num(body.get("season"), 1, 0, 999),
                      "episode": self._num(body.get("episode"), len(series["episodes"]) + 1, 0, 9999),
                      "title": self._clean_title(body.get("episode_title") or body.get("title"), "Episode"),
                      "path": path, "size": size}
                series["episodes"].append(ep)
                added_id = series["id"]
            c["library"] = lib
            self._save(c)
        return {"ok": True, "id": added_id}

    @staticmethod
    def _find_series(lib, body):
        sid = str(body.get("series_id") or "")
        if sid:
            for it in lib:
                if it.get("id") == sid and it.get("kind") == "series":
                    return it
        name = re.sub(r"\s+", " ", str(body.get("series_title") or "").strip()).lower()
        if name:
            for it in lib:
                if it.get("kind") == "series" and str(it.get("title", "")).strip().lower() == name:
                    return it
        return None

    @staticmethod
    def _path_in_library(lib, path):
        for it in lib:
            if it.get("kind") == "movie" and it.get("path") == path:
                return True
            for ep in it.get("episodes", []) if it.get("kind") == "series" else []:
                if isinstance(ep, dict) and ep.get("path") == path:
                    return True
        return False

    def _resolve_archive(self, rel):
        rel = str(rel or "")
        base = os.path.realpath(self.upload_dir())
        if not rel or ".." in rel.split("/"):
            return None
        full = os.path.realpath(os.path.join(base, rel.lstrip("/")))
        if full == base or not full.startswith(base + os.sep):
            return None
        if os.path.splitext(full)[1].lower() not in ARCHIVE_EXT or not os.path.isfile(full):
            return None
        return full

    @staticmethod
    def _open_archive(full):
        """Open a .zip or (optionally compressed) .tar and return (archive, entries) where each entry is
        (name, size, open_callable). Only real files are listed; directories, symlinks and devices are skipped."""
        if zipfile.is_zipfile(full):
            zf = zipfile.ZipFile(full)
            entries = [(i.filename, i.file_size, (lambda i=i: zf.open(i)))
                       for i in zf.infolist() if not i.is_dir()]
            return zf, entries
        tf = tarfile.open(full, "r:*")   # auto-detects gzip/bzip2/xz/plain
        entries = [(m.name, m.size, (lambda m=m: tf.extractfile(m)))
                   for m in tf.getmembers() if m.isfile()]
        return tf, entries

    @staticmethod
    def _parse_se(name, default_ep):
        """Best-effort (season, episode) from a filename; falls back to season 1 and the given episode number."""
        stem = os.path.splitext(os.path.basename(str(name or "")))[0]
        for rx, both in ((_SxE, True), (_NxN, True), (_EP, False), (_TRAIL, False)):
            m = rx.search(stem)
            if m:
                return (int(m.group(1)), int(m.group(2))) if both else (1, int(m.group(1)))
        return 1, default_ep

    def add_series_archive(self, body):
        """Extract the video files from an uploaded .zip/.tar into a new or existing series, one episode per file.

        Only each entry's basename is used for the output path (so a crafted archive can't write outside the uploads
        folder), only video extensions are taken, per-file and total extracted size are capped, and the archive
        itself is deleted afterwards."""
        full = self._resolve_archive(str(body.get("path") or ""))
        if not full:
            raise VideoError("That archive isn't on the server. Upload it first.")
        up = self.upload_dir()
        extracted = []   # (rel, out_full, orig_name, size)
        try:
            free = self._disk_free()
            budget = min(ZIP_FILE_MAX, (free - DISK_HEADROOM) if free is not None else ZIP_FILE_MAX)
            total = 0
            archive, all_entries = self._open_archive(full)
            try:
                entries = [(name, size, opener) for (name, size, opener) in all_entries
                           if os.path.splitext(name)[1].lower() in VIDEO_EXT]
                if not entries:
                    raise VideoError("That archive has no video files in it.")
                if len(entries) > MAX_EPISODES:
                    raise VideoError("That archive has too many files.", 413)
                entries.sort(key=lambda t: t[0].lower())   # stable order when filenames don't number episodes
                for name, size, opener in entries:
                    if size > UPLOAD_FILE_MAX:
                        raise VideoError("An episode in the archive is too big (16 GB at most).", 413)
                    total += size
                    if total > budget:
                        raise VideoError("The archive's episodes won't fit on the server's disk.", 413)
                    ext = os.path.splitext(name)[1].lower()
                    out = os.path.join(up, self._safe_name(os.path.basename(name), ext))
                    stem, e = os.path.splitext(out)
                    i = 2
                    while os.path.exists(out) or any(x[1] == out for x in extracted):
                        out = "%s (%d)%s" % (stem, i, e)
                        i += 1
                    src = opener()
                    if src is None:
                        continue
                    try:
                        with open(out, "wb") as dst:
                            shutil.copyfileobj(src, dst, 1024 * 1024)
                    finally:
                        src.close()
                    os.chmod(out, 0o644)
                    extracted.append((os.path.relpath(out, up), out, name, size))
            finally:
                archive.close()
            with self.lock:
                c = self.cfg()
                lib = self._library(c)
                if len(lib) >= MAX_ITEMS:
                    raise VideoError("Your library is full. Delete something first.", 413)
                series = self._find_series(lib, body)
                if series is None:
                    series = {"id": _new_id(), "kind": "series",
                              "title": self._clean_title(body.get("series_title") or body.get("title"), "Untitled series"),
                              "episodes": [], "added": int(time.time())}
                    lib.append(series)
                base_n = len(series["episodes"])
                for idx, (rel, _out, orig, size) in enumerate(extracted):
                    season, ep = self._parse_se(orig, base_n + idx + 1)
                    series["episodes"].append({
                        "id": _new_id(), "season": season, "episode": ep,
                        "title": self._clean_title(os.path.splitext(os.path.basename(orig))[0], "Episode"),
                        "path": rel, "size": size})
                c["library"] = lib
                self._save(c)
                series_id = series["id"]
        except VideoError:
            for _rel, out, _o, _s in extracted:
                self._discard(out)
            raise
        except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as ex:
            for _rel, out, _o, _s in extracted:
                self._discard(out)
            raise VideoError("Couldn't read that archive: %s" % ex)
        finally:
            self._discard(full)   # the uploaded archive is not kept
        return {"ok": True, "id": series_id, "count": len(extracted)}

    def _delete_file(self, path):
        full = self.resolve(path)
        if full:
            try:
                os.remove(full)
            except OSError:
                pass

    def delete(self, body):
        """Remove a movie, a whole series, or a single episode — and delete the underlying file(s)."""
        item_id = str(body.get("item_id") or "")
        episode_id = str(body.get("episode_id") or "")
        removed_paths = []
        with self.lock:
            c = self.cfg()
            lib = self._library(c)
            item = next((it for it in lib if it.get("id") == item_id), None)
            if item is None:
                raise VideoError("That item isn't in your library.", 404)
            if item.get("kind") == "series" and episode_id:
                eps = item.get("episodes", [])
                ep = next((e for e in eps if isinstance(e, dict) and e.get("id") == episode_id), None)
                if ep is None:
                    raise VideoError("That episode isn't in the series.", 404)
                removed_paths.append(ep.get("path"))
                item["episodes"] = [e for e in eps if e is not ep]
                if not item["episodes"]:
                    lib.remove(item)
            else:
                if item.get("kind") == "series":
                    removed_paths += [e.get("path") for e in item.get("episodes", []) if isinstance(e, dict)]
                else:
                    removed_paths.append(item.get("path"))
                lib.remove(item)
            # drop progress for anything removed
            progress = c.get("progress") if isinstance(c.get("progress"), dict) else {}
            if episode_id and item.get("kind") == "series":
                pr = progress.get(item_id)
                if isinstance(pr, dict):
                    pr.get("eps", {}).pop(episode_id, None)
                    if pr.get("last_episode") == episode_id:
                        pr["last_episode"] = None
            else:
                progress.pop(item_id, None)
            c["library"], c["progress"] = lib, progress
            self._save(c)
        # delete files that no other library entry still points at
        for p in removed_paths:
            if p and not self._path_in_library(self._library(self.cfg()), p):
                self._delete_file(p)
        return {"ok": True}

    def rename(self, body):
        item_id = str(body.get("item_id") or "")
        episode_id = str(body.get("episode_id") or "")
        title = self._clean_title(body.get("title"), "")
        if not title:
            raise VideoError("Enter a title.")
        with self.lock:
            c = self.cfg()
            item = next((it for it in self._library(c) if it.get("id") == item_id), None)
            if item is None:
                raise VideoError("That item isn't in your library.", 404)
            if episode_id and item.get("kind") == "series":
                ep = next((e for e in item.get("episodes", []) if isinstance(e, dict) and e.get("id") == episode_id), None)
                if ep is None:
                    raise VideoError("That episode isn't in the series.", 404)
                ep["title"] = title
            else:
                item["title"] = title
            self._save(c)
        return {"ok": True}

    # -- progress (resume) ---------------------------------------------------
    def set_progress(self, body):
        item_id = str(body.get("item_id") or "")
        episode_id = str(body.get("episode_id") or "")
        try:
            t = max(0.0, min(360_000.0, float(body.get("time") or 0)))
            dur = max(0.0, min(360_000.0, float(body.get("duration") or 0)))
        except (TypeError, ValueError):
            raise VideoError("Bad progress.")
        with self.lock:
            c = self.cfg()
            item = next((it for it in self._library(c) if it.get("id") == item_id), None)
            if item is None:
                raise VideoError("That item isn't in your library.", 404)
            key = "movie"
            if item.get("kind") == "series":
                if not episode_id or not any(isinstance(e, dict) and e.get("id") == episode_id
                                             for e in item.get("episodes", [])):
                    raise VideoError("Unknown episode.")
                key = episode_id
            progress = c.get("progress") if isinstance(c.get("progress"), dict) else {}
            pr = progress.get(item_id) if isinstance(progress.get(item_id), dict) else {}
            eps = pr.get("eps") if isinstance(pr.get("eps"), dict) else {}
            finished = bool(body.get("finished")) or (dur > 0 and t >= dur - 15)
            eps[key] = {"time": 0 if finished else round(t, 1), "duration": round(dur, 1),
                        "finished": finished, "updated": int(time.time())}
            pr["eps"] = eps
            if item.get("kind") == "series":
                pr["last_episode"] = episode_id
            progress[item_id] = pr
            c["progress"] = progress
            self._save(c)
        return {"ok": True}

    def clear_progress(self, body):
        item_id = str(body.get("item_id") or "")
        with self.lock:
            c = self.cfg()
            progress = c.get("progress") if isinstance(c.get("progress"), dict) else {}
            progress.pop(item_id, None)
            c["progress"] = progress
            self._save(c)
        return {"ok": True}


# ---- HTTP: Range-aware file streaming (so the browser's player can seek) ----
def _stream_file(h, full, ctype):
    size = os.path.getsize(full)
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
    h.send_header("Cache-Control", "private, max-age=3600")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.end_headers()
    if h.command == "HEAD":
        return
    with open(full, "rb") as f:
        try:
            h.connection.sendfile(f, start, length)   # kernel copies file→socket directly
            return
        except (BrokenPipeError, ConnectionResetError):
            return
        except (OSError, AttributeError, ValueError):
            pass
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


def register(ctx, args):
    demo = bool(getattr(args, "demo", False))
    video = Video(getattr(args, "state_dir", "") or "/var/lib/nodeyard/dashboard", demo=demo)
    ctx.video = video

    def _status(h, q):
        return h._json({"ok": True, "library": video.library(), "demo": demo})

    def _file(h, q):
        full = video.resolve(q.get("path", [""])[0])
        if not full:
            return h._send(404, "Not found", "text/plain; charset=utf-8")
        _stream_file(h, full, VIDEO_EXT.get(os.path.splitext(full)[1].lower(), "application/octet-stream"))

    def _wrap(fn):
        def handler(h, body):
            if demo:
                return h._json({"ok": True, "demo": True})
            try:
                return h._json(fn(body))
            except VideoError as e:
                return h._json({"ok": False, "error": str(e)}, e.code)
        return handler

    def _upload(h, body):
        if demo:
            return h._json({"ok": True, "received": 0, "demo": True})
        try:
            return h._json(dict({"ok": True}, **video.upload(body)))
        except VideoError as e:
            return h._json({"ok": False, "error": str(e)}, e.code)

    ctx.get_routes.update({"/api/video": _status, "/api/video/file": _file})
    ctx.post_routes.update({
        "/api/video/upload": _upload,
        "/api/video/upload-cancel": _wrap(lambda b: video.cancel_upload(b.get("id"))),
        "/api/video/add": _wrap(video.add),
        "/api/video/add-archive": _wrap(video.add_series_archive),
        "/api/video/delete": _wrap(video.delete),
        "/api/video/rename": _wrap(video.rename),
        "/api/video/progress": _wrap(video.set_progress),
        "/api/video/clear-progress": _wrap(video.clear_progress),
    })
    ctx.post_limits["/api/video/upload"] = UPLOAD_BODY_MAX
