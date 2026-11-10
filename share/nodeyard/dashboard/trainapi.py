"""Train AI: train your own language model from scratch, and learn exactly how it works.

The page (web/train.js) teaches the maths, checks the training data you give it, picks settings for this
machine's hardware (and shows the formula and numbers behind every one), runs the trainer, draws the loss as it
learns, lets you chat with the result, and shows the value of every variable in one real forward and backward pass.

The trainer is share/nodeyard/jobs/train_bpe_gpt_torch.py (the same portable PyTorch trainer `nodeyard jobs`
runs across the cluster), started as a child process of the dashboard. It needs PyTorch: "Set up the trainer"
makes a private virtual environment under <state_dir>/train/venv and installs torch and numpy into it.
NODEYARD_TRAIN_PYTHON picks another Python that already has them.

Everything lives under <state_dir>/train/: data/ (your uploaded files), models/<name>/ (one folder per model),
job.json and logs/ (the current or last run, so a dashboard restart picks a running job back up).
"""
import base64
import binascii
import collections
import json
import math
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(os.path.dirname(HERE), "jobs")
TRAINER = os.path.join(JOBS_DIR, "train_bpe_gpt_torch.py")

DATA_EXT = (".txt", ".md", ".jsonl", ".json")
UPLOAD_CHUNK_MAX = 4 * 1024 * 1024
UPLOAD_BODY_MAX = UPLOAD_CHUNK_MAX * 4 // 3 + 4096
DATA_TOTAL_MAX = 20 * 1024 * 1024 * 1024
PASTE_MAX = 8 * 1024 * 1024
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}$")
STEP_RE = re.compile(r"^Step (\d+)/(\d+) \| train loss ([-\d.eE+naif]+) \| ([\d.]+) steps/sec \| ([\d,]+) tokens/sec \| "
                     r"total training step ([\d,]+) \| learning rate ([-\d.eE+]+)")
VAL_RE = re.compile(r"^Validation at step (\d+): loss ([-\d.eE+naif]+) \| total training step ([\d,]+)")
GRAD_RE = re.compile(r"^Gradient norm at step (\d+): ([\d.eE+-]+)")
CHARS_PER_TOKEN = 3.6        # typical for a learned BPE vocabulary on English; the trainer prints the real figure

PROBE_CODE = r"""
import json, os
out = {"torch": None}
try:
    import numpy, torch
    out["torch"] = torch.__version__
    out["threads"] = torch.get_num_threads()
    out["cuda"] = bool(torch.cuda.is_available())
    if out["cuda"]:
        p = torch.cuda.get_device_properties(0)
        out["gpu"] = {"name": p.name, "vram": int(p.total_memory), "count": torch.cuda.device_count()}
    out["mps"] = bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())
except Exception as e:
    out["error"] = "%s: %s" % (type(e).__name__, e)
print(json.dumps(out))
"""


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


