"""Train AI: train your own language model from scratch, and learn exactly how it works.

The page (web/train.js) teaches the maths, checks the training data you give it, picks settings for this
machine's hardware (and shows the formula and numbers behind every one), runs the trainer, draws the loss as it
learns, lets you chat with the result, and shows the value of every variable in one real forward and backward pass.

The trainer is share/nodeyard/jobs/train_bpe_gpt_torch.py. It never runs inside the dashboard (whose service is
capped at 768 MB): trainpool.py starts a training worker on the chosen machines (a pod per cluster node, or a local
process without a cluster) that installs PyTorch by itself the first time. The fastest machine trains; machines at
least a quarter as fast can share the job, each training its own copy for a round and then averaging their weights.
NODEYARD_TRAIN_PYTHON makes local workers use a Python that already has PyTorch.

Everything lives under <state_dir>/train/: data/ (your uploaded files), models/<name>/ (one folder per model, copied
back from the workers), job.json and logs/ (the current or last run, so a dashboard restart picks a job back up),
workers/ (local workers), worker-token and speeds.json (each machine's measured speed).
"""
import base64
import binascii
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time

import ptread
import trainsets
import trainpool

HERE = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(os.path.dirname(HERE), "jobs")
TRAINER = os.path.join(JOBS_DIR, "train_bpe_gpt_torch.py")

DATA_EXT = (".txt", ".md", ".jsonl", ".json")
UPLOAD_CHUNK_MAX = 4 * 1024 * 1024
UPLOAD_BODY_MAX = UPLOAD_CHUNK_MAX * 4 // 3 + 4096
DATA_TOTAL_MAX = 20 * 1024 * 1024 * 1024
PASTE_MAX = 8 * 1024 * 1024
CHECK_SAMPLE = 16 * 1024 * 1024   # bigger files are checked from their first 16 MB (the dashboard has 768 MB)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}$")
STEP_RE = re.compile(r"^Step (\d+)/(\d+) \| train loss ([-\d.eE+naif]+) \| ([\d.]+) steps/sec \| ([\d,]+) tokens/sec \| "
                     r"total training step ([\d,]+) \| learning rate ([-\d.eE+]+)")
VAL_RE = re.compile(r"^Validation at step (\d+): loss ([-\d.eE+naif]+) \| total training step ([\d,]+)")
STEP_GRAD_RE = re.compile(r"\| gradient norm ([\d.eE+-]+)\s*$")
GRAD_RE = re.compile(r"^Gradient norm at step (\d+): ([\d.eE+-]+)")
MODEL_FILES = ("model.pt", "optimizer.pt", "bpe.json", "model.json")
CHARS_PER_TOKEN = 3.6        # typical for a learned BPE vocabulary on English; the trainer prints the real figure

class TrainError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


# -- maths shared by the recommender and the tests ------------------------------------------------------------

def count_parameters(vocab, context, width, layers):
    """Exact number of weights in the trainer's GPT.

    token embedding V·d + position embedding T·d
    + each block: attention (Wq,Wk,Wv: 3d² + 3d, output: d² + d) + feed-forward (d·4d + 4d, 4d·d + d) + two layer norms (4d)
      = 12d² + 13d
    + final layer norm 2d + output layer d·V + V
    """
    d = width
    return vocab * d + context * d + layers * (12 * d * d + 13 * d) + 2 * d + d * vocab + vocab


def activation_bytes(batch, context, width, layers, heads, vocab):
    """Memory the forward pass keeps for the backward pass, float32.

    Per block and token about 34·d floats (inputs to each matrix multiply, the layer norms, the 4d hidden layer and
    GELU) plus 5·h·T for the attention scores and weights; then the logits, their softmax and gradient: 3·V per token.
    """
    per_token = layers * (34 * width + 5 * heads * context) + 3 * vocab
    return 4 * batch * context * per_token


def flops_per_step(params, batch, context):
    """About 6 floating-point operations per weight per token: 2 forward (multiply + add), 4 backward."""
    return 6 * params * batch * context


def estimate_speed(device, cores=0, arch="", gpu=None):
    """Sustained FLOP/s this trainer gets, as a rough estimate (the real speed is shown once training runs)."""
    if device == "cuda":
        vram = (gpu or {}).get("vram") or 0
        return 0.8e12 if vram < 6e9 else 3e12 if vram < 12e9 else 8e12
    if device == "mps":
        return 1.5e12
    per_core = 2e9 if arch in ("aarch64", "arm64", "armv7l") and platform.system() != "Darwin" else 8e9
    return max(1, cores) * per_core


