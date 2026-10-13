"""AI › Recommended: the model to run for coding, chat, vision and OCR, reasoning, and small and fast, picked for these
machines (the best one that still answers at a decent speed here) next to the best one overall.

Speed is estimated the way `nodeyard ai split plan` ("Check speed") does: generating a token reads the model's active
weights once, so time per token = the bytes each machine reads / its memory speed, plus a network hop for every machine
past the first. File sizes come from Hugging Face (the same listing as Find models); when it can't be reached they are
estimated from the parameter count. Vision models run on Ollama (one machine), since split models have no image input.

  GET /api/ai/recommended   {categories: [{id, label, pick, best, others}], hardware: {...}}
"""
import concurrent.futures
import re
import threading
import time

GiB = 1024 ** 3
HOP_S = 0.040              # each machine past the first adds a network round trip per token (as the CLI assumes)
GPU_BW = 80.0              # GB/s a small NVIDIA card reaches
# tokens/s that feels quick for each kind of work (reasoning models write long hidden thoughts first), and the least
# that's still usable (about reading speed)
DECENT = {"coding": 5.0, "chat": 5.0, "reasoning": 6.0, "vision": 3.0, "small": 10.0}
USABLE = {"coding": 3.0, "chat": 3.0, "reasoning": 3.5, "vision": 2.0, "small": 6.0}
# quality lost to a smaller quantisation (points out of 100)
QLOSS = {"Q8_0": 0, "Q6_K_XL": 0, "Q6_K": 0, "Q5_K_XL": 1, "Q5_K_M": 1, "Q5_K_S": 1, "Q4_K_XL": 1, "Q4_K_M": 2, "MXFP4": 2,
         "IQ4_XS": 3, "Q4_K_S": 3, "IQ4_NL": 3, "Q3_K_XL": 4, "Q3_K_L": 5, "Q3_K_M": 6, "IQ3_M": 8, "IQ3_XXS": 11}
# quality of each quantisation (best first) and roughly its bytes per parameter, for estimates
QUANTS = [("Q8_0", 1.06), ("Q6_K_XL", 0.88), ("Q6_K", 0.82), ("Q5_K_XL", 0.74), ("Q5_K_M", 0.71), ("Q5_K_S", 0.69), ("Q4_K_XL", 0.63),
          ("Q4_K_M", 0.60), ("MXFP4", 0.56), ("IQ4_XS", 0.54), ("Q4_K_S", 0.57), ("IQ4_NL", 0.57), ("Q3_K_XL", 0.52), ("Q3_K_L", 0.53),
          ("Q3_K_M", 0.49), ("IQ3_M", 0.46), ("IQ3_XXS", 0.40)]
BPP = dict(QUANTS)
# files in a model's repo that aren't the model: speculative-decoding drafts, vision projectors, importance matrices
HELPER_RE = re.compile(r"(?:^|[/_.-])(?:eagle\d*|draft|mtp|mmproj|imatrix|projector|vision)(?:[_.-]|$)", re.I)
QRANK = {q: i for i, (q, _) in enumerate(QUANTS)}
CATEGORIES = [("coding", "Coding", "Writing and fixing code, and tool use for agents like yardcode"),
              ("chat", "General chat", "Everyday questions, writing, summaries and translation"),
              ("vision", "Vision and OCR", "Reading screenshots, photos and documents (attach an image in Chat)"),
              ("reasoning", "Reasoning and maths", "Step-by-step problems: it thinks before it answers"),
              ("small", "Small and fast", "Quick answers on modest machines, or a second model next to a big one")]

