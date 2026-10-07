"""Conversations on disk: every message is appended to a JSONL file as it happens, so nothing is lost and any
conversation can be resumed. File changes are checkpointed so /rewind and /undo can put files back.

Record types in the file: meta (first line), message, reset (the whole message list, after a compaction or rewind),
title, usage.
"""
import hashlib
import json
import os
import secrets
import time

from . import util


def project_dir(cwd):
    h = hashlib.sha1(os.path.abspath(cwd).encode()).hexdigest()[:8]
    return os.path.join(util.data_dir(), "sessions", "%s-%s" % (util.slug(os.path.basename(os.path.abspath(cwd)), 24), h))


class Checkpoints:
    """Old versions of files, saved the first time a turn changes them."""

    def __init__(self, root):
        self.root = root
        self.index = {}      # turn -> {path: stored file name or None (the file didn't exist)}
        self._load()

    def _load(self):
        self.index = {int(k): v for k, v in util.read_json(os.path.join(self.root, "index.json")).items()}

    def _save(self):
        util.ensure_dir(self.root)
        util.write_json(os.path.join(self.root, "index.json"), {str(k): v for k, v in self.index.items()}, 0o600)

    def save(self, turn, path):
        entry = self.index.setdefault(turn, {})
        if path in entry:
            return
        if os.path.isfile(path):
            name = "%d-%s" % (turn, hashlib.sha1(path.encode()).hexdigest()[:10])
            util.ensure_dir(self.root)
            with open(path, "rb") as src, open(os.path.join(self.root, name), "wb") as dst:
                dst.write(src.read())
            entry[path] = name
        else:
            entry[path] = None
        self._save()

    def restore_from(self, turn):
        """Put every file changed in turns >= TURN back as it was before TURN. Returns the paths restored."""
        done = []
        for t in sorted((t for t in self.index if t >= turn), reverse=True):
            for path, name in self.index[t].items():
                try:
                    if name is None:
                        if os.path.isfile(path):
                            os.unlink(path)
                    else:
                        with open(os.path.join(self.root, name), "rb") as f:
                            data = f.read()
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        with open(path, "wb") as out:
                            out.write(data)
                    done.append(path)
                except OSError:
                    pass
        for t in [t for t in self.index if t >= turn]:
            del self.index[t]
        self._save()
        return list(dict.fromkeys(done))

    def files_since(self, turn):
        out = []
        for t in sorted(self.index):
            if t >= turn:
                out += list(self.index[t])
        return list(dict.fromkeys(out))


