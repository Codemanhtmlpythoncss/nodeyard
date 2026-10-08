"""A pretend AI back end for the demo (and the tests): canned model search, a streaming chat, tasks that
print a few lines, and Ollama models that can be loaded and unloaded. Nothing leaves this computer."""
import json
import re
import secrets
import threading
import time

import aiapi

GiB = 1024 ** 3

MODELS = [
    ("mradermacher/Huihui-Qwen3-Coder-30B-A3B-Instruct-abliterated-i1-GGUF", 61000, 190, 30.5, "Code, 30B (3B active)"),
    ("Qwen/Qwen2.5-Coder-7B-Instruct-GGUF", 480000, 410, 7.6, "Code, 7B"),
    ("bartowski/Qwen2.5-Coder-14B-Instruct-GGUF", 150000, 120, 14.8, "Code, 14B"),
    ("bartowski/Llama-3.2-3B-Instruct-GGUF", 920000, 380, 3.2, "Chat, 3B"),
    ("unsloth/gemma-3-4b-it-GGUF", 410000, 260, 4.3, "Chat, 4B"),
    ("TheBloke/Mistral-7B-Instruct-v0.2-GGUF", 780000, 1700, 7.2, "Chat, 7B"),
    ("bartowski/Phi-3.5-mini-instruct-GGUF", 300000, 170, 3.8, "Chat, 3.8B"),
]
QUANTS = [("Q2_K", 0.36), ("Q3_K_M", 0.46), ("Q4_K_M", 0.58), ("Q5_K_M", 0.68), ("Q6_K", 0.79), ("Q8_0", 1.02)]


class DemoHF:
    def search(self, q, sort, limit):
        q = (q or "").lower().strip()
        rows = [m for m in MODELS if not q or all(w in m[0].lower() for w in q.split())]
        key = {"downloads": 1, "likes": 2}.get(sort, 1)
        rows.sort(key=lambda m: -m[key])
        return [{"id": m[0], "downloads": m[1], "likes": m[2], "updated": "2026-09-14T10:00:00.000Z", "pipeline": "text-generation", "tags": ["text-generation", "demo data", m[4]]}
                for m in rows[:limit]]

    def files(self, repo):
        m = next((x for x in MODELS if x[0] == repo), None)
        if not m:
            raise aiapi.AIError("Hugging Face doesn't have that.", 404)
        base = re.sub(r"-GGUF$", "", repo.split("/")[1])
        out = []
        for quant, bpw in QUANTS:
            size = int(m[3] * 1e9 * bpw)
            out.append({"file": "%s.%s.gguf" % (base, quant), "size": size, "parts": 1, "quant": quant, "split_ok": True, "ollama": "hf.co/%s:%s" % (repo, quant)})
        return out


ANSWERS = {
    "math": "Let's work it out.\n\n**Step 1: read the binary number**\n\n`1010100101₂ = 512 + 128 + 32 + 4 + 1 = 677`\n\n**Step 2: shift left by 2** (multiply by 4)\n\n`677 << 2 = 677 × 4 = 2708`\n\nSo `1010100101 << 2` is **2708** in decimal.",
    "code": "Here's a small example:\n\n```python\ndef fib(n: int) -> int:\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n    return a\n\nprint([fib(i) for i in range(10)])\n```\n\nIt runs in linear time and constant memory. Want a recursive or memoised version too?",
    "hello": "Hello! I'm the demo model: nothing here is a real AI. Connect your own cluster and I'll be replaced by whatever you run.\n\nAsk me about:\n- **binary maths** (try `1010100101 << 2`)\n- **a Python function**\n- anything else, and I'll answer generically.",
    "file": "I made it as files you can download:\n\n<file path=\"log_summary/summarise.py\">\n#!/usr/bin/env python3\n\"\"\"Counts the lines of a log file by level (INFO, WARNING, ERROR).\"\"\"\nimport collections\nimport sys\n\n\ndef main(path):\n    counts = collections.Counter()\n    with open(path, encoding=\"utf-8\", errors=\"replace\") as f:\n        for line in f:\n            for level in (\"ERROR\", \"WARNING\", \"INFO\"):\n                if level in line:\n                    counts[level] += 1\n                    break\n    for level, n in counts.most_common():\n        print(f\"{level:8} {n}\")\n\n\nif __name__ == \"__main__\":\n    main(sys.argv[1])\n</file>\n\n<file path=\"log_summary/README.md\">\n# log_summary\n\nRun it on any log file:\n\n    python3 summarise.py /var/log/syslog\n</file>\n\n<folder path=\"log_summary/samples\"/>\n\n- **summarise.py** does the counting.\n- **README.md** says how to run it.\n\nUse **Download all** to get the folder as a .zip.",
    "other": "That's a good question. This is the demo, so I can only give canned answers, but with a real model loaded on your cluster this reply would come from it, streamed token by token, with the speed shown underneath.",
}