def shape_for(target, vocab, context):
    """The widest/deepest shape whose parameter count fits the target. Head size 32 (64 from width 512), depth ≈ width/48."""
    best = None
    for width in range(32, 2049, 32):
        layers = max(1, min(24, round(width / 48)))
        heads = max(1, width // (64 if width >= 512 else 32))
        n = count_parameters(vocab, context, width, layers)
        if best is not None and n > target:
            break
        best = {"width": width, "layers": layers, "heads": heads, "parameters": n}
    return best


def _pow2_floor(x):
    return 1 << max(0, int(math.floor(math.log2(max(1, x)))))


def recommend(hw, data_chars=None, hours=1.0, profile=None):
    """Settings for this hardware and this data, each with the formula, the numbers and why.

    hw: {"device": cuda|mps|cpu, "cores", "arch", "ram_free", "gpu": {"name", "vram"} or None}
    profile (from checking the data): {"unique_fraction": share of the text that isn't a repeat,
             "example_chars": typical length of one example (a conversation or paragraph), "chat": conversations?}
    """
    profile = profile or {}
    device = hw.get("device") or "cpu"
    cores = hw.get("cores") or 1
    ram_free = hw.get("ram_free") or 2 * 1024 ** 3
    gpu = hw.get("gpu")
    why = []

    if hw.get("budget") and len(hw.get("machines") or []) > 1:
        budget = int(hw["budget"])
        budget_text = "the smallest share among the machines training together"
    elif device == "cuda":
        budget = int(0.7 * (gpu or {}).get("vram", 0))
        budget_text = "70%% of the GPU's %s of memory" % _gb((gpu or {}).get("vram", 0))
    else:
        budget = int(max(256 * 1024 ** 2, min(0.5 * ram_free, ram_free - 1024 ** 3)))
        budget_text = "half the free RAM (%s free), keeping 1 GB for everything else" % _gb(ram_free)
    speed = hw.get("speed") or estimate_speed(device, cores, hw.get("arch", ""), gpu)
    seconds = max(60.0, float(hours) * 3600)
    compute = speed * seconds

    unique = profile.get("unique_fraction")
    repeated = data_chars is not None and unique is not None and unique < 0.9
    tokens = int((data_chars or 4_000_000) * (unique if repeated else 1.0) / CHARS_PER_TOKEN)
    if repeated:
        why.append({"name": "Training text", "value": "%s new tokens" % _n(tokens), "formula": "tokens ≈ unique characters ÷ 3.6",
                    "numbers": "%s characters, but only %d%% isn't a repeat → %s ÷ 3.6 = %s" % (_n(data_chars), round(unique * 100), _n(data_chars * unique), _n(tokens)),
                    "explain": "Lines that appear more than once (for example every line as both a question and the next reply) teach nothing new the second time, so the model is sized for the text that's actually different."})
    elif data_chars is None:
        why.append({"name": "Training text", "value": "not chosen yet", "formula": "tokens ≈ characters ÷ 3.6",
                    "numbers": "assuming 4,000,000 characters ≈ %s tokens" % _n(tokens),
                    "explain": "Choose your data in step 2 and these numbers update for it."})
    else:
        why.append({"name": "Training text", "value": "%s tokens" % _n(tokens), "formula": "tokens ≈ characters ÷ 3.6",
                    "numbers": "%s ÷ 3.6 = %s" % (_n(data_chars), _n(tokens)),
                    "explain": "A token is a piece of a word. A BPE vocabulary learned from English averages about 3.6 characters per token; the trainer prints the exact figure."})

    if data_chars is None or data_chars < 1_000_000:
        vocab = 2048
    elif data_chars < 20_000_000:
        vocab = 8192
    else:
        vocab = 16384
    why.append({"name": "Vocabulary size V", "value": _n(vocab), "formula": "2,048 under 1 MB of text · 8,192 under 20 MB · 16,384 above",
                "numbers": "%s characters → V = %s" % (_n(data_chars or 0), _n(vocab)),
                "explain": "How many different tokens the model knows. Each needs its own row of d numbers in and out of the model (2·V·d weights), so a big vocabulary on little text wastes weights on tokens it rarely sees."})

    # Three limits on the model size N; the smallest wins.
    n_compute = math.sqrt(compute / 120)
    n_data = tokens * 10
    context0 = 128
    n_memory = budget / 20      # 16 bytes per weight for weights+gradients+AdamW m and v, plus room for activations
    caps = [("compute", n_compute), ("data", n_data), ("memory", n_memory)]
    limit_name, target = min(caps, key=lambda c: c[1])
    target = max(50_000, min(target, 300_000_000))
    why.append({"name": "Model size N (weights)", "value": _n(target), "formula": "N = min(√(C ÷ 120), 10 × tokens, memory ÷ 20 bytes)",
                "numbers": "compute C = %s FLOP/s × %s s = %s → √(C÷120) = %s · data %s × 10 = %s · memory %s ÷ 20 = %s → %s is the limit"
                           % (_sci(speed), _n(seconds), _sci(compute), _n(n_compute), _n(tokens), _n(n_data), _gb(budget), _n(n_memory), limit_name),
                "explain": "Compute: the Chinchilla rule says train on about 20 tokens per weight, and training costs 6 FLOPs per weight per token, so C = 6·N·20N = 120·N². Data: with little text a bigger model still does better as long as dropout stops it memorising and you stop when validation stops falling (nanoGPT's Shakespeare model has about 10 weights per character of text), so this allows up to 10 weights per token, and the trainer keeps the checkpoint with the best validation. A smaller cap makes a model too small to learn the text, and the loss goes flat early. Memory: float32 weights, gradients and AdamW's two moments are 16 bytes per weight. %s" % ("Speed here is a guess for this hardware (%s FLOP/s); the real tokens/sec shows once training starts." % _sci(speed))})

    context = 64 if target < 300_000 else 256 if target > 5_000_000 and device != "cpu" else context0
    base_context = context
    example_tokens = (profile.get("example_chars") or 0) / CHARS_PER_TOKEN
    if example_tokens:   # make room for a whole typical example (plus a quarter), up to 512
        fit = 64
        while fit < min(512, example_tokens * 1.25):
            fit *= 2
        context = max(context, fit)
    # Keep the vocabulary's two tables (in and out, 2·V·d) to at most half the weights, or the model is all lookup table.
    wanted = vocab
    while True:
        shape = shape_for(target, vocab, context)
        if vocab <= 512 or 2 * vocab * shape["width"] <= 0.5 * shape["parameters"]:
            break
        vocab //= 2
    if vocab != wanted:
        why[1]["value"] = _n(vocab)
        why[1]["numbers"] += " → lowered to %s so the embedding tables (2·V·d) stay under half the model" % _n(vocab)
    width, layers, heads, params = shape["width"], shape["layers"], shape["heads"], shape["parameters"]
    why.append({"name": "Context length T", "value": str(context),
                "formula": "64 for tiny models · 128 normally · 256 for > 5M weights on a GPU; raised to fit a typical example (up to 512)",
                "numbers": ("N = %s on %s → T = %d" % (_n(target), device, base_context)) + (
                    " · a typical example is %s characters ≈ %d tokens → %d" % (_n(profile["example_chars"]), example_tokens, context) if example_tokens else ""),
                "explain": "How many tokens the model can look back at. Attention costs grow with T² and memory with T, so longer context is only worth it once the model is big enough to use it."})
    why.append({"name": "Width d, layers L, heads h", "value": "d=%d · L=%d · h=%d" % (width, layers, heads),
                "formula": "largest d (step 32) with L ≈ d ÷ 48 and head size 32 (64 from d = 512) where params ≤ N",
                "numbers": "params = V·d + T·d + L·(12d² + 13d) + 2d + d·V + V = %s·%d + %d·%d + %d·(12·%d² + 13·%d) + 2·%d + %d·%s + %s = %s"
                           % (_n(vocab), width, context, width, layers, width, width, width, width, _n(vocab), _n(vocab), _n(params)),
                "explain": "Width is how many numbers describe each token; layers is how many times the model refines them; heads split attention into parallel lookups. Deep-and-narrow and wide-and-shallow both work worse than a balance."})

    act_one = activation_bytes(1, context, width, layers, heads, vocab)
    weights_bytes = 16 * params
    room = max(act_one, budget - weights_bytes)
    batch = max(1, min(64, _pow2_floor(room / act_one), _pow2_floor(16384 / context)))
    why.append({"name": "Batch size B", "value": str(batch), "formula": "largest power of 2 ≤ (memory − 16·N) ÷ activation memory per sequence, at most 16,384 tokens per step",
                "numbers": "(%s − %s) ÷ %s per sequence → B = %d (%s tokens per step)" % (_gb(budget), _gb(weights_bytes), _mb(act_one), batch, _n(batch * context)),
                "explain": "How many text windows are averaged into one update. Bigger batches give a less noisy gradient but need memory for every token's intermediate values."})

    per_step = flops_per_step(params, batch, context)
    steps_time = compute / per_step
    steps_data = 50 * tokens / (batch * context)
    steps = int(max(200, min(200_000, steps_time, steps_data)) // 50 * 50)
    epochs = steps * batch * context / max(1, tokens)
    why.append({"name": "Steps", "value": _n(steps), "formula": "min(C ÷ (6·N·B·T), 50 passes over the data)",
                "numbers": "%s ÷ (6·%s·%d·%d) = %s · 50·%s ÷ (%d·%d) = %s → %s steps ≈ %.1f passes over your text"
                           % (_sci(compute), _n(params), batch, context, _n(steps_time), _n(tokens), batch, context, _n(steps_data), _n(steps), epochs),
                "explain": "One step = read a batch, measure the loss, compute gradients, update every weight once. Reading text up to 4 times is about as good as new text; more passes still help, less each time (Muennighoff et al., 2023; nanoGPT's Shakespeare run reads its text about 80 times), so this stops at 50. If validation starts rising, the model is memorising: the trainer keeps the best checkpoint, so stopping late costs nothing but time."})
    ratio = target / max(1, tokens)
    dropout = 0.2 if ratio > 0.5 else 0.1 if ratio > 0.05 else 0.0
    why.append({"name": "Dropout", "value": "%g" % dropout, "formula": "0.2 above 1 weight per 2 tokens · 0.1 above 1 per 20 · otherwise 0 (nanoGPT uses 0.2 on Shakespeare)",
                "numbers": "%s weights ÷ %s tokens = %.2f weights per token → dropout %g" % (_n(target), _n(tokens), ratio, dropout),
                "explain": "While training, each block zeroes this fraction of its values at random, so the model can't memorise through any one path. Needed when the model is big for its text; with plenty of text it only slows learning."})

    lr = 1e-3 if params < 20e6 else 6e-4 if params < 100e6 else 3e-4
    warmup = int(max(20, min(2000, steps * 0.05)))
    why.append({"name": "Learning rate η", "value": "%g" % lr, "formula": "1e-3 under 20M weights · 6e-4 under 100M · 3e-4 above; warm up for 5% of steps, then cosine decay to 10%",
                "numbers": "N = %s → η = %g, warm-up %s steps" % (_n(params), lr, _n(warmup)),
                "explain": "How far each update moves the weights. Too high and the loss jumps or becomes NaN; too low and it barely moves. Big models need smaller steps."})
    eval_every = int(max(25, min(1000, steps // 20)))
    seconds_est = steps * per_step / speed
    total_mem = weights_bytes + batch * act_one
    chat = bool(profile.get("chat", hw.get("chat")))
    why.append({"name": "Learn only the replies", "value": "on" if chat else "off", "formula": "on when the data is conversations",
                "numbers": "conversations found" if chat else "no User:/Assistant: turns found",
                "explain": "With conversations, the loss counts only the Assistant's words, so the model learns to answer rather than to write questions. Plain text has no turns: every token is learned."})
    expect = expectation(params, tokens, chat=chat)
    return {
        "device": device, "speed": speed, "budget": budget, "budget_text": budget_text, "limit": limit_name,
        "settings": {"vocab_size": vocab, "context": context, "dim": width, "layers": layers, "heads": heads,
                     "batch_size": batch, "steps": steps, "learning_rate": lr, "warmup": warmup, "weight_decay": 0.1,
                     "eval_every": eval_every, "dropout": dropout, "assistant_only": chat},
        "parameters": params, "tokens": tokens, "seconds": seconds_est, "memory": total_mem, "why": why, "expect": expect,
    }


QWEN_SMALL = {"name": "Qwen2.5-0.5B", "params": 494e6, "tokens": 18e12}


def expectation(params, tokens, chat=False):
    """What a model this size, on this much text, can be expected to do: honest numbers, not hype."""
    if params < 1e6:
        level = "Learns which letters and word pieces go together. Output looks like words, rarely sentences."
    elif params < 10e6:
        level = "Learns spelling, common words and grammar in the style of your text. Sentences, but meaning drifts."
    elif params < 60e6:
        level = "Coherent sentences and short paragraphs in your text's style; repeats facts that appear many times."
    else:
        level = "Fluent paragraphs; simple question answering if trained on lots of conversation data."
    if chat:
        level += " Trained on conversations, it learns the User:/Assistant: turn-taking and the style of replies."
    qwen_compute = 6 * QWEN_SMALL["params"] * QWEN_SMALL["tokens"]
    return {"level": level, "qwen": QWEN_SMALL, "qwen_compute": qwen_compute,
            "ratio_params": QWEN_SMALL["params"] / max(1, params), "ratio_tokens": QWEN_SMALL["tokens"] / max(1, tokens)}


def _n(x):
    return "{:,}".format(int(round(x)))


def _sci(x):
    return "%.2g" % x


def _gb(b):
    return "%.1f GB" % (b / 1024 ** 3)


def _mb(b):
    return "%.1f MB" % (b / 1024 ** 2)


# -- this machine's hardware ---------------------------------------------------------------------------------

def read_memory():
    """(total, available) bytes."""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            info = {line.split(":")[0]: int(line.split()[1]) * 1024 for line in f if ":" in line and line.split()[1].isdigit()}
        return info.get("MemTotal", 0), info.get("MemAvailable", info.get("MemFree", 0))
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout.strip())
            vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
            page = int(re.search(r"page size of (\d+)", vm).group(1))
            free = sum(int(m) for k, m in re.findall(r"Pages (free|inactive|speculative|purgeable):\s+(\d+)", vm))
            return total, free * page
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            pass
    return 0, 0


def node_gpu(node):
    """The node's NVIDIA GPU as {"name", "vram"} (bytes), or None.

    Prefers the live hardware inventory, but falls back to the durable node labels the AI-split setup puts on a GPU
    node after it has checked that nvidia-smi works in a container there (nodeyard.io/gpu=nvidia, VRAM in
    nodeyard.io/gpu-vram-mib). The label survives when a node's agent inventory is momentarily missing (debian-1's
    server->node path drops out often), so the machine keeps its GPU here instead of being shown as a plain CPU box.
    This is the same label pod_manifest() provisions the GPU from, so the table and the pod now agree."""
    gpus = (((node.get("hw") or {}).get("hardware") or {}).get("gpus")) or []
    nvidia = [g for g in gpus if (g.get("vendor") or "").lower() == "0x10de" or "nvidia" in (g.get("name_full") or "").lower()]
    if nvidia:
        g = nvidia[0]
        return {"name": g.get("name_full") or "NVIDIA GPU", "vram": g.get("vram") or 4e9}
    labels = node.get("labels") or {}
    if labels.get("nodeyard.io/gpu") == "nvidia":
        try:
            vram = int(labels.get("nodeyard.io/gpu-vram-mib")) * 1024 ** 2
        except (TypeError, ValueError):
            vram = 4e9
        return {"name": "NVIDIA GPU", "vram": vram}
    return None


def node_speed(node):
    """(device, estimated FLOP/s, GPU name) for a cluster node, from what the dashboard already knows about it."""
    g = node_gpu(node)
    if g:
        return "cuda", estimate_speed("cuda", gpu={"vram": g["vram"]}), g["name"]
    arch = node.get("arch", "")
    return "cpu", estimate_speed("cpu", node.get("cpu_cores") or 1, "aarch64" if arch.startswith("arm") else arch), ""


# -- the manager ---------------------------------------------------------------------------------------------

class Trainer:
    def __init__(self, state_dir, demo=False, store=None, kubeconfig=None):
        self.root = os.path.join(state_dir, "train")
        self.data_dir = os.path.join(self.root, "data")
        self.models_dir = os.path.join(self.root, "models")
        self.logs_dir = os.path.join(self.root, "logs")
        for d in (self.data_dir, self.models_dir, self.logs_dir):
            os.makedirs(d, exist_ok=True)
        self.demo = demo
        self.store = store
        self.lock = threading.Lock()
        self.pool = trainpool.Pool(self.root, demo=demo, store=store, kubeconfig=kubeconfig)
        self.thread = None
        self.parsed = {"path": None, "offset": 0, "train": [], "val": [], "grad": [], "lines": [], "last": {}, "quality": [], "verdict": None}
        self.parse_lock = threading.Lock()
        self.check_cache = {}
        self.uploads = {}
        self._probing = set()        # machines whose link is being measured right now (one probe at a time each)
        self._probe_lock = threading.Lock()
        self.speeds = self._load_json("speeds.json")
        self.fetcher = trainsets.Fetcher(self.data_dir, lambda: DATA_TOTAL_MAX - self._data_total())
        self.job = self._load_job()
        if self.job.get("model") and not self.job.get("ended"):
            if self.job.get("machines"):   # the dashboard restarted mid-job: pick it back up
                self.thread = threading.Thread(target=self._orchestrate, args=(True,), daemon=True)
                self.thread.start()
            else:                           # a job from before training moved onto workers
                self.job.update(ended=time.time(), code=None)

    def _load_json(self, name):
        try:
            with open(os.path.join(self.root, name), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    # -- machines
    def machines(self, both=False):
        """Every machine that could train, fastest first, with why any can't, and its worker's state.

        both=True also lists the CPU half of each GPU machine ("<name> · CPU") as its own entry, so a GPU machine can
        train on its GPU and its CPU at the same time: the two are separate training participants that get averaged
        together like any other machines."""
        out = []
        if self.pool.mode == "cluster":
            for n in self.pool.cluster_nodes():
                device, speed, _ = node_speed(n)
                gpu = node_gpu(n)
                total, used = n.get("mem_total") or 0, n.get("mem_used") or 0
                free_disk = (n.get("disk_total") or 0) - (n.get("disk_used") or 0)
                need_disk = 4e9 if device == "cuda" else 1.5e9
                reason = ""
                if not n.get("ready"):
                    reason = "not ready: Kubernetes hasn't heard from it"
                elif n.get("arch") in ("arm", "386"):
                    reason = "a 32-bit CPU: PyTorch has no build for it"
                elif n.get("disk_total") and free_disk < need_disk:
                    reason = "only %s of disk free; PyTorch needs about %s" % (_gb(free_disk), _gb(need_disk))
                elif total and total < 1.5 * 1024 ** 3:
                    reason = "under 1.5 GB of memory"
                out.append({"name": n.get("name"), "kind": "node", "device": device, "gpu": gpu,
                            "cores": n.get("cpu_cores") or 1, "arch": n.get("arch", ""), "ram_total": total, "ram_free": max(0, total - used),
                            "speed": speed, "usable": not reason, "reason": reason, "_node": n})
        else:
            total, free = read_memory()
            names = self.pool.local_names()
            for name in names:   # several local workers (a test setup) share this machine's cores and memory
                cores = max(1, (os.cpu_count() or 1) // len(names))
                out.append({"name": name, "kind": "local", "device": "cpu", "gpu": None, "cores": cores, "arch": platform.machine(),
                            "ram_total": total, "ram_free": free // len(names), "speed": estimate_speed("cpu", cores, platform.machine()),
                            "usable": True, "reason": "", "_node": None})
        for m in out:
            e = self.pool.entry(m["name"])
            st = e.get("status") or {}
            probe = st.get("probe") or {}
            if e["state"] == "ready":   # the worker knows what PyTorch can really use there
                if probe.get("cuda") and probe.get("gpu"):          # really training on the GPU
                    m["device"], m["gpu"] = "cuda", {"name": probe["gpu"]["name"], "vram": probe["gpu"]["vram"]}
                elif m["gpu"] and probe.get("gpu"):
                    # The node has a GPU and this worker can see it, but PyTorch can't use it right now (a CPU-only
                    # build, or the driver is busy). Keep showing the GPU and its estimate — the worker's cuda_error
                    # note explains the CPU fall-back — instead of relabelling it a plain CPU box.
                    m["device"], m["gpu"] = "cuda", {"name": probe["gpu"]["name"], "vram": probe["gpu"]["vram"]}
                elif m["gpu"] and st.get("device") != "cuda" and not probe.get("torch"):
                    pass   # worker still starting: keep the node's known GPU until it has really probed
                else:
                    m["device"] = st.get("device") or m["device"]
                    if m["device"] != "cuda":
                        m["gpu"] = None
                if m["kind"] == "local" or st.get("ram_free"):
                    m["ram_free"] = st.get("ram_free") or m["ram_free"]
                m["speed"] = estimate_speed(m["device"], m["cores"], m["arch"], m["gpu"])
            if e["state"] == "failed" and m["usable"]:
                m["usable"], m["reason"] = False, e["detail"]
            measured = self.speeds.get(m["name"])
            if measured and measured.get("device") == m["device"]:
                m["speed"], m["measured"] = measured["flops"], True
            # What this machine's CPU alone would add. For a CPU machine that's its own speed; for a GPU
            # machine it's the idle cores a CPU co-worker ("<name> · CPU") could put to work.
            m["cpu_speed"] = m["speed"] if m["device"] == "cpu" else estimate_speed("cpu", m["cores"], m["arch"])
            m["worker"] = {"state": e["state"], "detail": e["detail"], "log": e.get("deplog") or [],
                           "torch": probe.get("torch"), "cuda_error": probe.get("cuda_error")}
        if both:
            out += self._cpu_variants(out)
        out.sort(key=lambda m: (not m["usable"], -m["speed"]))
        return out

    def _cpu_variants(self, machines):
        """A CPU-only co-worker for each usable GPU machine, so its idle cores train alongside its GPU."""
        extra = []
        for m in machines:
            if m["device"] != "cuda" or not m.get("gpu") or not m["usable"]:
                continue
            name = trainpool.cpu_name(m["name"])
            e = self.pool.entry(name)
            speed = m["cpu_speed"]
            cm = {"name": name, "kind": m["kind"], "device": "cpu", "gpu": None, "cores": m["cores"],
                  "arch": m["arch"], "ram_total": m["ram_total"], "ram_free": (m.get("ram_free") or 0) // 2,
                  "speed": speed, "cpu_speed": speed, "usable": True, "reason": "", "_node": m.get("_node"),
                  "variant": "cpu", "parent": m["name"]}
            measured = self.speeds.get(name)
            if measured and measured.get("device") == "cpu":
                cm["speed"], cm["measured"] = measured["flops"], True
            cm["worker"] = {"state": e["state"], "detail": e["detail"], "log": e.get("deplog") or [],
                            "torch": None, "cuda_error": None}
            extra.append(cm)
        return extra

    @staticmethod
    def budget_for(m):
        """Memory this machine can give training (bytes)."""
        if m["device"] == "cuda" and m.get("gpu"):
            return int(0.7 * m["gpu"]["vram"])
        free = m.get("ram_free") or 2 * 1024 ** 3
        return int(max(256 * 1024 ** 2, min(0.5 * free, free - 1024 ** 3)))

    def plan(self, machines, chosen=None, links=None, model_bytes=0):
        """The fastest usable machine leads; others join while they're at least a quarter as fast (at most 4).

        links (optional, from measure_link) is {name: {"rtt_ms", "mbit"}}; when given, a machine is left out if the
        round's weight swap over its link would eat more than it adds — so sharing is only chosen when it's worth it.
        A machine's own CPU half ("<name> · CPU", present only when machines() was asked for both) rides along with
        it: same machine, no network, free idle cores."""
        coworkers = {m["parent"]: m for m in machines if m.get("variant") == "cpu" and m["usable"]}
        usable = [m for m in machines if m["usable"] and not m.get("variant") and (not chosen or m["name"] in chosen)]
        notes = []
        if not usable:
            why = "; ".join("%s: %s" % (m["name"], m["reason"]) for m in machines if not m["usable"]) or "no machines found"
            return {"lead": None, "share": [], "notes": ["Nothing can train right now (%s)." % why], "speed": 0, "budget": 0}
        lead = usable[0]
        share = [lead]
        for m in usable[1:]:
            ratio = m["speed"] / max(1.0, lead["speed"])
            if len(share) >= 4:
                notes.append("%s left out: at most 4 machines share one model." % m["name"])
            elif ratio < 0.25 and not chosen:
                notes.append("%s left out: about %s× slower than %s, so it would add under %d%% and slow every round's weight swap."
                             % (m["name"], _n(1 / max(ratio, 1e-9)), lead["name"], max(1, int(ratio * 100))))
            elif links is not None and not chosen and not self._link_worth(m, links, model_bytes)[0]:
                notes.append("%s left out: %s" % (m["name"], self._link_worth(m, links, model_bytes)[1]))
            else:
                share.append(m)
        members = []
        for m in share:
            members.append(m)
            co = coworkers.get(m["name"])
            if co:
                members.append(co)
        if coworkers:
            notes.append("Each GPU machine also trains on its own CPU (a second trainer on the same machine), so its "
                         "idle cores help too — same machine, so there's no network cost between them.")
        speed = members[0]["speed"] + 0.8 * sum(m["speed"] for m in members[1:])
        if len(members) > 1:
            notes.append("Shared: each machine trains its own copy for about 2 minutes, then the copies' weights are averaged "
                         "(weighted by how much text each read) and every machine carries on from the average. "
                         "Speed ≈ fastest + 80% of the others (the swaps cost the rest).")
        return {"lead": lead["name"], "share": [m["name"] for m in members], "notes": notes, "speed": speed,
                "budget": min(self.budget_for(m) for m in members)}

    @staticmethod
    def _link_worth(m, links, model_bytes, round_seconds=120.0):
        """Is it worth sharing with this machine, given the latency/bandwidth to it? A round is about round_seconds of
        training plus two weight swaps (send this copy to the lead, pull the average back). Not worth it if the swaps
        would eat more than half the round."""
        link = links.get(m["name"]) or {}
        rtt = (link.get("rtt_ms") or 0) / 1000.0
        mbit = link.get("mbit") or 0
        swap = 2 * rtt + (2 * model_bytes * 8 / (mbit * 1e6) if mbit and model_bytes else 0)
        if swap > 0.5 * round_seconds:
            return False, ("the link is too slow — swapping weights each round would cost about %ds of every %ds round"
                           % (int(swap), int(round_seconds)))
        return True, ""

    def measure_links(self, machines):
        """Return the cached latency/bandwidth to every machine, and quietly refresh any that's ready and stale.

        The refresh runs in the background (one probe per machine at a time) so a slow link never holds up the page."""
        now = time.time()
        for m in machines:
            if (m.get("worker") or {}).get("state") != "ready":
                continue
            name = m["name"]
            link = self.pool.links.get(name)
            if link and now - link.get("at", 0) < 90:
                continue
            with self._probe_lock:
                if name in self._probing:
                    continue
                self._probing.add(name)
            threading.Thread(target=self._probe_link, args=(name,), daemon=True).start()
        return {n: {"rtt_ms": v.get("rtt_ms"), "mbit": v.get("mbit")} for n, v in self.pool.links.items()}

    def _probe_link(self, name):
        try:
            r = self.pool.entry(name).get("remote")
            if r:
                self.pool.measure_link(name, r, bandwidth=True, cache_seconds=600)
        except Exception:   # noqa: BLE001 - a probe must never take the dashboard down
            pass
        finally:
            with self._probe_lock:
                self._probing.discard(name)

    def autosetup(self, body):
        """One button: choose the best machines (each one's GPU and CPU together), warm their workers up, measure the
        links between them, and hand back a ready-to-run configuration — which machines, sharing on, and the settings
        recommended for this data. It does not start training; the page fills itself in and the user presses Start."""
        names = [n for n in (body.get("data") or []) if isinstance(n, str)]
        chars, chat = None, False
        for n in names:
            try:
                c = self.check(n)
                chars = (chars or 0) + c["chars"]
                chat = chat or c["chat"]
            except TrainError:
                pass
        hours = _clamp_float(body.get("hours"), 0.05, 24 * 30, 1.0)
        machines = self.machines(both=True)
        if not any(m["usable"] for m in machines):
            why = "; ".join("%s: %s" % (m["name"], m["reason"]) for m in machines if not m["usable"]) or "no machines found"
            raise TrainError("No machine can train right now (%s)." % why, 409)
        for m in machines:   # warm every real machine so sharing actually engages; the CPU half comes up with it
            if m["usable"] and not m.get("variant"):
                self.pool.ensure(m["name"], m.get("_node"))
        self.measure_links(machines)
        machines = self.machines(both=True)   # re-read: worker states, speeds and links may have moved
        links = self.pool.links or None
        profile = {"chat": chat, "unique_fraction": 1.0, "example_chars": 0} if chars else None
        plan = self.plan(machines, links=links, model_bytes=0)
        hw = self.plan_hardware(machines, plan)
        rec = recommend(dict(hw, chat=chat), chars, hours, profile)
        plan = self.plan(machines, links=links, model_bytes=4 * (rec.get("parameters") or 0))
        hw = self.plan_hardware(machines, plan)
        rec = recommend(dict(hw, chat=chat), chars, hours, profile)
        for m in machines:
            lk = (links or {}).get(m["name"])
            if lk:
                m["link"] = {"rtt_ms": lk.get("rtt_ms"), "mbit": lk.get("mbit")}
        return {"both": True, "share": True, "together": "share", "machines": plan["share"], "plan": plan,
                "hardware": hw, "recommend": rec, "machine_list": [self.public(m) for m in machines], "mode": self.pool.mode}

    def plan_hardware(self, machines, plan):
        """What the recommender sizes the model for: the lead's hardware, the plan's combined speed, the smallest memory."""
        by = {m["name"]: m for m in machines}
        lead = by.get(plan["lead"])
        if not lead:
            return {"device": "cpu", "cores": 1, "arch": "", "ram_free": 2 * 1024 ** 3, "gpu": None, "speed": estimate_speed("cpu", 1), "budget": 256 * 1024 ** 2}
        return {"device": lead["device"], "cores": lead["cores"], "arch": lead["arch"], "ram_free": lead["ram_free"], "gpu": lead["gpu"],
                "speed": plan["speed"], "budget": plan["budget"], "machines": plan["share"]}

    @staticmethod
    def public(m):
        return {k: v for k, v in m.items() if not k.startswith("_")}
    # -- data
    def _data_path(self, name):
        if not isinstance(name, str) or not NAME_RE.match(name) or not name.lower().endswith(DATA_EXT) or ".." in name:
            raise TrainError("Use a file name made of letters, numbers, spaces, dots, dashes or underscores, ending in .txt, .md, .jsonl or .json.")
        return os.path.join(self.data_dir, name)

    def datasets(self):
        out = []
        for name in sorted(os.listdir(self.data_dir)):
            full = os.path.join(self.data_dir, name)
            if name.startswith(".") or not os.path.isfile(full):
                continue
            st = os.stat(full)
            out.append({"name": name, "size": st.st_size, "modified": st.st_mtime})
        return out

    def _data_total(self):
        return sum(d["size"] for d in self.datasets())

    def upload(self, body):
        """One ordered piece of an upload (base64 in JSON); the last one ("done") moves it into place."""
        uid = str(body.get("id") or "")
        if not re.match(r"^[a-z0-9]{6,40}$", uid):
            raise TrainError("Bad upload.")
        path = self._data_path(body.get("name"))
        try:
            chunk = base64.b64decode(body.get("data") or "", validate=True)
        except (binascii.Error, ValueError):
            raise TrainError("Bad upload data.")
        if len(chunk) > UPLOAD_CHUNK_MAX:
            raise TrainError("Upload piece too large.", 413)
        part = os.path.join(self.data_dir, ".part-" + uid)
        with self.lock:
            index = int(body.get("index") or 0)
            expected = self.uploads.get(uid, 0)
            if index != expected:
                raise TrainError("The upload got out of step. Try again.", 409)
            if index == 0 and os.path.exists(part):
                os.remove(part)
            if self._data_total() + (os.path.getsize(part) if os.path.exists(part) else 0) + len(chunk) > DATA_TOTAL_MAX:
                self._discard(part)
                self.uploads.pop(uid, None)
                raise TrainError("Your training data is full (20 GB). Delete some first.", 413)
            with open(part, "ab") as f:
                f.write(chunk)
            self.uploads[uid] = index + 1
            if body.get("done"):
                self.uploads.pop(uid, None)
                os.replace(part, path)
                return {"received": os.path.getsize(path), "done": True, "name": os.path.basename(path)}
            return {"received": os.path.getsize(part)}

    @staticmethod
    def _discard(path):
        try:
            os.remove(path)
        except OSError:
            pass

    def paste(self, body):
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise TrainError("Paste some text first.")
        data = text.encode("utf-8")
        if len(data) > PASTE_MAX:
            raise TrainError("That's more than 8 MB: upload it as a file instead.", 413)
        path = self._data_path(body.get("name"))
        with open(path, "wb") as f:
            f.write(data)
        return {"name": os.path.basename(path), "size": len(data)}

    def delete_data(self, body):
        path = self._data_path(body.get("name"))
        if not os.path.isfile(path):
            raise TrainError("That file isn't there any more.", 404)
        os.remove(path)
        return {}

    def check(self, name):
        """Read a data file the way the trainer will and report what it found (big files from a sample)."""
        path = self._data_path(name)
        if not os.path.isfile(path):
            raise TrainError("That file isn't there any more.", 404)
        st = os.stat(path)
        key = (name, st.st_size, st.st_mtime)
        if key in self.check_cache:
            return self.check_cache[key]
        training_data = _jobs_module("training_data")
        with open(path, "rb") as f:
            data = f.read(CHECK_SAMPLE)
        sampled = st.st_size > len(data)
        if sampled:
            data = data[:data.rfind(b"\n") + 1] or data
        raw = data.decode("utf-8", errors="replace")
        text, report = training_data.normalize(raw)
        scale = st.st_size / max(1, len(data)) if sampled else 1.0
        if sampled and report["format"] == "json":
            report["problems"].insert(0, "This JSON list is too big to check here (only the first 16 MB was read); the trainer reads all of it.")
        chars, words = int(len(text) * scale), int(len(text.split()) * scale)
        for k in ("records", "skipped"):
            report[k] = int(report[k] * scale)
        advice = []
        if sampled:
            advice.append(["ok", "Big file: these numbers are estimated from its first 16 MB."])
        if chars < 1000:
            advice.append(["bad", "Under 1,000 characters: the trainer won't start. Add much more text."])
        elif chars < 1_000_000:
            advice.append(["warn", "Under 1 MB: the model will mostly memorise this text. 10 MB or more gives it something to generalise from."])
        elif chars < 10_000_000:
            advice.append(["ok", "A good size for a small model (a few million weights)."])
        else:
            advice.append(["good", "Plenty of text for a model of tens of millions of weights."])
        if report["skipped"]:
            advice.append(["warn", "%s record(s) were skipped: see the problems list." % _n(report["skipped"])])
        if report["format"] in ("json", "jsonl") and not report["chat"]:
            advice.append(["ok", "No conversations found: the model will learn to continue text, not to reply."])
        if report["chat"]:
            advice.append(["good", "Conversations found: tick “learn only the replies” so the model learns to answer, not to ask."])
        # What the data is like, for the recommender: how much is repeated, and how long one example usually is.
        seen, total_chars, unique_chars = set(), 0, 0
        for line in text.splitlines():
            body = line.split(":", 1)[1].strip() if line.startswith(("User:", "Assistant:", "System:")) else line.strip()
            if not body:
                continue
            total_chars += len(body)
            key = hash(body)
            if key not in seen:
                seen.add(key)
                unique_chars += len(body)
        blocks = sorted(len(b) for b in text.split("\n\n") if b.strip())
        example_chars = blocks[len(blocks) // 2] if blocks else 0
        unique_fraction = unique_chars / total_chars if total_chars else 1.0
        if unique_fraction < 0.9:
            advice.append(["warn", "Only %d%% of the lines are different: the rest are repeats. Repeats don't teach anything new, so the model is sized for the unique text." % round(unique_fraction * 100)])
        examples = []   # the first records before and after conversion, so you can see what happens to each field
        if report["format"] == "jsonl":
            for line in raw.splitlines():
                if line.strip() and len(examples) < 3:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    examples.append({"raw": line[:600], "text": training_data.record_to_text(rec)})
        elif report["format"] == "json" and not sampled:
            try:
                items = json.loads(raw)
                for rec in (items if isinstance(items, list) else [items])[:3]:
                    examples.append({"raw": json.dumps(rec, ensure_ascii=False)[:600], "text": training_data.record_to_text(rec)})
            except ValueError:
                pass
        out = dict(report, name=name, size=st.st_size, chars=chars, words=words, tokens_est=int(chars / CHARS_PER_TOKEN),
                   preview=text[:2500], advice=advice, examples=examples, sampled=sampled,
                   unique_fraction=round(unique_fraction, 4), example_chars=example_chars)
        if len(self.check_cache) > 16:
            self.check_cache.clear()
        self.check_cache[key] = out
        return out

    def convert(self, body):
        """Save the trained-on text of a file as <name>.train.txt: exactly what the model reads, nothing else."""
        src = self._data_path(body.get("name"))
        if not os.path.isfile(src):
            raise TrainError("That file isn't there any more.", 404)
        stem = re.sub(r"\.(txt|md|jsonl|json)$", "", os.path.basename(src), flags=re.I)
        if stem.endswith(".train"):
            raise TrainError("That file is already in the training format.")
        dst = self._data_path((stem[:68] + ".train.txt"))
        if os.path.getsize(src) * 1.2 > DATA_TOTAL_MAX - self._data_total():
            raise TrainError("Not enough room in your training data (20 GB): delete some files first.", 413)
        try:
            rep = trainsets.convert_file(src, dst)
        except (OSError, ValueError) as e:
            raise TrainError("Couldn't convert it: %s" % e)
        return dict(rep, name=os.path.basename(dst), size=os.path.getsize(dst))

    def samples(self, names, count=20, held_out=False):
        """Real exchanges from the ticked files to test the model with: (what the person said, the reply in the data).
        Plain text gives (the start of a line, how it goes on). held_out: from the last 5%, which the trainer never
        trains on, so passing means it learned the patterns rather than these lines."""
        import random
        training_data = _jobs_module("training_data")
        pairs, chat = [], False
        for name in names[:10]:
            path = self._data_path(name)
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as f:
                raw = f.read(CHECK_SAMPLE)
            text, rep = training_data.normalize(raw.decode("utf-8", errors="replace"))
            blocks = [b for b in text.split("\n\n") if b.strip()]
            cut = int(len(blocks) * 0.95)
            blocks = blocks[cut:] if held_out else blocks[:cut]
            for b in blocks:
                lines = b.split("\n")
                for a, c in zip(lines, lines[1:]):
                    if a.startswith("User:") and c.startswith("Assistant:"):
                        q, ans = a[5:].strip(), c[10:].strip()
                        if q and ans and len(q) <= 600 and len(ans) <= 400:
                            pairs.append({"prompt": q, "answer": ans})
                            chat = True
                if not rep["chat"]:
                    for line in lines:
                        w = line.split()
                        if len(w) >= 12:
                            pairs.append({"prompt": " ".join(w[:8]), "answer": " ".join(w[8:20])})
        if not pairs:
            raise TrainError("No exchanges found in the ticked files to test with.", 404)
        seen, unique = set(), []
        for p in pairs:   # a pairs file repeats every line: test each prompt once
            if p["prompt"] not in seen:
                seen.add(p["prompt"])
                unique.append(p)
        picked = random.sample(unique, min(max(1, count), len(unique)))
        return {"samples": picked, "chat": chat, "held_out": held_out, "from": len(unique)}

    def speakers(self, name):
        path = self._data_path(name)
        if not os.path.isfile(path):
            raise TrainError("That file isn't there any more.", 404)
        return {"speakers": trainsets.speakers(path)}

    def multiparty(self, body):
        """A script or group chat → chat pairs where one chosen person is the Assistant."""
        src = self._data_path(body.get("name"))
        if not os.path.isfile(src):
            raise TrainError("That file isn't there any more.", 404)
        speaker = str(body.get("speaker") or "")
        if speaker not in {s["name"] for s in trainsets.speakers(src)}:
            raise TrainError("Pick one of the speakers found in the file.")
        context = max(1, min(8, int(body.get("context") or 3)))
        stem = re.sub(r"\.(txt|md|jsonl|json)$", "", os.path.basename(src), flags=re.I)[:40]
        dst = self._data_path("%s-as-%s.jsonl" % (stem, re.sub(r"[^A-Za-z0-9_-]+", "-", speaker).strip("-")[:30] or "speaker"))
        if os.path.getsize(src) * 2 > DATA_TOTAL_MAX - self._data_total():
            raise TrainError("Not enough room in your training data (20 GB): delete some files first.", 413)
        rep = trainsets.multiparty_to_pairs(src, dst, speaker, context, bool(body.get("names", True)))
        if not rep["examples"]:
            os.remove(dst)
            raise TrainError("%s never speaks after someone else, so there's nothing to learn from." % speaker)
        return dict(rep, name=os.path.basename(dst), size=os.path.getsize(dst))

    # -- saved settings
    PRESET_KEYS = {"vocab_size": int, "context": int, "dim": int, "layers": int, "heads": int, "batch_size": int, "steps": int,
                   "learning_rate": float, "warmup": int, "weight_decay": float, "eval_every": int, "save_every": int,
                   "dropout": float, "assistant_only": bool, "device": str}

    def presets(self):
        return sorted(self._load_json("presets.json").get("presets", []), key=lambda p: p["name"].lower())

    def save_preset(self, body):
        name = str(body.get("name") or "").strip()
        if not re.match(r"^[\w .'()+-]{1,40}$", name):
            raise TrainError("Name it with up to 40 letters, numbers, spaces or - _ . ( ) +.")
        given = body.get("settings") if isinstance(body.get("settings"), dict) else {}
        settings = {}
        for k, cast in self.PRESET_KEYS.items():
            if k in given:
                try:
                    settings[k] = cast(given[k])
                except (TypeError, ValueError):
                    raise TrainError("%s must be a number." % k)
        if settings.get("device") not in (None, "auto", "cpu", "cuda", "mps"):
            raise TrainError("Unknown device.")
        with self.lock:
            kept = [p for p in self.presets() if p["name"] != name]
            if len(kept) >= 50:
                raise TrainError("That's 50 saved settings already: delete one first.")
            kept.append({"name": name, "settings": settings, "saved_at": time.time(), "note": str(body.get("note") or "")[:200]})
            self._write_json("presets.json", {"presets": kept})
        return {"presets": self.presets()}

    def delete_preset(self, body):
        with self.lock:
            kept = [p for p in self.presets() if p["name"] != body.get("name")]
            self._write_json("presets.json", {"presets": kept})
        return {"presets": self.presets()}

    def _write_json(self, name, obj):
        tmp = os.path.join(self.root, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        os.replace(tmp, os.path.join(self.root, name))

    def fetch(self, body):
        try:
            return {"job": self.fetcher.start(str(body.get("id") or ""))}
        except ValueError as e:
            raise TrainError(str(e), 404)
        except OverflowError as e:
            raise TrainError(str(e), 413)

    # -- models
    def _model_dir(self, name):
        if not isinstance(name, str) or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,59}$", name):
            raise TrainError("Model names are letters, numbers, dots, dashes and underscores.")
        return os.path.join(self.models_dir, name)

    def models(self):
        out = []
        for name in sorted(os.listdir(self.models_dir)):
            cfg = os.path.join(self.models_dir, name, "model.json")
            if not os.path.isfile(cfg):
                continue
            try:
                with open(cfg, "r", encoding="utf-8") as f:
                    info = json.load(f)
            except (OSError, ValueError):
                continue
            size = sum(os.path.getsize(os.path.join(self.models_dir, name, x)) for x in os.listdir(os.path.join(self.models_dir, name)))
            out.append({"name": name, "size": size, **{k: info.get(k) for k in (
                "parameters", "vocabulary", "layers", "width", "heads", "context", "trained_steps", "validation_loss",
                "assistant_only", "data_files", "saved_at", "compute_device", "learning_rate", "batch_size")}})
        return out

    def delete_model(self, body):
        path = self._model_dir(body.get("name"))
        if self.running() and self.job.get("model") == body.get("name"):
            raise TrainError("That model is training. Stop it first.", 409)
        if not os.path.isdir(path):
            raise TrainError("That model isn't there any more.", 404)
        shutil.rmtree(path)
        return {}

    # -- the training job
    def _load_job(self):
        return self._load_json("job.json")

    def _save_job(self):
        tmp = os.path.join(self.root, "job.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.job, f)
        os.replace(tmp, os.path.join(self.root, "job.json"))

    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, body):
        with self.lock:
            if self.running():
                raise TrainError("A model is already training. Stop it first.", 409)
            files = body.get("data") or []
            if not isinstance(files, list) or not files:
                raise TrainError("Choose at least one training file.")
            for name in files:
                if not os.path.isfile(self._data_path(name)):
                    raise TrainError("%s isn't there any more." % name, 404)
            resume = bool(body.get("resume"))
            name = body.get("model") or time.strftime("model-%Y%m%d-%H%M%S")
            mdir = self._model_dir(name)
            if resume and not os.path.isfile(os.path.join(mdir, "model.json")):
                raise TrainError("There's no saved model called %s to continue." % name, 404)
            if not resume and os.path.exists(os.path.join(mdir, "model.json")):
                raise TrainError("A model called %s already exists: pick another name, or continue it." % name, 409)
            base_steps = 0
            if resume:
                try:
                    with open(os.path.join(mdir, "model.json"), "r", encoding="utf-8") as f:
                        base_steps = int(json.load(f).get("trained_steps") or 0)
                except (OSError, ValueError):
                    pass
            s = body.get("settings") or {}

            def num(key, lo, hi, cast=int, default=None):
                v = s.get(key, default)
                try:
                    v = cast(v)
                except (TypeError, ValueError):
                    raise TrainError("%s must be a number." % key)
                if not (lo <= v <= hi):
                    raise TrainError("%s must be between %s and %s." % (key, lo, hi))
                return v

            dim, heads = num("dim", 8, 4096), num("heads", 1, 64)
            if dim % heads:
                raise TrainError("The width (%d) must divide evenly by the number of heads (%d)." % (dim, heads))
            steps, warmup = num("steps", 1, 10_000_000), num("warmup", 0, 1_000_000, int, 100)
            # Options every machine gets; steps, warm-up, checks and saves are added per run.
            options = ["--batch-size", str(num("batch_size", 1, 4096)), "--context", str(num("context", 8, 8192)),
                       "--dim", str(dim), "--heads", str(heads), "--layers", str(num("layers", 1, 128)),
                       "--vocab-size", str(num("vocab_size", 256, 100_000)),
                       "--learning-rate", repr(num("learning_rate", 1e-7, 1.0, float)),
                       "--weight-decay", repr(num("weight_decay", 0.0, 1.0, float, 0.1)),
                       "--dropout", repr(num("dropout", 0.0, 0.5, float, 0.0)),
                       "--device", s.get("device") if s.get("device") in ("auto", "cpu", "cuda", "mps") else "auto"]
            if s.get("assistant_only"):
                options.append("--assistant-only")
            eval_every, save_every = num("eval_every", 1, 1_000_000, int, 250), num("save_every", 0, 1_000_000, int, 500)
            quality = None
            q = body.get("quality")
            if isinstance(q, dict) and q.get("on"):
                prompts = [{"prompt": str(p.get("prompt") or "")[:1000], "expect": [str(e)[:80] for e in (p.get("expect") or [])][:10],
                            "answer": str(p.get("answer") or "")[:1000]}
                           for p in (q.get("prompts") or [])[:100] if isinstance(p, dict) and str(p.get("prompt") or "").strip()]
                target = q.get("target_loss")
                try:
                    target = float(target) if target not in (None, "") else None
                except (TypeError, ValueError):
                    raise TrainError("The target loss must be a number.")
                if not any(p["expect"] or p["answer"] for p in prompts) and target is None:
                    raise TrainError("To stop when it's good enough, give at least one test prompt an expected word or answer, or a target loss.")
                quality = json.dumps({"prompts": prompts, "pass": _clamp_float(q.get("pass"), 0.05, 1.0, 1.0), "target_loss": target,
                                      "give_up": max(0, min(100, int(q.get("give_up") or 10))),
                                      "match": "close" if q.get("match") == "close" else "exact"})
            chosen = body.get("machines") if isinstance(body.get("machines"), list) and body.get("machines") else None
            both = bool(body.get("both"))
            model_bytes = 4 * count_parameters(int(_opt(options, "--vocab-size")), int(_opt(options, "--context")),
                                               int(_opt(options, "--dim")), int(_opt(options, "--layers")))
            machines = self.machines(both=both)
            plan = self.plan(machines, chosen, links=self.pool.links or None, model_bytes=model_bytes)
            if not plan["lead"]:
                raise TrainError(plan["notes"][0], 409)
            share = plan["share"] if body.get("share", True) else plan["share"][:1]
            log_path = os.path.join(self.logs_dir, "%s-%d.log" % (name, int(time.time())))
            open(log_path, "a").close()
            self.job = {"model": name, "log": log_path, "started": time.time(), "resume": resume, "data": files, "settings": s,
                        "ended": None, "code": None, "machines": share, "lead": share[0], "mode": ("race" if body.get("together") in ("race", "parallel") else "shared") if len(share) > 1 else "single",
                        "options": options, "steps": steps, "base_steps": base_steps, "quality": quality, "warmup": warmup, "eval_every": eval_every, "save_every": save_every,
                        "speeds": {m["name"]: m["speed"] for m in machines if m["name"] in share},
                        "round": 0, "rounds": 0, "phase": "starting", "stop": False, "wjob": None}
            self._save_job()
            self.thread = threading.Thread(target=self._orchestrate, daemon=True)
            self.thread.start()
            self._prune_logs()
            return {"model": name, "machines": share}

    def _prune_logs(self):
        logs = sorted((os.path.join(self.logs_dir, x) for x in os.listdir(self.logs_dir)), key=os.path.getmtime)
        for old in logs[:-20]:
            self._discard(old)

    def stop(self, body=None):
        if not self.running():
            raise TrainError("Nothing is training.", 409)
        self.job["stop"] = True   # the job's loop asks each machine to finish its step and save
        self._save_job()
        return {"stopping": True}

    def status(self, since=0):
        """The job, its new log lines since line `since`, and every loss point (parsed from the log file)."""
        with self.parse_lock:
            return self._status(since)

    def _status(self, since):
        job = {k: v for k, v in self.job.items() if k not in ("options", "speeds", "quality")}
        job["until_good"] = bool(self.job.get("quality"))
        running = self.running()
        p = self.parsed
        path = job.get("log")
        if path != p["path"]:
            self.parsed = p = {"path": path, "offset": 0, "train": [], "val": [], "grad": [], "lines": [], "last": {}, "quality": [], "verdict": None}
        if path and os.path.isfile(path):
            with open(path, "rb") as f:
                f.seek(p["offset"])
                data = f.read(4 * 1024 * 1024)
            cut = data.rfind(b"\n") + 1
            p["offset"] += cut
            for line in data[:cut].decode("utf-8", errors="replace").splitlines():
                p["lines"].append(line)
                m = STEP_RE.match(line)
                if m:
                    pt = {"step": int(m.group(6).replace(",", "")), "loss": _f(m.group(3)), "lr": _f(m.group(7)),
                          "tps": int(m.group(5).replace(",", "")), "of": int(m.group(2)), "local": int(m.group(1)), "sps": _f(m.group(4))}
                    p["train"].append(pt)
                    p["last"] = pt
                    g = STEP_GRAD_RE.search(line)
                    if g:
                        p["grad"].append({"step": pt["step"], "local": pt["local"], "norm": _f(g.group(1))})
                        p["step_grads"] = True
                    continue
                m = VAL_RE.match(line)
                if m:
                    p["val"].append({"step": int(m.group(3).replace(",", "")), "loss": _f(m.group(2))})
                    continue
                if line.startswith("QUALITY "):
                    try:
                        p["quality"].append(json.loads(line[8:]))
                        del p["quality"][:-100]
                    except ValueError:
                        pass
                    continue
                if line.startswith(("Good enough at", "Stopped improving")):
                    p["verdict"] = line
                m = GRAD_RE.match(line)
                if m and not p.get("step_grads"):
                    p["grad"].append({"local": int(m.group(1)), "norm": _f(m.group(2))})
            if len(p["lines"]) > 5000:
                drop = len(p["lines"]) - 4000
                p["lines"] = p["lines"][drop:]
                p["dropped"] = p.get("dropped", 0) + drop
        dropped = p.get("dropped", 0)
        start = max(0, int(since) - dropped)
        return {"job": job, "running": running, "lines": p["lines"][start:], "next": dropped + len(p["lines"]),
                "train": _thin(p["train"], 1500), "val": p["val"], "grad": _thin(p["grad"], 1500), "last": p["last"],
                "quality": [{"step": x["step"], "score": x["score"], "loss": x.get("loss")} for x in p["quality"]],
                "quality_last": p["quality"][-1] if p["quality"] else None, "verdict": p["verdict"]}

    # -- running a job on the workers
    def _orchestrate(self, reattach=False):
        job = self.job
        log = open(job["log"], "a", encoding="utf-8", buffering=1)

        def say(line):
            log.write(line.rstrip("\n") + "\n")

        code = 1
        self.pool.busy.update(job["machines"])
        try:
            if job["mode"] == "single":
                code = self._run_single(job, say, reattach)
            elif job["mode"] == "race":
                code = self._run_race(job, say, reattach)
            else:
                code = self._run_shared(job, say, reattach)
        except (trainpool.PoolError, TrainError) as e:
            say("ERROR: %s" % e)
        except Exception as e:   # noqa: BLE001 - written to the job's log
            say("ERROR: %s: %s" % (type(e).__name__, e))
        finally:
            self.pool.busy.difference_update(job["machines"])
            with self.lock:
                job.update(ended=time.time(), code=code, phase="done")
                if self.job is job:
                    self._save_job()
            log.close()

    def _machine_node(self, name):
        base, _ = trainpool.split_variant(name)   # "<node> · CPU" trains on the same node as "<node>"
        for m in self.machines():
            if m["name"] == base:
                return m.get("_node")
        return None

    def _remote(self, name, say, wait=3600):
        say("Getting %s ready (it installs PyTorch the first time; this can take a few minutes)..." % name)
        return self.pool.ready(name, self._machine_node(name), wait=wait, say=lambda l: say("[%s] %s" % (name, l)))

    def _sync_data(self, r, files, say):
        have = r.ls("data")
        for name in files:
            local = os.path.join(self.data_dir, name)
            v = trainpool.file_version(local)
            if have.get(name, {}).get("version") != v:
                say("Sending %s (%s) to %s..." % (name, _mb(os.path.getsize(local)), r.name))
                r.put_file("data/" + name, local, v)

    def _push_model(self, r, model, files=MODEL_FILES):
        mdir = self._model_dir(model)
        have = r.ls("models/" + model)
        for f in files:
            local = os.path.join(mdir, f)
            if os.path.isfile(local):
                v = trainpool.file_version(local)
                if have.get(f, {}).get("version") != v:
                    r.put_file("models/%s/%s" % (model, f), local, v)

    def _pull_model(self, r, model):
        mdir = self._model_dir(model)
        os.makedirs(mdir, exist_ok=True)
        have = r.ls("models/" + model)
        if "model.json" not in have:
            return False
        for f in MODEL_FILES:   # model.json last, so the model list never shows a half-copied model
            if f in have and f != "model.json":
                r.get_file("models/%s/%s" % (model, f), os.path.join(mdir, f), have[f]["version"])
        r.get_file("models/%s/model.json" % model, os.path.join(mdir, "model.json"), have["model.json"]["version"])
        return True

    def _tail(self, r, wj, say, prefix="", on_line=None):
        """Copy a worker's job log into this job's log until it ends; returns its exit code. Rides out short network drops."""
        misses = 0
        last_save = time.time()
        while True:
            if (self.job.get("stop") or wj.get("halt")) and not wj.get("stopped"):
                try:
                    r.call("POST", "/stop", {})
                    wj["stopped"] = True
                except trainpool.PoolError:
                    pass
            try:
                out = r.call("GET", "/log?id=%s&offset=%d" % (wj["id"], wj["offset"]), timeout=30)
                misses = 0
            except trainpool.PoolError as e:
                misses += 1
                if misses > 20:
                    raise trainpool.PoolError("Lost touch with %s for a minute: %s" % (r.name, e))
                time.sleep(3)
                continue
            if out.get("gone"):
                raise trainpool.PoolError("%s lost the training run (its worker restarted)." % r.name)
            for line in out["text"].splitlines():
                say(prefix + line)
                if on_line:
                    on_line(line)
            wj["offset"] = out["next"]
            if time.time() - last_save > 10:
                with self.lock:
                    self._save_job()
                last_save = time.time()
            if not out["running"] and not out["text"]:
                return out.get("code") if out.get("code") is not None else 1
            time.sleep(0.1 if out["text"] else 0.5)

    def _measure(self, name):
        """Reads a run's log lines for its speed; remembers it so the next plan uses the measured speed."""
        seen = {"params": None, "sps": None, "tps": None, "device": None}

        def on_line(line):
            m = STEP_RE.match(line)
            if m:
                seen["sps"], seen["tps"] = float(m.group(4)), int(m.group(5).replace(",", ""))
            elif line.startswith("Model initialized: "):
                seen["params"] = int(re.sub(r"[^\d]", "", line.split(" trainable")[0]))
            elif line.startswith("Device: "):
                seen["device"] = line.split()[1]
        return seen, on_line

    def _remember_speed(self, name, seen):
        if seen["params"] and seen["params"] >= 1_000_000 and seen["tps"] and seen["device"]:   # tiny models measure overhead, not speed
            dev = "cuda" if seen["device"].startswith("cuda") else seen["device"]
            self.speeds[name] = {"flops": 6.0 * seen["params"] * seen["tps"], "device": dev, "at": time.time()}
            try:
                with open(os.path.join(self.root, "speeds.json"), "w", encoding="utf-8") as f:
                    json.dump(self.speeds, f)
            except OSError:
                pass

    def _run_single(self, job, say, reattach):
        model = job["model"]
        name = job["lead"]
        r = self._remote(name, say)
        seen, measure = self._measure(name)
        if not (reattach and job.get("wjob")):
            job["phase"] = "sending data"
            self._sync_data(r, job["data"], say)
            if job["resume"]:
                self._push_model(r, model)
            else:
                self._delete_remote(r, model)
            if job["stop"]:
                return 0
            opts = job["options"] + ["--steps", str(job["steps"]), "--warmup", str(job["warmup"]),
                                     "--eval-every", str(job["eval_every"]), "--save-every", str(job["save_every"])]
            if job["resume"]:
                opts.append("--resume")
            if job.get("quality"):
                opts += ["--quality", job["quality"]]
            say("Training on %s." % name)
            wj = r.call("POST", "/train", {"model": model, "data": job["data"], "options": opts})
            job.update(wjob={"name": name, "id": wj["id"], "offset": 0}, phase="training")
            with self.lock:
                self._save_job()
        else:
            say("The dashboard restarted: following the run on %s again." % name)

        def on_line(line):
            measure(line)
            if line.startswith("Checkpoint saved"):
                try:
                    self._pull_model(r, model)
                except trainpool.PoolError as e:
                    say("(Couldn't copy the checkpoint here yet: %s)" % e)

        code = self._tail(r, job["wjob"], say, on_line=on_line)
        job["phase"] = "saving"
        if self._pull_model(r, model):
            say("Model copied back to the dashboard.")
        self._remember_speed(name, seen)
        return code

    def _delete_remote(self, r, model):
        try:
            r.call("POST", "/delete", {"path": "models/" + model})
        except trainpool.PoolError:
            pass

    def _up_all(self, job, say):
        """Bring every chosen machine's worker up and send it the data, in parallel. Drops the ones that fail."""
        names = list(job["machines"])
        remotes, errors = {}, {}

        def up(name):
            try:
                remotes[name] = self._remote(name, say)
                self._sync_data(remotes[name], job["data"], say)
            except trainpool.PoolError as e:
                errors[name] = str(e)

        threads = [threading.Thread(target=up, args=(n,)) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for name, err in errors.items():
            say("%s is left out: %s" % (name, err))
        active = [n for n in names if n in remotes]
        if not active:
            raise trainpool.PoolError("None of the machines could start.")
        if len(active) == 1:
            say("Only %s is ready, so it trains alone." % active[0])
            job.update(mode="single", lead=active[0], machines=active)
        return active, remotes

    def _run_race(self, job, say, reattach):
        """Race: every machine trains its own full copy (its own random seed). When the fastest finishes, the others
        stop and save their best checkpoint; the copy with the lowest validation loss is kept."""
        model = job["model"]
        if reattach:
            say("The dashboard restarted during the race: stopping the machines. Start again to finish it.")
            for name in job["machines"]:
                e = self.pool.entry(name)
                try:
                    (e["remote"] or self.pool.ready(name, self._machine_node(name), wait=120)).call("POST", "/stop", {})
                except trainpool.PoolError:
                    pass
            return 1
        active, remotes = self._up_all(job, say)
        if len(active) == 1:
            return self._run_single(job, say, False)
        job["lead"] = lead_name = active[0]
        say("Race: %s each train their own copy of %s, with different random seeds. When %s finishes, the others stop "
            "and the copy with the lowest validation loss is kept." % (" and ".join(active), model, lead_name))
        for n in active:
            if job["resume"]:
                self._push_model(remotes[n], model)
            else:
                self._delete_remote(remotes[n], model)
        tails = {n: {"id": None, "offset": 0} for n in active}
        results = {}

        def halt_others(reason):
            for n, wj in tails.items():
                if not wj.get("halt"):
                    wj["halt"] = reason

        def run(n, i):
            seen, measure = self._measure(n)

            def on_line(line):
                measure(line)
                if line.startswith("Good enough at"):
                    halt_others("%s is good enough" % n)
            try:
                opts = job["options"] + ["--steps", str(job["steps"]), "--warmup", str(job["warmup"]), "--eval-every", str(job["eval_every"]),
                                         "--save-every", str(job["save_every"]), "--seed", str(17 + 1000 * i)]
                if job["resume"]:
                    opts.append("--resume")
                if job.get("quality"):
                    opts += ["--quality", job["quality"]]
                tails[n]["id"] = remotes[n].call("POST", "/train", {"model": model, "data": job["data"], "options": opts})["id"]
                code = self._tail(remotes[n], tails[n], say, prefix="" if n == lead_name else "[%s] " % n, on_line=on_line)
                results[n] = {"code": code}
                self._remember_speed(n, seen)
            except trainpool.PoolError as e:
                say("[%s] %s" % (n, e))
                results[n] = {"code": 1}
            if n == lead_name:
                halt_others("%s finished" % lead_name)

        job["phase"] = "racing"
        threads = [threading.Thread(target=run, args=(n, i)) for i, n in enumerate(active)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Judge: each machine's saved model.json holds its best validation loss.
        job["phase"] = "judging"
        table = []
        for n in active:
            info = None
            try:
                tmp = os.path.join(self.root, ".race-%s.json" % re.sub(r"[^A-Za-z0-9-]", "-", n))
                remotes[n].get_file("models/%s/model.json" % model, tmp)
                with open(tmp, "r", encoding="utf-8") as f:
                    info = json.load(f)
                os.remove(tmp)
            except (trainpool.PoolError, OSError, ValueError):
                pass
            loss = (info or {}).get("validation_loss")
            table.append({"machine": n, "validation_loss": loss, "steps": (info or {}).get("trained_steps"), "code": results.get(n, {}).get("code")})
        ranked = sorted((t for t in table if t["validation_loss"] is not None), key=lambda t: t["validation_loss"])
        for t in table:
            say("Race result: %s — %s" % (t["machine"], ("validation %.4f after %s steps" % (t["validation_loss"], _n(t["steps"] or 0)))
                                                       if t["validation_loss"] is not None else "no saved model"))
        if not ranked:
            raise trainpool.PoolError("No machine finished with a saved model.")
        best = ranked[0]
        best["winner"] = True
        job["results"] = table
        self._pull_model(remotes[best["machine"]], model)
        say("Kept %s's copy: validation %.4f%s." % (best["machine"], best["validation_loss"],
            (" (%.4f better than the next)" % (ranked[1]["validation_loss"] - best["validation_loss"])) if len(ranked) > 1 else ""))
        return 0

    def _run_shared(self, job, say, reattach):
        model = job["model"]
        if reattach:
            say("The dashboard restarted during shared training. The last averaged model is kept; stopping the machines.")
            for name in job["machines"]:
                e = self.pool.entry(name)
                try:
                    (e["remote"] or self.pool.ready(name, self._machine_node(name), wait=120)).call("POST", "/stop", {})
                except trainpool.PoolError:
                    pass
            return 0
        active, remotes = self._up_all(job, say)
        if len(active) == 1:
            return self._run_single(job, say, False)
        job["lead"] = lead_name = active[0]
        lead = remotes[lead_name]
        for n in active[1:]:
            self._delete_remote(remotes[n], model)
        speeds = job["speeds"]
        sps = {}
        done = 0

        # Round 0: one machine warms the model up (and learns the tokenizer), so every copy starts from the same weights.
        if job["resume"]:
            self._push_model(lead, model)
            say("Continuing %s: every machine starts from the saved weights." % model)
        else:
            self._delete_remote(lead, model)
            w0 = max(10, min(job["warmup"] or 10, job["steps"] // 3))
            job["phase"] = "warm-up"
            say("Round 0: %s warms the model up alone for %d steps (the learning rate ramps from 0) and learns the tokenizer." % (lead_name, w0))
            seen, measure = self._measure(lead_name)
            opts = job["options"] + ["--steps", str(w0), "--warmup", str(w0), "--eval-every", str(w0), "--save-every", "0"]
            wj = lead.call("POST", "/train", {"model": model, "data": job["data"], "options": opts})
            if self._tail(lead, {"id": wj["id"], "offset": 0}, say, on_line=measure) != 0:
                raise trainpool.PoolError("The warm-up on %s failed (see above)." % lead_name)
            self._remember_speed(lead_name, seen)
            sps[lead_name] = seen["sps"]
            self._pull_model(lead, model)
            done = w0
        if job["stop"]:
            return 0
        sps.setdefault(lead_name, None)
        if not sps[lead_name]:
            sps[lead_name] = max(0.05, speeds.get(lead_name, 1e10) / max(1.0, 6.0 * count_parameters(
                int(_opt(job["options"], "--vocab-size")), int(_opt(job["options"], "--context")), int(_opt(job["options"], "--dim")),
                int(_opt(job["options"], "--layers"))) * int(_opt(job["options"], "--batch-size")) * int(_opt(job["options"], "--context"))))
        for n in active[1:]:
            sps[n] = sps[lead_name] * speeds.get(n, 1.0) / max(1.0, speeds.get(lead_name, 1.0))
        round_seconds = max(2.0, float(os.environ.get("NODEYARD_TRAIN_ROUND_SECONDS") or 120))
        per_round = max(10, int(sps[lead_name] * round_seconds))
        remaining = max(0, job["steps"] - done)
        rounds = max(1, math.ceil(remaining / per_round))
        job["rounds"] = rounds
        for n in active[1:]:
            for f in ("model.pt", "bpe.json", "model.json"):
                remotes[n].call("POST", "/pull", {"url": lead.url, "path": "models/%s/%s" % (model, f), "dest": "models/%s/%s" % (model, f)}, timeout=600)
        tokens_per_step = int(_opt(job["options"], "--batch-size")) * int(_opt(job["options"], "--context"))
        lead_done = 0
        for rnd in range(1, rounds + 1):
            if job["stop"] or len(active) == 0:
                break
            k = min(per_round, remaining - lead_done)
            if k <= 0:
                break
            plan = {n: max(5, int(round(k * sps[n] / sps[lead_name]))) for n in active}
            job.update(round=rnd, phase="training")
            with self.lock:
                self._save_job()
            say("Round %d of %d: %s." % (rnd, rounds, ", ".join("%s trains %d steps" % (n, plan[n]) for n in active)))
            results = {}

            def run(n, i):
                seen, measure = self._measure(n)
                try:
                    opts = job["options"] + ["--resume", "--steps", str(plan[n]), "--lr-progress", "%.6f" % ((rnd - 1) / rounds), "%.6f" % (rnd / rounds),
                                             "--seed", str(17 + 1000 * i + rnd), "--eval-every", str(plan[n]), "--save-every", "0"]
                    if n == lead_name and job.get("quality"):
                        opts += ["--quality", job["quality"]]

                    def verdict(line, measure=measure):
                        measure(line)
                        if line.startswith(("Good enough at", "Stopped improving")):
                            job["stop"] = True   # the averaged model this round makes is the last

                    wj = remotes[n].call("POST", "/train", {"model": model, "data": job["data"], "options": opts})
                    code = self._tail(remotes[n], {"id": wj["id"], "offset": 0}, say, prefix="" if n == lead_name else "[%s] " % n,
                                      on_line=verdict if n == lead_name else measure)
                    results[n] = (code, seen)
                except trainpool.PoolError as e:
                    say("[%s] %s" % (n, e))
                    results[n] = (1, seen)

            threads = [threading.Thread(target=run, args=(n, i)) for i, n in enumerate(active)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            if results.get(lead_name, (1,))[0] != 0:
                raise trainpool.PoolError("%s (the lead machine) failed in round %d; the last averaged model is kept." % (lead_name, rnd))
            for n in list(active):
                code, seen = results[n]
                if code != 0:
                    say("%s failed this round and is left out from now on." % n)
                    active.remove(n)
                    continue
                if seen["sps"]:
                    sps[n] = seen["sps"]
                self._remember_speed(n, seen)
            # Average: the lead fetches each copy straight from its machine, averages, and the others fetch the result.
            job["phase"] = "averaging"
            parts = [["model.pt", plan[lead_name] * tokens_per_step]]
            for n in active[1:]:
                peer = "peer-%s.pt" % re.sub(r"[^A-Za-z0-9-]", "-", n)
                lead.call("POST", "/pull", {"url": remotes[n].url, "path": "models/%s/model.pt" % model, "dest": "models/%s/%s" % (model, peer)}, timeout=600)
                parts.append([peer, plan[n] * tokens_per_step])
            if len(parts) > 1:
                out = lead.call("POST", "/run", {"cmd": "average", "model": model, "parts": parts, "timeout": 600}, timeout=660)
                for line in (out.get("out") or "").splitlines():
                    say(line)
                if out.get("code") != 0:
                    raise trainpool.PoolError("Averaging failed: %s" % ((out.get("err") or out.get("out") or "").strip().splitlines() or ["?"])[-1])
                for n in active[1:]:
                    remotes[n].call("POST", "/pull", {"url": lead.url, "path": "models/%s/model.pt" % model, "dest": "models/%s/model.pt" % model}, timeout=600)
            job["phase"] = "saving"
            self._pull_model(lead, model)
            lead_done += plan[lead_name]
        say("Shared training finished after %d round(s) on %s." % (job["round"], ", ".join(active)))
        return 0

    # -- using a model
    def _use_worker(self):
        """A machine whose worker can run the model now: the fastest one already up, else the plan's lead (started)."""
        machines = self.machines()
        ready = [m for m in machines if m["usable"] and m["worker"]["state"] == "ready"]
        pick = ready[0] if ready else next((m for m in machines if m["usable"]), None)
        if not pick:
            raise TrainError(self.plan(machines)["notes"][0], 503)
        try:
            return self.pool.ready(pick["name"], pick.get("_node"), wait=0)
        except trainpool.PoolError as e:
            raise TrainError(str(e), e.code)

    def _run(self, body, timeout=200):
        r = self._use_worker()
        model = body.get("model")
        try:
            if model and not (self.running() and self.job.get("model") == model and r.name in self.job.get("machines", [])):
                self._push_model(r, model, ("model.pt", "model.json", "bpe.json"))
            out = r.call("POST", "/run", dict(body, timeout=timeout), timeout=timeout + 30)
        except trainpool.PoolError as e:
            raise TrainError(str(e), e.code)
        if out.get("code") != 0:
            err = [l for l in ((out.get("out") or "") + (out.get("err") or "")).splitlines() if l.strip()]
            raise TrainError((err[-1] if err else "The trainer failed.").replace("ERROR: ", ""), 500)
        return out.get("out") or ""

    def chat(self, body):
        model = body.get("model")
        mdir = self._model_dir(model)
        if not os.path.isfile(os.path.join(mdir, "model.pt")):
            raise TrainError("That model hasn't been saved yet.", 404)
        prompt = str(body.get("prompt") or "")[:4000]
        mode = body.get("mode") == "continue"
        history = body.get("history") if isinstance(body.get("history"), list) else []
        if not mode:
            turns = []
            for turn in history[-6:]:
                if isinstance(turn, dict) and turn.get("role") in ("user", "assistant"):
                    turns.append("%s: %s" % ("User" if turn["role"] == "user" else "Assistant", str(turn.get("content", ""))[:2000]))
            turns.append("User: " + prompt)
            prompt = "\n".join(turns) + "\nAssistant:"
        options = ["--prompt", prompt,
                   "--max-tokens", str(max(8, min(512, int(body.get("max_tokens") or 160)))),
                   "--temperature", repr(max(0.05, min(2.0, float(body.get("temperature") or 0.7)))),
                   "--top-k", str(max(0, min(1000, int(body.get("top_k") or 40)))),
                   "--top-p", repr(max(0.05, min(1.0, float(body.get("top_p") or 0.9)))),
                   "--seed", str(int(time.time()) % 100000)]
        if not mode:
            options.append("--chat-mode")
        out = self._run({"cmd": "generate", "model": model, "options": options})
        if not mode:
            m = re.search(r"CHAT_REPLY_BEGIN\n(.*?)\nCHAT_REPLY_END", out, re.S)
            reply = (m.group(1) if m else out).strip()
        else:
            reply = out.split("Generated continuation:\n", 1)[-1].rstrip()
        return {"reply": reply, "prompt": prompt}

    def trace(self, body):
        """Every number of one forward pass (run on a worker), saved here so the page can expand any of them."""
        prompt = str(body.get("prompt") or "Hello there")[:300]
        tokens = max(1, min(16, int(body.get("tokens") or 8)))
        model = body.get("model") or None
        if model:
            mdir = self._model_dir(model)
            if not os.path.isfile(os.path.join(mdir, "model.pt")):
                model = None
        r = self._use_worker()
        try:
            if model and not (self.running() and self.job.get("model") == model and r.name in self.job.get("machines", [])):
                self._push_model(r, model, ("model.pt", "model.json", "bpe.json"))
            out = r.call("POST", "/run", {"cmd": "trace", "model": model, "options": ["--prompt", prompt, "--max-tokens", str(tokens)], "timeout": 300},
                         timeout=330)
            if out.get("code") != 0:
                err = [l for l in ((out.get("out") or "") + (out.get("err") or "")).splitlines() if l.strip()]
                raise TrainError((err[-1] if err else "The trace failed.").replace("ERROR: ", ""), 500)
            os.makedirs(os.path.join(self.root, "traces"), exist_ok=True)
            local = os.path.join(self.root, "traces", (model or "untrained-demo") + ".json")
            r.get_file("models/%s/trace.json" % (model or "untrained-demo"), local)
        except trainpool.PoolError as e:
            raise TrainError(str(e), e.code)
        with open(local, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["model"] = model
        return {"trace": data}

    def weights(self, q):
        """Rows of one weight matrix of a saved model, read straight from model.pt (no PyTorch needed)."""
        model = (q.get("model") or [""])[0]
        path = os.path.join(self._model_dir(model), "model.pt")
        if not os.path.isfile(path):
            raise TrainError("That model hasn't been saved yet.", 404)
        try:
            m = ptread.Model(path)
        except (OSError, ValueError, KeyError, StopIteration) as e:
            raise TrainError("Couldn't read the model file: %s" % e, 500)
        try:
            name = (q.get("name") or [""])[0]
            if not name:
                return {"tensors": m.shapes()}
            if name not in m.tensors:
                raise TrainError("No weights called %s." % name, 404)
            start = max(0, int((q.get("start") or ["0"])[0] or 0))
            count = max(1, min(256, int((q.get("count") or ["64"])[0] or 64)))
            return {"name": name, "shape": list(m.tensors[name].shape), "start": start, "rows": m.rows(name, start, count)}
        finally:
            m.close()

    def explain(self, body):
        options = ["--prompt", str(body.get("prompt") or "Hello there")[:500],
                   "--max-tokens", str(max(2, min(16, int(body.get("tokens") or 10))))]
        req = {"cmd": "explain", "options": options}
        if body.get("model"):
            mdir = self._model_dir(body["model"])
            if os.path.isfile(os.path.join(mdir, "model.pt")):
                req["model"] = body["model"]
        out = self._run(req, timeout=120)
        line = next((l for l in out.splitlines() if l.startswith("EXPLAIN_JSON ")), None)
        if not line:
            raise TrainError("The trainer didn't return the numbers.", 500)
        return {"explain": json.loads(line[len("EXPLAIN_JSON "):])}


def _f(x):
    try:
        return float(x)
    except ValueError:
        return None


def _thin(points, limit):
    """Keep at most `limit` points for the chart (every k-th, always the last)."""
    if len(points) <= limit:
        return points
    k = math.ceil(len(points) / limit)
    out = points[::k]
    if out[-1] is not points[-1]:
        out.append(points[-1])
    return out


def register(ctx, args):
    demo = bool(getattr(args, "demo", False))
    try:
        t = Trainer(getattr(args, "state_dir", "") or "/var/lib/nodeyard/dashboard", demo=demo, store=getattr(ctx, "store", None),
                    kubeconfig=getattr(args, "kubeconfig", None))
    except OSError as e:   # an unwritable state folder mustn't stop the dashboard
        msg = "Can't use the training folder: %s" % e
        ctx.get_routes["/api/train"] = lambda h, q: h._json({"ok": False, "error": msg}, 500)
        return
    ctx.trainer = t

    def wrap(fn):
        def handler(h, arg):
            try:
                return h._json(dict({"ok": True}, **(fn(arg) or {})))
            except TrainError as e:
                return h._json({"ok": False, "error": str(e)}, e.code)
            except trainpool.PoolError as e:
                return h._json({"ok": False, "error": str(e)}, e.code)
        return handler

    def overview(q):
        hours = _clamp_float((q.get("hours") or ["1"])[0], 0.05, 24 * 30, 1.0)
        chars = None
        names = [n for n in (q.get("data") or [""])[0].split("|") if n]
        chat = False
        unique_chars = example_weighted = 0.0
        for n in names:
            try:
                c = t.check(n)
                chars = (chars or 0) + c["chars"]
                chat = chat or c["chat"]
                unique_chars += c["chars"] * c.get("unique_fraction", 1.0)
                example_weighted += c["chars"] * c.get("example_chars", 0)
            except TrainError:
                pass
        profile = {"chat": chat, "unique_fraction": unique_chars / chars, "example_chars": example_weighted / chars} if chars else None
        both = (q.get("both") or ["0"])[0] == "1"
        machines = t.machines(both=both)
        chosen = [n for n in (q.get("machines") or [""])[0].split("|") if n] or None
        links = t.measure_links(machines) or None
        plan = t.plan(machines, chosen, links=links)
        hw = t.plan_hardware(machines, plan)
        rec = recommend(dict(hw, chat=chat), chars, hours, profile)
        # Judge latency against the real model size, then settle the plan and hardware once more.
        plan = t.plan(machines, chosen, links=links, model_bytes=4 * (rec.get("parameters") or 0))
        hw = t.plan_hardware(machines, plan)
        rec = recommend(dict(hw, chat=chat), chars, hours, profile)
        for m in machines:   # show each machine's latency in the table
            lk = (links or {}).get(m["name"])
            if lk:
                m["link"] = {"rtt_ms": lk.get("rtt_ms"), "mbit": lk.get("mbit")}
        if plan["lead"]:   # get the best machine ready (PyTorch installs itself) while the page is open
            lead = next(m for m in machines if m["name"] == plan["lead"])
            t.pool.ensure(lead["name"], lead.get("_node"))
        # What would get a bigger model: more time on these machines, or a faster machine (even one that's down).
        times = []
        for h in (0.25, 1, 3, 8, 24, 72):
            r = recommend(dict(hw, chat=chat), chars, h, profile)
            times.append({"hours": h, "parameters": r["parameters"], "limit": r["limit"], "seconds": r["seconds"]})
        faster = []
        for m in machines:
            if m["speed"] > 2 * (hw.get("speed") or 1) and m["name"] not in plan["share"]:
                mh = {"device": m["device"], "cores": m["cores"], "arch": m["arch"], "ram_free": m["ram_free"], "gpu": m["gpu"], "speed": m["speed"]}
                r = recommend(dict(mh, chat=chat), chars, hours, profile)
                faster.append({"name": m["name"], "gpu": (m["gpu"] or {}).get("name"), "speed": m["speed"], "usable": m["usable"], "reason": m["reason"],
                               "parameters": r["parameters"], "limit": r["limit"], "seconds": r["seconds"]})
        rec["bigger"] = {"times": times, "faster": faster[:3]}
        return {"machines": [t.public(m) for m in machines], "plan": plan, "mode": t.pool.mode, "hardware": hw, "recommend": rec,
                "datasets": t.datasets(), "catalog": t.fetcher.catalog(), "models": t.models(), "presets": t.presets(), "running": t.running(), "job": t.job, "hours": hours, "data_dir": t.data_dir}

    def warm(body):
        names = body.get("machines") if isinstance(body.get("machines"), list) else []
        by = {m["name"]: m for m in t.machines()}
        for n in names[:8]:
            if n in by and by[n]["usable"]:
                t.pool.ensure(n, by[n].get("_node"))
        return {}

    ctx.get_routes.update({
        "/api/train": wrap(overview),
        "/api/train/status": wrap(lambda q: t.status(int((q.get("since") or ["0"])[0] or 0))),
        "/api/train/check": wrap(lambda q: {"check": t.check((q.get("name") or [""])[0])}),
        "/api/train/weights": wrap(t.weights),
        "/api/train/datasets": wrap(lambda q: {"catalog": t.fetcher.catalog()}),
        "/api/train/speakers": wrap(lambda q: t.speakers((q.get("name") or [""])[0])),
        "/api/train/samples": wrap(lambda q: t.samples([n for n in (q.get("data") or [""])[0].split("|") if n],
                                                      max(1, min(100, int((q.get("count") or ["20"])[0] or 20))),
                                                      (q.get("held_out") or ["0"])[0] == "1")),
    })
    ctx.post_routes.update({
        "/api/train/upload": wrap(t.upload),
        "/api/train/paste": wrap(t.paste),
        "/api/train/data/delete": wrap(t.delete_data),
        "/api/train/model/delete": wrap(t.delete_model),
        "/api/train/start": wrap(t.start),
        "/api/train/autosetup": wrap(t.autosetup),
        "/api/train/stop": wrap(t.stop),
        "/api/train/chat": wrap(t.chat),
        "/api/train/explain": wrap(t.explain),
        "/api/train/trace": wrap(t.trace),
        "/api/train/warm": wrap(warm),
        "/api/train/datasets/get": wrap(t.fetch),
        "/api/train/convert": wrap(t.convert),
        "/api/train/multiparty": wrap(t.multiparty),
        "/api/train/presets/save": wrap(t.save_preset),
        "/api/train/presets/delete": wrap(t.delete_preset),
    })
    ctx.post_limits["/api/train/upload"] = UPLOAD_BODY_MAX
    ctx.post_limits["/api/train/paste"] = PASTE_MAX * 2 + 4096


def _jobs_module(name):
    sys.path.insert(0, JOBS_DIR)
    try:
        return __import__(name)
    finally:
        sys.path.remove(JOBS_DIR)


def _opt(options, flag):
    """The value after a flag in a trainer option list."""
    return options[options.index(flag) + 1]


def _clamp_float(x, lo, hi, default):
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return default