class Session:
    def __init__(self, cwd, session_id=None, persist=True):
        self.cwd = os.path.abspath(cwd)
        self.persist = persist
        self.dir = project_dir(self.cwd)
        self.id = session_id or time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        self.path = os.path.join(self.dir, self.id + ".jsonl")
        self.messages = []
        self.title = ""
        self.created = time.time()
        self.usage = {"prompt": 0, "completion": 0, "requests": 0, "seconds": 0.0, "gen_seconds": 0.0, "gen_tokens": 0}
        self.turn = 0
        self.checkpoints = Checkpoints(os.path.join(self.dir, self.id + ".ckpt"))
        self.model = ""
        self.remote_id = ""          # the chat's id on the nodeyard server, once shared (see sync.py)
        self.synced = 0              # how many messages the server already has
        self.synced_reset = 0
        self.reset_count = 0         # bumped when history is rewritten (compaction, rewind): the server needs all of it again
        if persist and os.path.exists(self.path):
            self._read()
        elif persist:
            self._write({"type": "meta", "id": self.id, "cwd": self.cwd, "created": self.created})

    # ---- files ----
    def _write(self, rec):
        if not self.persist:
            return
        try:
            util.ensure_dir(self.dir, 0o700)
            new = not os.path.exists(self.path)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if new:
                os.chmod(self.path, 0o600)
        except OSError:
            self.persist = False   # a read-only home directory must not stop the work

    def _read(self):
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                t = r.get("type")
                if t == "meta":
                    self.created = r.get("created", self.created)
                elif t == "message":
                    self.messages.append(r["message"])
                elif t == "reset":
                    self.messages = r["messages"]
                elif t == "title":
                    self.title = r.get("title", "")
                elif t == "usage":
                    self.usage.update(r.get("usage", {}))
                elif t == "model":
                    self.model = r.get("model", "")
                elif t == "remote":
                    self.remote_id, self.synced = r.get("id", ""), int(r.get("synced", 0))
        self.turn = sum(1 for m in self.messages if m.get("role") == "user" and not m.get("_synthetic"))

    # ---- messages ----
    def add(self, message):
        message = dict(message)
        message.setdefault("_ts", int(time.time()))
        if message["role"] == "user" and not message.get("_synthetic"):
            self.turn += 1
            message["_turn"] = self.turn
            if not self.title:
                self.title = (message.get("content") or "").strip().split("\n")[0][:70]
                self._write({"type": "title", "title": self.title})
        self.messages.append(message)
        self._write({"type": "message", "message": message})
        return message

    def reset(self, messages):
        """Replace the whole conversation (after a compaction or a rewind)."""
        self.messages = list(messages)
        self.reset_count += 1
        self.turn = sum(1 for m in self.messages if m.get("role") == "user" and not m.get("_synthetic"))
        self._write({"type": "reset", "messages": self.messages})

    def set_synced(self, count, reset_count, remote_id):
        self.synced, self.synced_reset, self.remote_id = count, reset_count, remote_id
        self._write({"type": "remote", "id": remote_id, "synced": count})

    def add_usage(self, comp):
        u = self.usage
        u["prompt"] += comp.prompt_tokens
        u["completion"] += comp.completion_tokens
        u["requests"] += 1
        u["seconds"] += comp.seconds
        if comp.completion_tokens and comp.seconds:
            u["gen_seconds"] += max(0.0, comp.seconds - (comp.first_token or 0))
            u["gen_tokens"] += comp.completion_tokens
        self._write({"type": "usage", "usage": u})

    def set_model(self, model):
        if model and model != self.model:
            self.model = model
            self._write({"type": "model", "model": model})

    def api_messages(self):
        """The messages as the model should see them: no bookkeeping keys."""
        return [{k: v for k, v in m.items() if not k.startswith("_")} for m in self.messages]

    # ---- turns, checkpoints, rewind ----
    def user_turns(self):
        """[(message index, text)] for every real user message."""
        return [(i, m.get("content") or "") for i, m in enumerate(self.messages) if m.get("role") == "user" and not m.get("_synthetic")]

    def checkpoint_file(self, path):
        self.checkpoints.save(self.turn, os.path.abspath(path))

    def rewind(self, user_index, files=True, conversation=True):
        """Go back to before the Nth user message (1-based). Returns (files restored, messages dropped)."""
        turns = self.user_turns()
        if not 1 <= user_index <= len(turns):
            raise ValueError("No such message.")
        cut = turns[user_index - 1][0]
        restored = self.checkpoints.restore_from(user_index) if files else []
        dropped = 0
        if conversation:
            dropped = len(self.messages) - cut
            self.reset(self.messages[:cut])
        return restored, dropped

    # ---- listing ----
    @staticmethod
    def list(cwd=None, limit=30):
        root = project_dir(cwd) if cwd else os.path.join(util.data_dir(), "sessions")
        out = []
        dirs = [root] if cwd else [os.path.join(root, d) for d in (os.listdir(root) if os.path.isdir(root) else [])]
        for d in dirs:
            if not os.path.isdir(d):
                continue
            for f in os.listdir(d):
                if not f.endswith(".jsonl"):
                    continue
                p = os.path.join(d, f)
                title, count, created, scwd = "", 0, 0, ""
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            try:
                                r = json.loads(line)
                            except ValueError:
                                continue
                            if r.get("type") == "meta":
                                created, scwd = r.get("created", 0), r.get("cwd", "")
                            elif r.get("type") == "title":
                                title = r.get("title", "")
                            elif r.get("type") == "message" and r["message"].get("role") == "user":
                                count += 1
                except OSError:
                    continue
                out.append({"id": f[:-6], "title": title or "(no title)", "turns": count, "created": created, "updated": os.path.getmtime(p), "cwd": scwd, "path": p})
        out.sort(key=lambda s: -s["updated"])
        return out[:limit]

    @staticmethod
    def find(prefix, cwd=None):
        for s in Session.list(cwd, limit=500):
            if s["id"].startswith(prefix):
                return s
        return None