def pick_answer(text):
    t = text.lower()
    if "binary" in t or "<<" in t or ">>" in t:
        return ANSWERS["math"]
    if any(w in t for w in ("file", "script", "folder", "make", "create")):
        return ANSWERS["file"]
    if any(w in t for w in ("python", "code", "function", "write")):
        return ANSWERS["code"]
    if any(w in t for w in ("hello", "hi ", "hey")) or len(t) < 12:
        return ANSWERS["hello"]
    return ANSWERS["other"]


class FakeStream:
    """Looks enough like an HTTP response for the chat relay: readline() gives server-sent-event lines."""

    def __init__(self, text, delay=0.025):
        self.chunks = re.findall(r"\S+\s*|\s+", text)
        self.delay = delay
        self.n = 0
        self.pending = []
        self.done = False
        self.stopped = False

    def readline(self):
        if self.stopped:
            return b""
        if self.pending:
            return self.pending.pop(0)
        if self.n < len(self.chunks):
            time.sleep(self.delay)
            piece = self.chunks[self.n]
            self.n += 1
            self.pending = [b"\n"]
            return ("data: %s\n" % json.dumps({"choices": [{"index": 0, "delta": {"content": piece}}]})).encode()
        if not self.done:
            self.done = True
            final = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "timings": {"predicted_n": self.n, "predicted_per_second": 11.4, "prompt_per_second": 38.2},
                     "usage": {"completion_tokens": self.n}}
            self.pending = [b"\n", b"data: [DONE]\n", b"\n"]
            return ("data: %s\n" % json.dumps(final)).encode()
        return b""


class FakeSock:
    def __init__(self, stream):
        self.stream = stream

    def shutdown(self, how):
        self.stream.stopped = True


class FakeConn:
    def __init__(self, stream=None):
        self.sock = FakeSock(stream) if stream else None

    def close(self):
        pass