# (name, GGUF repo or None, Ollama tag or None, total params (B), active params (B), {category: quality 0-100}, note,
#  approximate Ollama download in GB for Ollama-only models)
CATALOGUE = [
    ("Qwen3-Coder 480B-A35B", "unsloth/Qwen3-Coder-480B-A35B-Instruct-GGUF", "qwen3-coder:480b", 480, 35, {"coding": 97}, "The strongest open coding model", 0),
    ("GLM-4.5-Air 106B-A12B", "unsloth/GLM-4.5-Air-GGUF", None, 106, 12, {"coding": 90, "chat": 86, "reasoning": 84}, "Excellent at agent-style coding", 0),
    ("gpt-oss 120B", "ggml-org/gpt-oss-120b-GGUF", "gpt-oss:120b", 117, 5.1, {"reasoning": 93, "coding": 88, "chat": 86}, "Strong reasoning, and fast for its size (5B active)", 0),
    ("Qwen3.6 35B-A3B", "unsloth/Qwen3.6-35B-A3B-GGUF", None, 35, 3, {"chat": 88, "coding": 86, "reasoning": 80}, "nodeyard's default: strong all-rounder, 3B active so it's quick", 0),
    ("Qwen3-Coder 30B-A3B", "unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF", "qwen3-coder:30b", 30.5, 3.3, {"coding": 85}, "Fast coder (3B active), good at tool calls", 0),
    ("gpt-oss 20B", "ggml-org/gpt-oss-20b-GGUF", "gpt-oss:20b", 21, 3.6, {"reasoning": 84, "coding": 80, "chat": 78}, "OpenAI's open model: good reasoning, very fast (3.6B active)", 0),
    ("Qwen3 30B-A3B Thinking", "unsloth/Qwen3-30B-A3B-Thinking-2507-GGUF", "qwen3:30b", 30.5, 3.3, {"reasoning": 86}, "Thinks step by step; quick for its quality", 0),
    ("Qwen3 30B-A3B Instruct", "unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF", "qwen3:30b", 30.5, 3.3, {"chat": 84, "coding": 76}, "Fast, capable general model (3B active)", 0),
    ("Qwen3 235B-A22B Instruct", "unsloth/Qwen3-235B-A22B-Instruct-2507-GGUF", "qwen3:235b", 235, 22, {"chat": 95}, "One of the best open chat models", 0),
    ("Qwen3 235B-A22B Thinking", "unsloth/Qwen3-235B-A22B-Thinking-2507-GGUF", None, 235, 22, {"reasoning": 96}, "Top open reasoning model", 0),
    ("Llama 3.3 70B", "bartowski/Llama-3.3-70B-Instruct-GGUF", "llama3.3:70b", 70.6, 70.6, {"chat": 85}, "Strong, dense and slow", 0),
    ("Devstral Small 24B", "unsloth/Devstral-Small-2507-GGUF", "devstral:24b", 23.6, 23.6, {"coding": 82}, "Built for coding agents", 0),
    ("Gemma 3 27B", "unsloth/gemma-3-27b-it-GGUF", "gemma3:27b", 27, 27, {"chat": 82, "vision": 82}, "Well-rounded, sees images", 17),
    ("Mistral Small 3.2 24B", "unsloth/Mistral-Small-3.2-24B-Instruct-2506-GGUF", None, 24, 24, {"chat": 80}, "Clear, concise answers", 0),
    ("QwQ 32B", "bartowski/Qwen_QwQ-32B-GGUF", "qwq:32b", 32.8, 32.8, {"reasoning": 84}, "Careful reasoner (dense, slow)", 0),
    ("Qwen2.5-Coder 14B", "bartowski/Qwen2.5-Coder-14B-Instruct-GGUF", "qwen2.5-coder:14b", 14.8, 14.8, {"coding": 74}, "Solid mid-size coder", 0),
    ("DeepSeek-R1 Distill 14B", "bartowski/DeepSeek-R1-Distill-Qwen-14B-GGUF", "deepseek-r1:14b", 14.8, 14.8, {"reasoning": 72}, "Reasoning in a mid-size model", 0),
    ("Gemma 3 12B", "unsloth/gemma-3-12b-it-GGUF", "gemma3:12b", 12, 12, {"chat": 74, "vision": 74}, "Good chat, sees images", 8.1),
    ("DeepSeek-R1 0528 Qwen3 8B", "unsloth/DeepSeek-R1-0528-Qwen3-8B-GGUF", "deepseek-r1:8b", 8.2, 8.2, {"reasoning": 74}, "Small reasoner that punches above its size", 0),
    ("Qwen3 8B", "Qwen/Qwen3-8B-GGUF", "qwen3:8b", 8.2, 8.2, {"chat": 70, "reasoning": 66}, "Capable small all-rounder", 0),
    ("Qwen2.5-Coder 7B", "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF", "qwen2.5-coder:7b", 7.6, 7.6, {"coding": 66}, "Small coder", 0),
    ("Qwen3 4B Instruct", "unsloth/Qwen3-4B-Instruct-2507-GGUF", "qwen3:4b", 4, 4, {"small": 70, "chat": 62}, "The best small model", 0),
    ("Gemma 3 4B", "unsloth/gemma-3-4b-it-GGUF", "gemma3:4b", 4.3, 4.3, {"small": 62, "vision": 60}, "Small, sees images", 3.3),
    ("Qwen2.5-Coder 3B", "Qwen/Qwen2.5-Coder-3B-Instruct-GGUF", "qwen2.5-coder:3b", 3.1, 3.1, {"coding": 55, "small": 55}, "Tiny coder for slow machines", 0),
    ("Llama 3.2 3B", "bartowski/Llama-3.2-3B-Instruct-GGUF", "llama3.2:3b", 3.2, 3.2, {"small": 56}, "Small and quick", 0),
    ("Qwen3 1.7B", "unsloth/Qwen3-1.7B-GGUF", "qwen3:1.7b", 1.7, 1.7, {"small": 46}, "Very fast, very small", 0),
    ("Gemma 3 1B", "unsloth/gemma-3-1b-it-GGUF", "gemma3:1b", 1, 1, {"small": 36}, "For the slowest machines", 0),
    # vision (Ollama: split models take no images)
    ("Qwen3-VL 235B-A22B", None, "qwen3-vl:235b", 235, 22, {"vision": 96}, "The strongest open vision model", 143),
    ("Qwen3-VL 30B-A3B", None, "qwen3-vl:30b", 31, 3.3, {"vision": 88}, "Excellent OCR and image understanding, quick (3B active)", 20),
    ("Qwen2.5-VL 72B", None, "qwen2.5vl:72b", 73, 73, {"vision": 90}, "Top-tier documents and charts (dense, slow)", 49),
    ("Qwen2.5-VL 32B", None, "qwen2.5vl:32b", 33, 33, {"vision": 85}, "Very good OCR", 21),
    ("Qwen3-VL 8B", None, "qwen3-vl:8b", 8.8, 8.8, {"vision": 80}, "Good OCR in a small model", 6.1),
    ("Qwen2.5-VL 7B", None, "qwen2.5vl:7b", 8.3, 8.3, {"vision": 76}, "Reliable OCR, small", 6.0),
    ("MiniCPM-V 8B", None, "minicpm-v:8b", 8.1, 8.1, {"vision": 72}, "Strong at reading text in images", 5.5),
    ("Qwen3-VL 4B", None, "qwen3-vl:4b", 4.4, 4.4, {"vision": 70}, "Small vision model", 3.3),
    ("Granite 3.2 Vision 2B", None, "granite3.2-vision:2b", 2.5, 2.5, {"vision": 58, "small": 40}, "Tiny, made for documents and forms", 2.4),
]