def recommend(hw, data_chars=None, hours=1.0):
    """Settings for this hardware and this much data, each with the formula, the numbers and why.

    hw: {"device": cuda|mps|cpu, "cores", "arch", "ram_free", "gpu": {"name", "vram"} or None}
    """
    device = hw.get("device") or "cpu"
    cores = hw.get("cores") or 1
    ram_free = hw.get("ram_free") or 2 * 1024 ** 3
    gpu = hw.get("gpu")
    why = []

    if device == "cuda":
        budget = int(0.7 * (gpu or {}).get("vram", 0))
        budget_text = "70%% of the GPU's %s of memory" % _gb((gpu or {}).get("vram", 0))
    else:
        budget = int(max(256 * 1024 ** 2, min(0.5 * ram_free, ram_free - 1024 ** 3)))
        budget_text = "half the free RAM (%s free), keeping 1 GB for everything else" % _gb(ram_free)
    speed = estimate_speed(device, cores, hw.get("arch", ""), gpu)
    seconds = max(60.0, float(hours) * 3600)
    compute = speed * seconds

    tokens = int((data_chars or 4_000_000) / CHARS_PER_TOKEN)
    if data_chars is None:
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
    n_data = tokens / 5
    context0 = 128
    n_memory = budget / 20      # 16 bytes per weight for weights+gradients+AdamW m and v, plus room for activations
    caps = [("compute", n_compute), ("data", n_data), ("memory", n_memory)]
    limit_name, target = min(caps, key=lambda c: c[1])
    target = max(50_000, min(target, 300_000_000))
    why.append({"name": "Model size N (weights)", "value": _n(target), "formula": "N = min(√(C ÷ 120), tokens ÷ 5, memory ÷ 20 bytes)",
                "numbers": "compute C = %s FLOP/s × %s s = %s → √(C÷120) = %s · data %s ÷ 5 = %s · memory %s ÷ 20 = %s → %s is the limit"
                           % (_sci(speed), _n(seconds), _sci(compute), _n(n_compute), _n(tokens), _n(n_data), _gb(budget), _n(n_memory), limit_name),
                "explain": "Compute: the Chinchilla rule says train on about 20 tokens per weight, and training costs 6 FLOPs per weight per token, so C = 6·N·20N = 120·N². Data: repeating text more than about 4 times stops helping, so N ≤ tokens·4 ÷ 20. Memory: float32 weights, gradients and AdamW's two moments are 16 bytes per weight. %s" % ("Speed here is a guess for this hardware (%s FLOP/s); the real tokens/sec shows once training starts." % _sci(speed))})

    context = 64 if target < 300_000 else 256 if target > 5_000_000 and device != "cpu" else context0
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
    why.append({"name": "Context length T", "value": str(context), "formula": "64 for tiny models · 128 normally · 256 for > 5M weights on a GPU",
                "numbers": "N = %s on %s → T = %d" % (_n(target), device, context),
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
    steps_data = 4 * tokens / (batch * context)
    steps = int(max(200, min(200_000, steps_time, steps_data)) // 50 * 50)
    epochs = steps * batch * context / max(1, tokens)
    why.append({"name": "Steps", "value": _n(steps), "formula": "min(C ÷ (6·N·B·T), 4 passes over the data)",
                "numbers": "%s ÷ (6·%s·%d·%d) = %s · 4·%s ÷ (%d·%d) = %s → %s steps ≈ %.1f passes over your text"
                           % (_sci(compute), _n(params), batch, context, _n(steps_time), _n(tokens), batch, context, _n(steps_data), _n(steps), epochs),
                "explain": "One step = read a batch, measure the loss, compute gradients, update every weight once."})

    lr = 1e-3 if params < 20e6 else 6e-4 if params < 100e6 else 3e-4
    warmup = int(max(20, min(2000, steps * 0.05)))
    why.append({"name": "Learning rate η", "value": "%g" % lr, "formula": "1e-3 under 20M weights · 6e-4 under 100M · 3e-4 above; warm up for 5% of steps, then cosine decay to 10%",
                "numbers": "N = %s → η = %g, warm-up %s steps" % (_n(params), lr, _n(warmup)),
                "explain": "How far each update moves the weights. Too high and the loss jumps or becomes NaN; too low and it barely moves. Big models need smaller steps."})
    eval_every = int(max(25, min(1000, steps // 20)))
    seconds_est = steps * per_step / speed
    total_mem = weights_bytes + batch * act_one
    expect = expectation(params, tokens, chat=bool(hw.get("chat")))
    return {
        "device": device, "speed": speed, "budget": budget, "budget_text": budget_text, "limit": limit_name,
        "settings": {"vocab_size": vocab, "context": context, "dim": width, "layers": layers, "heads": heads,
                     "batch_size": batch, "steps": steps, "learning_rate": lr, "warmup": warmup, "weight_decay": 0.1,
                     "eval_every": eval_every},
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


def node_speed(node):
    """(device, estimated FLOP/s, GPU name) for a cluster node, from what the dashboard already knows about it."""
    gpus = (((node.get("hw") or {}).get("hardware") or {}).get("gpus")) or []
    nvidia = [g for g in gpus if (g.get("vendor") or "").lower() == "0x10de" or "nvidia" in (g.get("name_full") or "").lower()]
    if nvidia:
        g = nvidia[0]
        return "cuda", estimate_speed("cuda", gpu={"vram": g.get("vram") or 4e9}), g.get("name_full") or "NVIDIA GPU"
    arch = node.get("arch", "")
    return "cpu", estimate_speed("cpu", node.get("cpu_cores") or 1, "aarch64" if arch.startswith("arm") else arch), ""


# -- the manager ---------------------------------------------------------------------------------------------

class Trainer:
    def __init__(self, state_dir, demo=False, store=None):
        self.root = os.path.join(state_dir, "train")
        self.data_dir = os.path.join(self.root, "data")
        self.models_dir = os.path.join(self.root, "models")
        self.logs_dir = os.path.join(self.root, "logs")
        self.venv = os.path.join(self.root, "venv")
        for d in (self.data_dir, self.models_dir, self.logs_dir):
            os.makedirs(d, exist_ok=True)
        self.demo = demo
        self.store = store
        self.lock = threading.Lock()
        self.proc = None
        self.job = self._load_job()
        self.parsed = {"path": None, "offset": 0, "train": [], "val": [], "grad": [], "lines": [], "last": {}}
        self.parse_lock = threading.Lock()
        self.probe_cache = (0.0, None)
        self.check_cache = {}
        self.setup_state = {"running": False, "log": [], "ok": None}
        self.uploads = {}

    # -- python with torch
    def python(self):
        env = os.environ.get("NODEYARD_TRAIN_PYTHON")
        if env:
            return env
        venv_py = os.path.join(self.venv, "bin", "python3")
        return venv_py if os.path.exists(venv_py) else sys.executable

    def probe(self, force=False):
        at, cached = self.probe_cache
        if cached and not force and time.time() - at < 300:
            return cached
        try:
            r = subprocess.run([self.python(), "-c", PROBE_CODE], capture_output=True, text=True, timeout=90)
            out = json.loads((r.stdout.strip().splitlines() or ["{}"])[-1])
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            out = {"torch": None, "error": str(e)}
        out["python"] = self.python()
        self.probe_cache = (time.time(), out)
        return out

    def hardware(self):
        p = self.probe()
        total, free = read_memory()
        device = "cuda" if p.get("cuda") else "mps" if p.get("mps") else "cpu"
        return {"cores": os.cpu_count() or 1, "arch": platform.machine(), "os": platform.system(), "host": platform.node(),
                "ram_total": total, "ram_free": free, "gpu": p.get("gpu"), "mps": bool(p.get("mps")), "device": device,
                "torch": p.get("torch"), "torch_error": p.get("error"), "python": p.get("python")}

    def cluster(self):
        """The cluster's machines ranked by estimated training speed, so the page can say which is best."""
        if self.store is None:
            return []
        try:
            nodes = ((self.store.snapshot() or {}).get("state") or {}).get("nodes") or []
        except Exception:
            return []
        out = []
        for n in nodes:
            device, speed, gpu = node_speed(n)
            out.append({"name": n.get("name"), "ready": bool(n.get("ready")), "arch": n.get("arch", ""), "cores": n.get("cpu_cores"),
                        "ram": n.get("mem_total"), "device": device, "gpu": gpu, "speed": speed})
        out.sort(key=lambda x: (not x["ready"], -x["speed"]))
        return out

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
        """Read a data file the way the trainer will and report what it found."""
        path = self._data_path(name)
        if not os.path.isfile(path):
            raise TrainError("That file isn't there any more.", 404)
        st = os.stat(path)
        key = (name, st.st_size, st.st_mtime)
        if key in self.check_cache:
            return self.check_cache[key]
        sys.path.insert(0, JOBS_DIR)
        try:
            import training_data
        finally:
            sys.path.remove(JOBS_DIR)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
        text, report = training_data.normalize(raw)
        chars = len(text)
        words = len(text.split())
        advice = []
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
        out = dict(report, name=name, size=st.st_size, chars=chars, words=words, tokens_est=int(chars / CHARS_PER_TOKEN),
                   preview=text[:1500], advice=advice)
        if len(self.check_cache) > 16:
            self.check_cache.clear()
        self.check_cache[key] = out
        return out

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
        try:
            with open(os.path.join(self.root, "job.json"), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_job(self):
        tmp = os.path.join(self.root, "job.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.job, f)
        os.replace(tmp, os.path.join(self.root, "job.json"))

    def running(self):
        if self.proc is not None:
            return self.proc.poll() is None
        pid = self.job.get("pid")
        if not pid or self.job.get("ended"):
            return False
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        # Only trust a pid if it is still our trainer (pids are reused).
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                return b"train_bpe_gpt_torch.py" in f.read()
        except OSError:
            return sys.platform == "darwin"

    def start(self, body):
        with self.lock:
            if self.running():
                raise TrainError("A model is already training. Stop it first.", 409)
            if not self.probe().get("torch"):
                raise TrainError("PyTorch isn't installed for the trainer yet: press “Set up the trainer” first.", 409)
            files = body.get("data") or []
            if not isinstance(files, list) or not files:
                raise TrainError("Choose at least one training file.")
            paths = []
            for name in files:
                p = self._data_path(name)
                if not os.path.isfile(p):
                    raise TrainError("%s isn't there any more." % name, 404)
                paths.append(p)
            resume = bool(body.get("resume"))
            name = body.get("model") or time.strftime("model-%Y%m%d-%H%M%S")
            mdir = self._model_dir(name)
            if resume and not os.path.isfile(os.path.join(mdir, "model.json")):
                raise TrainError("There's no saved model called %s to continue." % name, 404)
            if not resume and os.path.exists(os.path.join(mdir, "model.json")):
                raise TrainError("A model called %s already exists: pick another name, or continue it." % name, 409)
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
            args = [self.python(), "-u", TRAINER, "train", "--app-data", mdir,
                    "--steps", str(num("steps", 1, 10_000_000)), "--batch-size", str(num("batch_size", 1, 4096)),
                    "--context", str(num("context", 8, 8192)), "--dim", str(dim), "--heads", str(heads),
                    "--layers", str(num("layers", 1, 128)), "--vocab-size", str(num("vocab_size", 256, 100_000)),
                    "--learning-rate", repr(num("learning_rate", 1e-7, 1.0, float)),
                    "--weight-decay", repr(num("weight_decay", 0.0, 1.0, float, 0.1)),
                    "--warmup", str(num("warmup", 0, 1_000_000, int, 100)), "--eval-every", str(num("eval_every", 1, 1_000_000, int, 250)),
                    "--save-every", str(num("save_every", 0, 1_000_000, int, 500)), "--device", s.get("device") if s.get("device") in ("auto", "cpu", "cuda", "mps") else "auto"]
            for p in paths:
                args += ["--data-file", p]
            if s.get("assistant_only"):
                args.append("--assistant-only")
            if resume:
                args.append("--resume")
            os.makedirs(mdir, exist_ok=True)
            log_path = os.path.join(self.logs_dir, "%s-%d.log" % (name, int(time.time())))
            log = open(log_path, "ab")
            try:
                self.proc = subprocess.Popen(args, cwd=JOBS_DIR, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                             start_new_session=True)
            finally:
                log.close()
            self.job = {"pid": self.proc.pid, "model": name, "log": log_path, "started": time.time(), "resume": resume,
                        "data": files, "settings": s, "ended": None, "code": None}
            self._save_job()
            threading.Thread(target=self._wait, args=(self.proc,), daemon=True).start()
            self._prune_logs()
            return {"model": name}

    def _wait(self, proc):
        code = proc.wait()
        with self.lock:
            if self.job.get("pid") == proc.pid:
                self.job["ended"], self.job["code"] = time.time(), code
                self._save_job()

    def _prune_logs(self):
        logs = sorted((os.path.join(self.logs_dir, x) for x in os.listdir(self.logs_dir)), key=os.path.getmtime)
        for old in logs[:-20]:
            self._discard(old)

    def stop(self, body=None):
        if not self.running():
            raise TrainError("Nothing is training.", 409)
        try:
            os.kill(self.job["pid"], signal.SIGINT)   # the trainer finishes its step, saves, and exits
        except OSError as e:
            raise TrainError("Couldn't stop it: %s" % e, 500)
        return {"stopping": True}

    def status(self, since=0):
        """The job, its new log lines since line `since`, and every loss point (parsed from the log file)."""
        with self.parse_lock:
            return self._status(since)

    def _status(self, since):
        job = dict(self.job)
        running = self.running()
        if not running and job.get("pid") and not job.get("ended"):
            job["ended"] = time.time()
        p = self.parsed
        path = job.get("log")
        if path != p["path"]:
            self.parsed = p = {"path": path, "offset": 0, "train": [], "val": [], "grad": [], "lines": [], "last": {}}
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
                    continue
                m = VAL_RE.match(line)
                if m:
                    p["val"].append({"step": int(m.group(3).replace(",", "")), "loss": _f(m.group(2))})
                    continue
                m = GRAD_RE.match(line)
                if m:
                    p["grad"].append({"local": int(m.group(1)), "norm": _f(m.group(2))})
            if len(p["lines"]) > 5000:
                drop = len(p["lines"]) - 4000
                p["lines"] = p["lines"][drop:]
                p["dropped"] = p.get("dropped", 0) + drop
        dropped = p.get("dropped", 0)
        start = max(0, int(since) - dropped)
        return {"job": job, "running": running, "lines": p["lines"][start:], "next": dropped + len(p["lines"]),
                "train": _thin(p["train"], 1500), "val": p["val"], "grad": p["grad"][-200:], "last": p["last"]}

    # -- using a model
    def _run(self, args, timeout=180):
        try:
            r = subprocess.run([self.python(), "-u", TRAINER] + args, cwd=JOBS_DIR, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TrainError("The model took too long to answer.", 504)
        except OSError as e:
            raise TrainError("Couldn't run the trainer: %s" % e, 500)
        if r.returncode != 0:
            err = [l for l in (r.stdout + r.stderr).splitlines() if l.strip()]
            raise TrainError((err[-1] if err else "The trainer failed.").replace("ERROR: ", ""), 500)
        return r.stdout

    def chat(self, body):
        mdir = self._model_dir(body.get("model"))
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
        args = ["generate", "--app-data", mdir, "--prompt", prompt,
                "--max-tokens", str(max(8, min(512, int(body.get("max_tokens") or 160)))),
                "--temperature", repr(max(0.05, min(2.0, float(body.get("temperature") or 0.7)))),
                "--top-k", str(max(0, min(1000, int(body.get("top_k") or 40)))),
                "--top-p", repr(max(0.05, min(1.0, float(body.get("top_p") or 0.9)))), "--device", "cpu",
                "--seed", str(int(time.time()) % 100000)]
        if not mode:
            args.append("--chat-mode")
        out = self._run(args)
        if not mode:
            m = re.search(r"CHAT_REPLY_BEGIN\n(.*?)\nCHAT_REPLY_END", out, re.S)
            reply = (m.group(1) if m else out).strip()
        else:
            reply = out.split("Generated continuation:\n", 1)[-1].rstrip()
        return {"reply": reply, "prompt": prompt}

    def explain(self, body):
        args = ["explain", "--prompt", str(body.get("prompt") or "Hello there")[:500],
                "--max-tokens", str(max(2, min(16, int(body.get("tokens") or 10))))]
        if body.get("model"):
            mdir = self._model_dir(body["model"])
            if os.path.isfile(os.path.join(mdir, "model.pt")):
                args += ["--app-data", mdir]
        out = self._run(args, timeout=120)
        line = next((l for l in out.splitlines() if l.startswith("EXPLAIN_JSON ")), None)
        if not line:
            raise TrainError("The trainer didn't return the numbers.", 500)
        return {"explain": json.loads(line[len("EXPLAIN_JSON "):])}

    # -- installing torch
    def setup(self, body=None):
        with self.lock:
            if self.setup_state["running"]:
                raise TrainError("Setup is already running.", 409)
            self.setup_state = {"running": True, "log": [], "ok": None, "started": time.time()}
        threading.Thread(target=self._setup, daemon=True).start()
        return {}

    def _setup(self):
        st = self.setup_state

        def say(line):
            st["log"].append(line)
            del st["log"][:-400]

        def run(cmd):
            say("$ " + " ".join(cmd))
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in p.stdout:
                say(line.rstrip())
            return p.wait()

        try:
            py = os.path.join(self.venv, "bin", "python3")
            if not os.path.exists(py):
                if run([sys.executable, "-m", "venv", self.venv]) != 0:
                    say("Couldn't make the virtual environment. On Debian or Ubuntu run: sudo apt install python3-venv")
                    st["ok"] = False
                    return
            run([py, "-m", "pip", "install", "--upgrade", "pip"])
            cmd = [py, "-m", "pip", "install", "numpy", "torch"]
            if sys.platform.startswith("linux") and not shutil.which("nvidia-smi"):
                cmd += ["--index-url", "https://download.pytorch.org/whl/cpu", "--extra-index-url", "https://pypi.org/simple"]
                say("No NVIDIA GPU here: installing the smaller CPU-only PyTorch.")
            code = run(cmd)
            st["ok"] = code == 0
            say("Done: PyTorch is ready." if code == 0 else "pip failed (exit %d): see above." % code)
        except Exception as e:   # noqa: BLE001 - shown to the user
            say("Setup failed: %s" % e)
            st["ok"] = False
        finally:
            st["running"] = False
            self.probe_cache = (0.0, None)


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
        t = Trainer(getattr(args, "state_dir", "") or "/var/lib/nodeyard/dashboard", demo=demo, store=getattr(ctx, "store", None))
    except OSError as e:   # an unwritable state folder mustn't stop the dashboard
        ctx.get_routes["/api/train"] = lambda h, q: h._json({"ok": False, "error": "Can't use the training folder: %s" % e}, 500)
        return
    ctx.trainer = t

    def wrap(fn):
        def handler(h, arg):
            try:
                return h._json(dict({"ok": True}, **(fn(arg) or {})))
            except TrainError as e:
                return h._json({"ok": False, "error": str(e)}, e.code)
        return handler

    def overview(q):
        hw = t.hardware()
        hours = _clamp_float((q.get("hours") or ["1"])[0], 0.05, 24 * 30, 1.0)
        chars = None
        names = [n for n in (q.get("data") or [""])[0].split("|") if n]
        chat = False
        for n in names:
            try:
                c = t.check(n)
                chars = (chars or 0) + c["chars"]
                chat = chat or c["chat"]
            except TrainError:
                pass
        rec = recommend(dict(hw, chat=chat), chars, hours)
        cluster = t.cluster()
        return {"hardware": hw, "recommend": rec, "cluster": cluster, "datasets": t.datasets(), "models": t.models(),
                "setup": t.setup_state, "running": t.running(), "job": t.job, "hours": hours, "data_dir": t.data_dir}

    ctx.get_routes.update({
        "/api/train": wrap(overview),
        "/api/train/status": wrap(lambda q: t.status(int((q.get("since") or ["0"])[0] or 0))),
        "/api/train/check": wrap(lambda q: {"check": t.check((q.get("name") or [""])[0])}),
        "/api/train/setup": wrap(lambda q: {"setup": t.setup_state}),
    })
    ctx.post_routes.update({
        "/api/train/upload": wrap(t.upload),
        "/api/train/paste": wrap(t.paste),
        "/api/train/data/delete": wrap(t.delete_data),
        "/api/train/model/delete": wrap(t.delete_model),
        "/api/train/start": wrap(t.start),
        "/api/train/stop": wrap(t.stop),
        "/api/train/chat": wrap(t.chat),
        "/api/train/explain": wrap(t.explain),
        "/api/train/setup": wrap(t.setup),
    })
    ctx.post_limits["/api/train/upload"] = UPLOAD_BODY_MAX
    ctx.post_limits["/api/train/paste"] = PASTE_MAX * 2 + 4096


def _clamp_float(x, lo, hi, default):
    try:
        return max(lo, min(hi, float(x)))
    except (TypeError, ValueError):
        return default
