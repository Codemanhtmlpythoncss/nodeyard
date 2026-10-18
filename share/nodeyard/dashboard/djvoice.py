"""The DJ's natural voice: Piper, a small neural text-to-speech program, running on this server, so the DJ sounds like a
person instead of the browser's built-in voice.

Downloaded only when asked for, into /var/lib/nodeyard/piper: the program (~26 MB, Piper's GitHub release for this
CPU) and each voice (~63 MB, rhasspy/piper-voices on Hugging Face). Each line is spoken by running Piper once (about a
second on a laptop CPU), one at a time, at low priority, and it's the first thing stopped if memory runs out.

  GET  /api/music/dj-voice          {installed, voices: [{id, name, ready}], busy, error}
  POST /api/music/dj-voice/setup    {voice}             download Piper and that voice in the background
  POST /api/music/dj-say            {text, voice}       -> audio/wav
"""
import collections
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tarfile
import threading
import urllib.request

PIPER_RELEASE = "2023.11.14-2"
PIPER_URL = "https://github.com/rhasspy/piper/releases/download/%s/piper_linux_%s.tar.gz"
VOICE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/%s/%s.onnx%s"
ARCHES = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64", "armv7l": "armv7l", "armv8l": "armv7l"}
# (id, name, folder in rhasspy/piper-voices): medium quality, ~63 MB each
VOICES = [
    ("en_US-lessac-medium", "Lessac · US, warm and clear", "en/en_US/lessac/medium"),
    ("en_US-john-medium", "John · US", "en/en_US/john/medium"),
    ("en_US-bryce-medium", "Bryce · US", "en/en_US/bryce/medium"),
    ("en_US-hfc_male-medium", "Male · US", "en/en_US/hfc_male/medium"),
    ("en_US-kristin-medium", "Kristin · US", "en/en_US/kristin/medium"),
    ("en_US-amy-medium", "Amy · US", "en/en_US/amy/medium"),
    ("en_GB-alan-medium", "Alan · UK", "en/en_GB/alan/medium"),
    ("en_GB-northern_english_male-medium", "Northern English male · UK", "en/en_GB/northern_english_male/medium"),
    ("en_GB-cori-medium", "Cori · UK", "en/en_GB/cori/medium"),
    ("en_GB-jenny_dioco-medium", "Jenny · UK", "en/en_GB/jenny_dioco/medium"),
]
BY_ID = {v[0]: v for v in VOICES}
MAX_TEXT = 600
# Piper's delivery: a touch quicker than its default and short gaps between sentences, like someone talking on air
SPEECH_ARGS = ["--length_scale", "0.92", "--sentence_silence", "0.18", "--noise_scale", "0.62", "--noise_w", "0.8"]
WRAP = ["sh", "-c", '{ echo 900 > /proc/self/oom_score_adj; } 2>/dev/null; command -v nice >/dev/null 2>&1 && exec nice -n 5 "$@"; exec "$@"', "piper"]


class VoiceError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def wav(pcm, rate):
    """16-bit mono PCM -> a WAV file."""
    return b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + \
        b"data" + struct.pack("<I", len(pcm)) + pcm