def node_bandwidth(n, bench):
    """GB/s a node's CPU reaches when generating (memory-bound; physical cores only), as split_node_speed in the CLI;
    a measured memory speed (`nodeyard hw bench`) caps it."""
    cores = int(n.get("cpu_cores") or 1)
    if (n.get("arch") or "") == "amd64":
        bw = max(1, cores // 2) * 3.0
    else:
        bw = max(1, cores) * 1.2
    bw = min(bw, 20.0)
    b = (bench or {}).get(n["name"]) or {}
    if b.get("mem_copy_all_gbs"):
        bw = min(bw, float(b["mem_copy_all_gbs"]) * 0.9)
    return max(0.5, bw)


def machines(state):
    """[{name, free, bw, gpu_free}] for the ready nodes: memory a model can use now (minus 1 GiB) and how fast."""
    out = []
    split_pods = [p for p in state.get("pods") or [] if p.get("namespace") == "ai-split"]
    for n in state.get("nodes") or []:
        if not n.get("ready"):
            continue
        hw = n.get("hw") or {}
        avail = hw.get("mem_available") if hw.get("mem_available") is not None else max(0, (n.get("mem_total") or 0) - (n.get("mem_used") or 0))
        mine = sum(p.get("mem") or 0 for p in split_pods if p.get("node") == n["name"])
        vram = (n.get("labels") or {}).get("nodeyard.io/gpu-vram-mib")
        out.append({"name": n["name"], "free": max(0, avail + mine - GiB), "total": n.get("mem_total") or 0, "bw": node_bandwidth(n, state.get("bench")),
                    "gpu_free": max(0, int(vram) * 1048576 - GiB) if vram and str(vram).isdigit() else 0})
    return out


def split_speed(size, act, nodes):
    """(tokens/s, machines used) for the fastest set of machines that holds the model, layers shared by speed; or (0, 0)."""
    need = size * 1.07 + GiB
    order = sorted(nodes, key=lambda m: -(m["bw"] + (GPU_BW if m["gpu_free"] else 0)))
    best = (0.0, 0)
    for k in range(1, len(order) + 1):
        use = order[:k]
        parts = []
        for m in use:         # a GPU is a fast "machine" of its own on the same node (no extra hop)
            if m["gpu_free"]:
                parts.append({"cap": m["gpu_free"], "bw": GPU_BW})
            parts.append({"cap": m["free"], "bw": m["bw"]})
        if sum(p["cap"] for p in parts) < need:
            continue
        left, share = need, {}
        live = list(range(len(parts)))
        while left > 1 and live:          # water-fill: in proportion to speed, capped at what fits
            tot = sum(parts[i]["bw"] for i in live)
            nxt = []
            given = 0
            for i in live:
                want = left * parts[i]["bw"] / tot
                room = parts[i]["cap"] - share.get(i, 0)
                g = min(want, room)
                share[i] = share.get(i, 0) + g
                given += g
                if room - g > 1:
                    nxt.append(i)
            left -= given
            live = nxt
            if given < 1:
                break
        # (placed by memory, which includes the 1 GiB of working buffers; read per token: only its share of the weights)
        t = sum(share[i] * (size / need) * act / (parts[i]["bw"] * 1e9) for i in share) + HOP_S * (k - 1)
        tps = 1.0 / t if t > 0 else 0.0
        if tps > best[0]:
            best = (round(tps, 1), k)
    return best


def single_speed(size, act, nodes):
    """(tokens/s, machine) on the one machine that runs it fastest (Ollama), or (0, "")."""
    best = (0.0, "")
    for m in nodes:
        if m["free"] + m["gpu_free"] < size * 1.1 + 0.5 * GiB:
            continue
        gpu = min(m["gpu_free"], size)
        t = (gpu * act / (GPU_BW * 1e9)) + ((size - gpu) * act / (m["bw"] * 1e9))
        tps = round(1.0 / t, 1) if t > 0 else 0.0
        if tps > best[0]:
            best = (tps, m["name"])
    return best


def active_fraction(total, active):
    return min(1.0, (active + 0.5) / total) if active < total else 1.0


class Recommender:
    def __init__(self, ctx, args):
        self.ctx = ctx
        self.demo = bool(getattr(args, "demo", False))
        self.lock = threading.Lock()
        self.files = {}          # repo -> (time, files | None)
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=6)

    def _hf(self):
        ai = getattr(self.ctx, "ai", None)
        return getattr(ai, "hf", None)

    def _files(self, repo, params):
        """The model's GGUFs by quant: {quant: (file, size, single file?)}; None (estimate instead) when Hugging Face
        can't be asked. Only files whose size fits the parameter count are the model: a repo can also hold a small
        draft model (gpt-oss has an 0.9 GB "eagle3" Q8_0 next to its 63 GB model) or a vision projector."""
        with self.lock:
            hit = self.files.get(repo)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
        got = None
        hf = self._hf()
        if hf is not None and not self.demo:
            try:
                got = {}
                for f in hf.files(repo):
                    q = (f.get("quant") or "").upper()
                    if q.startswith("UD-"):
                        q = q[3:]
                    if not q and "mxfp4" in f["file"].lower():
                        q = "MXFP4"
                    if q not in QRANK or HELPER_RE.search(f["file"].rsplit("/", 1)[-1]):
                        continue
                    expect = params * 1e9 * BPP[q]
                    if not 0.55 * expect <= f["size"] <= 1.6 * expect:
                        continue
                    if q not in got or abs(f["size"] - expect) < abs(got[q][1] - expect):
                        got[q] = (f["file"], f["size"], f.get("split_ok", True))
            except Exception:  # noqa: BLE001 -- offline or gone: estimate instead (marked as such)
                got = None
        with self.lock:
            self.files[repo] = (time.time(), got)
        return got

    def _options(self, entry, nodes, files):
        """Every quant of one model with its size, where it would run and how fast: [{quant, file, size, runner, tps, ...}]."""
        name, repo, tag, total, active, _q, _note, ollama_gb = entry
        act = active_fraction(total, active)
        opts = []
        if repo:
            known = files.get(repo)
            table = known if known else {q: (None, int(total * 1e9 * bpp), True) for q, bpp in QUANTS if q not in ("MXFP4",)}
            for q, (fname, size, single) in table.items():
                tps, used = split_speed(size, act, nodes) if single else (0.0, 0)
                o = {"quant": q, "file": fname, "size": size, "estimated": not known, "runner": "split" if single else "",
                     "tps": tps, "machines": used, "multi_part": not single}
                if tag:
                    o["ollama_tps"], o["ollama_node"] = single_speed(size, act, nodes)
                opts.append(o)
        elif tag:
            size = int(ollama_gb * 1e9) if ollama_gb else int(total * 1e9 * 0.62)
            tps, where = single_speed(size, act, nodes)
            opts.append({"quant": "Q4_K_M", "file": None, "size": size, "estimated": True, "runner": "ollama" if tps else "", "tps": tps, "machines": 1 if tps else 0,
                         "ollama_tps": tps, "ollama_node": where})
        opts.sort(key=lambda o: QRANK.get(o["quant"], 99))
        return opts

    def view(self):
        snap = self.ctx.store.snapshot()
        state = snap.get("state") or {}
        nodes = machines(state)
        params = {e[1]: e[3] for e in CATALOGUE if e[1]}
        repos = sorted(params)
        files = dict(zip(repos, self.pool.map(lambda r: self._files(r, params[r]), repos)))
        ollama = any(p.get("namespace", "").startswith("ollama") or "ollama" in (p.get("name") or "") for p in state.get("pods") or [])
        cats = []
        for cid, label, sub in CATEGORIES:
            entries = sorted([e for e in CATALOGUE if cid in e[5]], key=lambda e: -e[5][cid])
            rows = []
            for e in entries:
                if cid == "vision" and e[1] and not e[2]:
                    continue
                opts = self._options(e, nodes, files)
                if cid == "vision":
                    # images need Ollama (split models take no images): its standard download, on one machine
                    o = next((x for x in opts if x["quant"] == "Q4_K_M"), opts[0] if opts else None)
                    if o is None:
                        continue
                    if e[1] and e[7]:
                        o = dict(o, size=int(e[7] * 1e9), estimated=True, file=None)
                        o["ollama_tps"], o["ollama_node"] = single_speed(o["size"], active_fraction(e[3], e[4]), nodes)
                    o = dict(o, runner="ollama" if o.get("ollama_tps") else "", tps=o.get("ollama_tps") or 0.0, machines=1 if o.get("ollama_tps") else 0)
                    opts = [o]
                rows.append({"entry": e, "opts": opts})
            scored = sorted(((self._score(cid, r, o), r, o) for r in rows for o in [self._choice(cid, r)] if o), key=lambda x: -x[0])
            pick = self._pick(cid, scored, rows)
            best = self._best(cid, rows)
            cats.append({"id": cid, "label": label, "sub": sub, "decent": DECENT[cid], "pick": pick, "best": best,
                         "others": [self._card(cid, r, o, self._why(cid, o)) for _sc, r, o in scored
                                    if not (pick and pick["name"] == r["entry"][0]) and not (best and best["name"] == r["entry"][0])][:4]})
        return {"categories": cats, "ollama": ollama, "demo": self.demo,
                "hardware": {"machines": len(nodes), "free": sum(m["free"] for m in nodes), "biggest": max([m["free"] for m in nodes] or [0]),
                             "bandwidth": round(sum(m["bw"] for m in nodes), 1), "gpus": sum(1 for m in nodes if m["gpu_free"])}}

    @staticmethod
    def _score(cid, r, o):
        """How good a choice this is here: its quality (less what the quantisation costs), scaled down when it's slower
        than feels quick."""
        q = r["entry"][5][cid] - QLOSS.get(o["quant"], 6)
        return q * min(1.0, o["tps"] / DECENT[cid]) ** 0.5

    def _choice(self, cid, r):
        """This model's best quant here: the best-scoring one that runs at a usable speed, else the fastest that runs."""
        run = [o for o in r["opts"] if o["runner"] and o["tps"] > 0]
        if not run:
            return None
        usable = [o for o in run if o["tps"] >= USABLE[cid]]
        return max(usable, key=lambda o: self._score(cid, r, o)) if usable else max(run, key=lambda o: o["tps"])

    def _why(self, cid, o):
        where = ("across %d machines" % o["machines"]) if o["runner"] == "split" and o["machines"] > 1 else \
                ("on %s with Ollama" % o.get("ollama_node")) if o["runner"] == "ollama" else "on one machine"
        if o["tps"] >= DECENT[cid]:
            return "Quick here: about %g tokens/s %s" % (o["tps"], where)
        if o["tps"] >= USABLE[cid]:
            return "About %g tokens/s %s: the best quality that's still usable here (%g+ feels quick)" % (o["tps"], where, DECENT[cid])
        return "Only about %g tokens/s %s: your machines are slow for this kind of model" % (o["tps"], where)

    def _pick(self, cid, scored, rows):
        """The best model for these machines: the best score among the usable ones, else the fastest that fits."""
        usable = [x for x in scored if x[2]["tps"] >= USABLE[cid]]
        if usable:
            _sc, r, o = usable[0]
            return self._card(cid, r, o, self._why(cid, o))
        if scored:
            _sc, r, o = max(scored, key=lambda x: x[2]["tps"])
            return self._card(cid, r, o, self._why(cid, o))
        return None

    def _best(self, cid, rows):
        """The best model overall, at Q4 (or the nearest quant), whether or not it fits here."""
        if not rows:
            return None
        r = rows[0]
        q4 = next((o for o in r["opts"] if o["quant"] in ("Q4_K_M", "MXFP4")), None) or (r["opts"][len(r["opts"]) // 2] if r["opts"] else None)
        if q4 is None:
            return None
        runnable = [o for o in r["opts"] if o["runner"] and o["tps"] > 0]
        card = self._card(cid, r, runnable[0] if runnable else q4, "")
        card["fits"] = bool(runnable)
        if not runnable:
            card["why"] = "Too big for your machines: it needs about %.0f GiB of free memory at %s (you have %.0f GiB)" % (
                (q4["size"] * 1.07 + GiB) / GiB, q4["quant"], sum(m["free"] for m in machines(self.ctx.store.snapshot().get("state") or {})) / GiB)
        return card

    @staticmethod
    def _card(cid, r, o, why=""):
        name, repo, tag, total, active, quality, note, _gb = r["entry"]
        return {"name": name, "repo": repo, "ollama": tag, "note": note, "quality": quality[cid], "params": total, "active": active,
                "quant": o["quant"], "file": o["file"], "size": o["size"], "estimated": o["estimated"], "runner": o["runner"],
                "tps": o["tps"], "machines": o["machines"], "ollama_tps": o.get("ollama_tps"), "ollama_node": o.get("ollama_node"),
                "multi_part": o.get("multi_part", False), "why": why, "fits": bool(o["runner"] and o["tps"] > 0)}


def register(ctx, args):
    rec = Recommender(ctx, args)
    ctx.recommender = rec

    def view(h, q):
        try:
            h._json(dict(rec.view(), ok=True))
        except Exception as e:  # noqa: BLE001 -- a recommendation must never take the page down
            h._json({"ok": False, "error": "Couldn't work out recommendations: %s" % str(e)[:200]}, 500)

    ctx.get_routes["/api/ai/recommended"] = view