class DemoAI:
    def __init__(self, store):
        self.store = store
        self.hf = DemoHF()
        self.jobs = {}
        self.order = []
        self.lock = threading.Lock()
        self.loaded = {("ollama-demo-yard-1", "llama3.2:3b")}
        self.models = {"llama3.2:3b": 2 * GiB, "qwen2.5-coder:7b": int(4.7 * GiB), "phi4-mini:3.8b": int(2.5 * GiB)}

    # ---- chat ----------------------------------------------------------------
    def targets(self):
        st = self.store.snapshot()["state"] or {}
        sp = (st.get("ai") or {}).get("split") or {}
        out = [{"id": "split", "kind": "split", "name": sp.get("alias", "qwen3-coder-30b"), "model": sp.get("model", ""), "detail": "split across 3 machines", "ready": True}]
        for e in self.ollama_overview():
            for m in e["models"]:
                out.append({"id": "ollama:%s:%s" % (e["pod"], m["name"]), "kind": "ollama", "name": m["name"], "model": m["name"], "detail": "Ollama on %s" % e["node"], "ready": True, "size": m["size"]})
        busy = next((j for j in self.jobs.values() if j["status"] == "running" and j.get("exclusive", True)), None)
        return {"targets": out, "can_run": True, "busy": busy["id"] if busy else None, "has_key": True,
                "recent": [{"id": j, "title": self.jobs[j]["title"], "status": self.jobs[j]["status"], "started": self.jobs[j]["started"]} for j in reversed(self.order)]}

    def open_chat(self, target, payload, on_conn=None):
        if target != "split" and not target.startswith("ollama:"):
            raise aiapi.AIError("Unknown model.", 404)
        last = next((m["content"] for m in reversed(payload["messages"]) if m["role"] == "user"), "")
        # (an attached file is sent as a <file> block after the question: answer the question)
        last = last.split("<file ", 1)[0] or last
        stream = FakeStream(pick_answer(last))
        conn = FakeConn(stream)
        if on_conn:
            on_conn(conn)
        return conn, stream

    # ---- Ollama ---------------------------------------------------------------
    def ollama_overview(self):
        out = []
        for i, (node, pod) in enumerate((("yard-1", "ollama-demo-yard-1"), ("yard-2", "ollama-demo-yard-2"), ("yard-3", "ollama-demo-yard-3"))):
            models = []
            for name, size in self.models.items():
                if i == 2 and name == "qwen2.5-coder:7b":
                    continue
                on = (pod, name) in self.loaded
                models.append({"name": name, "size": size, "loaded": on, "memory": int(size * 1.1) if on else 0, "expires": "", "params": name.split(":")[1].upper() if ":" in name else "", "quant": "Q4_K_M"})
            out.append({"pod": pod, "node": node, "models": models, "error": ""})
        return out

    def ollama_load(self, pod, model, on):
        if (pod, model) not in {(e["pod"], m["name"]) for e in self.ollama_overview() for m in e["models"]}:
            raise aiapi.AIError("No such model on that Ollama.", 404)
        (self.loaded.add if on else self.loaded.discard)((pod, model))

    def reveal_key(self):
        return "demo-api-key-0000-0000-0000-0000"

    # ---- tasks ------------------------------------------------------------------
    def run(self, action, p):
        title, argv = aiapi.build_command(action, p, {n["name"] for n in (self.store.snapshot()["state"] or {"nodes": []})["nodes"]}, "/etc/nodeyard/secrets/ai-split-api-key")
        exclusive = action not in aiapi.SHARED_ACTIONS
        with self.lock:
            if exclusive and any(j["status"] == "running" and j.get("exclusive", True) for j in self.jobs.values()):
                raise aiapi.AIError("Another change to the model is still running. Wait for it to finish.", 409)
            jid = secrets.token_hex(6)
            job = {"id": jid, "title": title, "status": "running", "rc": None, "lines": [], "started": time.time(), "cmd": "nodeyard " + " ".join(argv),
                   "exclusive": exclusive}
            self.jobs[jid] = job
            self.order.append(jid)
        threading.Thread(target=self._play, args=(job, argv, action), daemon=True).start()
        return jid

    def _play(self, job, argv, action):
        script = {
            "plan": ["Planning the split of the model across your nodes...", "debian-1   10.7 GiB (61%)", "archlinux  4.8 GiB (27%)", "worker     1.8 GiB (10%)", "It fits: 17.3 GiB of 20.1 GiB free."],
            "deploy": ["> Replacing the running model", "Disk on debian-1: 849 GiB free", "created job/model-download", "created deployment/rpc-debian-1", "created deployment/llama-main", "OK Deployed. The model downloads, then loads."],
            "split-unload": ["> Unloading the split model", "deployment.apps/llama-main scaled", "OK Memory is free again."],
            "split-load": ["> Loading the split model", "deployment.apps/llama-main scaled", "OK Loading; this takes a minute or two."],
            "undeploy": ["> Removing the split model", "Unloading the model first...", "OK Removed."],
            "switch": ["> Switching model", "Running now: Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf. It is unloaded first, then its file and weight caches are deleted from every node.",
                       "Unloading Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf...", "OK Unloaded: every node has its memory back.", "Deleting Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf and its weight caches...",
                       "  yard-1           freed 17.3 GiB", "  yard-2           freed 5.4 GiB", "OK Deployed. The main node (yard-1) downloads the model, then loads it."],
            "download": ["Downloading in the background.", "OK Progress shows under Downloaded models."],
            "split-rm": ["  debian-1         freed 7.0 GiB", "OK Deleted."],
            "clean": ["  NODE             KIND           SIZE  NAME", "  debian-1         cache         13.6G  old-model", "  archlinux-2      cache          8.8G  old-model",
                      "  debian-1         freed 13.6 GiB, now 862.4 GiB free", "  archlinux-2      freed 8.8 GiB, now 39.1 GiB free", "OK Freed about 22.4 GiB."],
            "pull": ["-- yard-1 --", "pulling manifest", "pulling 8eeb52dfb3bb... 100%", "success", "OK Done."],
            "agent-install": ["> Setting up the node agent on every node", "daemon set \"nodeyard-agent\" successfully rolled out", "OK The node agent is installed."],
        }.get(action, ["(demo) " + " ".join(argv), "Done."])
        for line in script:
            time.sleep(0.5)
            with self.lock:
                job["lines"].append(line)
        with self.lock:
            job["status"], job["rc"] = "ok", 0

    def disk_models(self, force=False):
        G = GiB
        return {"ok": True, "in_use": "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf", "nodes": [
            {"node": "yard-1", "items": [{"kind": "disk", "capacity": 900 * G, "free": 801 * G},
                                         {"kind": "model", "bytes": int(17.3 * G), "name": "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"},
                                         {"kind": "model", "bytes": 7 * G, "name": "Ornith-1.5-9B-Q6_K.gguf"},
                                         {"kind": "partial", "bytes": int(1.2 * G), "name": "gemma-3-12b-it-Q4_K_M.gguf.part2"},
                                         {"kind": "cache", "bytes": int(9.6 * G), "name": "qwen3-coder-30b-a3b-instruct-q4-k-m"},
                                         {"kind": "cache", "bytes": 4 * G, "name": "ornith-1-5-9b-q6-k"}]},
            {"node": "yard-2", "items": [{"kind": "disk", "capacity": 58 * G, "free": 30 * G},
                                         {"kind": "cache", "bytes": int(5.4 * G), "name": "qwen3-coder-30b-a3b-instruct-q4-k-m"}]},
            {"node": "yard-3", "items": [{"kind": "disk", "capacity": int(12.5 * G), "free": int(3.9 * G)},
                                         {"kind": "cache", "bytes": int(2.3 * G), "name": ".cache"}]}],
            "downloads": [{"job": "dl-gemma", "file": "gemma-3-12b-it-Q4_K_M.gguf", "node": "yard-1", "size": int(7.3 * G),
                           "got": int((time.time() % 60) / 60 * 7.3 * G), "state": "running",
                           "rate": 7.3 * G / 60, "eta": int(60 - time.time() % 60)}]}

    def job(self, jid, since):
        with self.lock:
            j = self.jobs.get(jid)
            if not j:
                return None
            return {"id": jid, "title": j["title"], "status": j["status"], "rc": j["rc"], "cmd": j["cmd"], "started": j["started"], "lines": j["lines"][since:], "next": len(j["lines"])}