class DJVoice:
    def __init__(self, ctx, args):
        self.demo = bool(getattr(args, "demo", False))
        state = getattr(args, "state_dir", "") or "/var/lib/nodeyard/dashboard"
        self.root = os.path.join(os.path.dirname(state.rstrip("/")) or "/var/lib/nodeyard", "piper")
        self.lock = threading.Lock()
        self.speak_lock = threading.Lock()
        self.busy, self.error = "", ""
        self.cache = collections.OrderedDict()      # (voice, text) -> wav bytes: replays and the same line twice

    def _bin(self):
        p = os.path.join(self.root, "piper", "piper")
        return p if os.path.isfile(p) and os.access(p, os.X_OK) else ""

    def _voice_files(self, vid):
        return os.path.join(self.root, "voices", vid + ".onnx"), os.path.join(self.root, "voices", vid + ".onnx.json")

    def _ready(self, vid):
        onnx, conf = self._voice_files(vid)
        return os.path.isfile(onnx) and os.path.isfile(conf)

    def view(self):
        installed = self.demo or bool(self._bin())
        return {"installed": installed, "busy": self.busy, "error": self.error, "demo": self.demo,
                "voices": [{"id": v[0], "name": v[1], "ready": self.demo or (installed and self._ready(v[0]))} for v in VOICES]}

    # -- getting it ------------------------------------------------------------------------------------------------
    def setup(self, vid):
        if vid not in BY_ID:
            raise VoiceError("Pick one of the listed voices.")
        if self.demo:
            return self.view()
        arch = ARCHES.get(os.uname().machine)
        if not arch:
            raise VoiceError("Piper has no build for this server's CPU (%s)." % os.uname().machine)
        with self.lock:
            if self.busy:
                return self.view()
            self.busy, self.error = "starting", ""
        threading.Thread(target=self._download, args=(vid, arch), daemon=True, name="dj-voice-setup").start()
        return self.view()

    def _fetch(self, url, dest, what):
        self.busy = "downloading %s" % what
        tmp = dest + ".part"
        req = urllib.request.Request(url, headers={"User-Agent": "nodeyard-dashboard"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        os.replace(tmp, dest)

    def _download(self, vid, arch):
        try:
            os.makedirs(os.path.join(self.root, "voices"), exist_ok=True)
            if not self._bin():
                tgz = os.path.join(self.root, "piper.tar.gz")
                self._fetch(PIPER_URL % (PIPER_RELEASE, arch), tgz, "the Piper program")
                self.busy = "unpacking Piper"
                with tarfile.open(tgz) as t:
                    for m in t.getmembers():      # only plain files and folders, inside our own folder
                        name = os.path.normpath(m.name)
                        if name.startswith(("/", "..")) or not (m.isfile() or m.isdir() or m.issym()) or \
                                (m.issym() and (os.path.isabs(m.linkname) or os.path.normpath(os.path.join(os.path.dirname(name), m.linkname)).startswith(".."))):
                            raise VoiceError("The Piper download has an unexpected file in it (%s)." % m.name)
                    try:
                        t.extractall(self.root, filter="data")
                    except TypeError:       # (Python before 3.11.4: the members were checked above)
                        t.extractall(self.root)
                os.remove(tgz)
                if not self._bin():
                    raise VoiceError("Piper didn't unpack as expected.")
            if not self._ready(vid):
                folder = BY_ID[vid][2]
                onnx, conf = self._voice_files(vid)
                self._fetch(VOICE_URL % (folder, vid, ".json"), conf, "the voice's settings")
                self._fetch(VOICE_URL % (folder, vid, ""), onnx, "the %s voice" % BY_ID[vid][1].split(" ·")[0])
            self.busy = ""
        except Exception as e:  # noqa: BLE001 -- say what went wrong on the page
            self.error, self.busy = "Couldn't set up the natural voice: %s" % str(e)[:200], ""

    # -- speaking --------------------------------------------------------------------------------------------------
    def say(self, text, vid):
        text = re.sub(r"\s+", " ", str(text or "")).strip()[:MAX_TEXT]
        if not text:
            raise VoiceError("Nothing to say.")
        if vid not in BY_ID:
            raise VoiceError("Pick one of the listed voices.")
        if self.demo:
            raise VoiceError("The demo has no natural voice; the browser's voice is used.", 409)
        piper = self._bin()
        if not piper or not self._ready(vid):
            raise VoiceError("That voice isn't set up yet.", 409)
        key = hashlib.sha1((vid + "\n" + text).encode()).hexdigest()
        with self.lock:
            hit = self.cache.get(key)
            if hit:
                self.cache.move_to_end(key)
                return hit
        onnx, conf = self._voice_files(vid)
        try:
            with open(conf, "r", encoding="utf-8") as f:
                rate = int(((json.load(f).get("audio") or {}).get("sample_rate")) or 22050)
        except (OSError, ValueError):
            rate = 22050
        folder = os.path.dirname(piper)
        with self.speak_lock:         # one line at a time: each run loads the voice (~100 MB) for a second or two
            try:
                p = subprocess.run(WRAP + [piper, "--model", onnx, "--output_raw", "--quiet"] + SPEECH_ARGS, input=text.encode("utf-8"),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, cwd=folder,
                                   env={"LD_LIBRARY_PATH": folder, "PATH": "/usr/bin:/bin"})
            except subprocess.TimeoutExpired:
                raise VoiceError("Piper took too long.", 504)
            except OSError as e:
                raise VoiceError("Couldn't run Piper: %s" % e, 500)
        if p.returncode or not p.stdout:
            raise VoiceError("Piper couldn't say that: %s" % (p.stderr.decode("utf-8", "replace").strip().splitlines() or ["no output"])[-1][:200], 500)
        out = wav(p.stdout, rate)
        with self.lock:
            self.cache[key] = out
            while len(self.cache) > 12:
                self.cache.popitem(last=False)
        return out


def register(ctx, args):
    v = DJVoice(ctx, args)
    ctx.djvoice = v

    def wrap(fn):
        def route(h, arg):
            try:
                r = fn(h, arg)
            except VoiceError as e:
                return h._json({"ok": False, "error": str(e)}, e.code)
            return h._json(dict(r or {}, ok=True))
        return route

    def say(h, body):
        try:
            data = v.say(body.get("text"), str(body.get("voice") or ""))
        except VoiceError as e:
            return h._json({"ok": False, "error": str(e)}, e.code)
        h._send(200, data, "audio/wav", {"Cache-Control": "no-store"})

    ctx.get_routes["/api/music/dj-voice"] = wrap(lambda h, q: v.view())
    ctx.post_routes.update({"/api/music/dj-voice/setup": wrap(lambda h, b: v.setup(str(b.get("voice") or ""))),
                            "/api/music/dj-say": say})
