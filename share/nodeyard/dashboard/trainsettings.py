"""Train AI settings as a JSON file you can download, edit (or have an AI write) and upload again.

This is the one specification of that file: every key's type, range, default and meaning. The upload check, the
"Settings JSON specification" panel, the text you hand an AI, and the ranges Start enforces all read it, so they
can't disagree.
"""

import difflib
import json
import math
import re

FORMAT = "nodeyard-train-settings"
VERSION = 1

# The Settings card. "default" is what Start uses when a key is missing from the request; None means the form always
# sends one (the value recommended for your hardware and data).
SETTINGS = [
    {"key": "vocab_size", "type": "integer", "min": 256, "max": 100_000, "default": None, "shape": True, "label": "Vocabulary V",
     "meaning": "How many tokens the BPE tokenizer learns (the first 256 are raw bytes).",
     "advice": "2,048-8,192 for a few MB of text; 16,384-32,768 for 50 MB or more. Costs V x d weights in the embedding."},
    {"key": "context", "type": "integer", "min": 8, "max": 8192, "default": None, "shape": True, "label": "Context T",
     "meaning": "How many tokens the model sees at once.",
     "advice": "128-512 for chat and stories. Memory and time grow with T (attention grows with T squared)."},
    {"key": "dim", "type": "integer", "min": 8, "max": 4096, "default": None, "shape": True, "label": "Width d",
     "meaning": "Numbers per token inside the model. Must divide evenly by heads.",
     "advice": "Usually 64 x heads (e.g. 384 with 6 heads, 512 with 8)."},
    {"key": "layers", "type": "integer", "min": 1, "max": 128, "default": None, "shape": True, "label": "Layers L",
     "meaning": "Transformer blocks stacked on top of each other.",
     "advice": "4-16 for small models. Weights grow as about 12 x L x d squared."},
    {"key": "heads", "type": "integer", "min": 1, "max": 64, "default": None, "shape": True, "label": "Heads h",
     "meaning": "Parallel attention lookups per layer. dim must divide evenly by heads.",
     "advice": "dim / heads = 64 is the usual choice."},
    {"key": "batch_size", "type": "integer", "min": 1, "max": 4096, "default": None, "label": "Batch B",
     "meaning": "Windows of T tokens in one batch.",
     "advice": "The biggest that fits in memory. Tokens per update = batch_size x context x grad_accum; aim for 8k-64k."},
    {"key": "grad_accum", "type": "integer", "min": 1, "max": 64, "default": 1, "label": "Accumulate",
     "meaning": "Batches averaged into one weight update (1 = off).",
     "advice": "Raise it for steadier, larger updates without using more memory. Each step takes grad_accum times longer."},
    {"key": "steps", "type": "integer", "min": 1, "max": 10_000_000, "default": None, "label": "Steps",
     "meaning": "Weight updates in this run. When continuing, these are extra steps on top of the ones already trained. "
                "With stop_when on, this is the maximum.",
     "advice": "Passes over your text = steps x tokens per update / tokens in the data. Past about 20-50 passes a small "
               "data set is being memorised."},
    {"key": "learning_rate", "type": "number", "min": 1e-7, "max": 1.0, "default": None, "label": "Learning rate",
     "meaning": "The peak step size. It warms up from 0 over `warmup` steps, then follows a cosine down to 10% of the "
                "peak by the last step, and is lowered further when validation stops improving.",
     "advice": "About 1e-3 under 5M weights, 6e-4 for 10-50M, 3e-4 around 100M. When continuing, 30-60% of the "
               "first run's peak."},
    {"key": "warmup", "type": "integer", "min": 0, "max": 1_000_000, "default": 100, "label": "Warm-up",
     "meaning": "Steps to ramp the learning rate up from 0 at the start of the run.",
     "advice": "1-5% of steps, usually 100-2,000."},
    {"key": "weight_decay", "type": "number", "min": 0.0, "max": 1.0, "default": 0.1, "label": "Weight decay",
     "meaning": "How much AdamW shrinks the weights each step.",
     "advice": "0.1 is standard; 0-0.2."},
    {"key": "dropout", "type": "number", "min": 0.0, "max": 0.5, "default": 0.0, "label": "Dropout",
     "meaning": "Share of values zeroed at random while training, so the model can't just memorise.",
     "advice": "0 when the text has 20+ tokens per weight; 0.1-0.2 when the model is big for its data."},
    {"key": "eval_every", "type": "integer", "min": 1, "max": 1_000_000, "default": 250, "label": "Check every",
     "meaning": "Steps between validation checks (the held-out 5% of each file). Keep-the-best, stop_when and the "
                "learning-rate cuts all act at these checks.",
     "advice": "A few hundred steps; 50-200 checks per run."},
    {"key": "save_every", "type": "integer", "min": 0, "max": 1_000_000, "default": 500, "label": "Save every",
     "meaning": "Steps between checkpoints (0 = only at the end).",
     "advice": "2-5 x eval_every."},
    {"key": "device", "type": "string", "enum": ["auto", "cpu", "cuda", "mps"], "default": "auto", "label": "Device",
     "meaning": "What each machine trains on. auto = its GPU when it has one, else its CPU.",
     "advice": "auto."},
    {"key": "assistant_only", "type": "boolean", "default": False, "label": "Learn only the replies",
     "meaning": "On conversations (User:/Assistant: lines), no loss on the User: turns: it learns to answer, not to ask. "
                "Plain text (stories, books) is still learned in full.",
     "advice": "true for chatbots."},
]

# What to train and where: the rest of the Train page, outside the Settings card.
RUN = [
    {"key": "data", "type": "array of strings", "default": None,
     "meaning": "File names from step 2 (Your data) to tick, exactly as listed there. Replaces the current ticks (an "
                "empty list leaves them as they are); files that aren't there are skipped with a warning."},
    {"key": "model", "type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,59}$", "default": None,
     "meaning": "With continue true: the saved model to keep training. Otherwise: the name for the new model."},
    {"key": "continue", "type": "boolean", "default": False,
     "meaning": "true = continue `model` (it must exist). Its shape (vocab_size, context, dim, layers, heads) and "
                "tokenizer are kept, so those settings are ignored and can be left out."},
    {"key": "keep_best", "type": "boolean", "default": True,
     "meaning": "When continuing: only replace the saved model if this run beats its best validation loss."},
    {"key": "machines", "type": "array of strings", "default": None,
     "meaning": "Machines to train on, by the names in the Machines card (e.g. \"debian-1\"); not the \" · CPU\" rows."},
    {"key": "together", "type": "string", "enum": ["share", "race", "fastest"], "default": "share",
     "meaning": "With several machines: share = one model, copies averaged every ~2 minutes; race = each trains its "
                "own copy and the best is kept; fastest = only the fastest machine trains."},
    {"key": "both", "type": "boolean", "default": False,
     "meaning": "Also train on each GPU machine's CPU at the same time (a second trainer on that machine)."},
]

# The Stop when card.
STOP_WHEN = [
    {"key": "on", "type": "boolean", "default": False,
     "meaning": "false = train for exactly `steps`. true = stop as soon as the test prompts pass (and the target loss "
                "is reached), or when validation hasn't improved for give_up checks."},
    {"key": "preset", "type": "string", "enum": ["starter", "shakespeare", "stories", "data", "own"], "default": "starter",
     "meaning": "Which test prompts: starter, shakespeare and stories are built in; data = real exchanges picked from "
                "your training data; own = the prompts below."},
    {"key": "prompts", "type": "array of objects", "default": None,
     "meaning": "Your own test prompts (sets preset to own): [{\"prompt\": \"What is 12 plus 7?\", \"expect\": [\"19\"]}]. "
                "A reply passes if it contains any of the expect words. Up to 100."},
    {"key": "pass", "type": "number", "enum": [1, 0.8, 0.6, 0.5], "default": 1,
     "meaning": "Share of the test prompts that must pass."},
    {"key": "target_loss", "type": "number or null", "min": 0.0, "max": 20.0, "default": None,
     "meaning": "Also require validation loss at or below this (null = no target)."},
    {"key": "give_up", "type": "integer", "min": 3, "max": 100, "default": 10,
     "meaning": "Stop after this many checks in a row with no improvement."},
    {"key": "match", "type": "string", "enum": ["exact", "close"], "default": "exact",
     "meaning": "With preset data: a reply is correct with the same words (exact) or 80%+ of them (close)."},
    {"key": "count", "type": "integer", "min": 20, "max": 100, "default": 20,
     "meaning": "With preset data: how many exchanges to pick."},
    {"key": "held_out", "type": "boolean", "default": False,
     "meaning": "With preset data: pick them from the held-out 5% the model never trains on."},
]

SECTIONS = {"settings": SETTINGS, "run": RUN, "stop_when": STOP_WHEN}
BY_KEY = {s["key"]: s for s in SETTINGS}
SHAPE_KEYS = [s["key"] for s in SETTINGS if s.get("shape")]
TOP_KEYS = ("format", "version", "settings", "run", "stop_when", "note")
MODEL_SHAPE = {"vocab_size": "vocabulary", "context": "context", "dim": "width", "layers": "layers", "heads": "heads"}

EXAMPLE = {
    "format": FORMAT,
    "version": VERSION,
    "note": "Continue a ~10M-weight chatbot on the GPU machine, a bit lower learning rate, stop when it stops improving.",
    "settings": {"batch_size": 32, "grad_accum": 2, "steps": 20000, "learning_rate": 0.0004, "warmup": 200,
                 "weight_decay": 0.1, "dropout": 0.1, "eval_every": 250, "save_every": 1000, "device": "auto",
                 "assistant_only": True},
    "run": {"data": ["dolly-15k.jsonl", "tinystories.txt"], "model": "my-model", "continue": True, "keep_best": True,
            "machines": ["debian-1"], "together": "fastest", "both": False},
    "stop_when": {"on": True, "preset": "data", "pass": 0.5, "target_loss": None, "give_up": 10, "match": "close",
                  "count": 20, "held_out": True},
}


def spec():
    """Everything the page needs to show the specification and the text for an AI."""
    return {"format": FORMAT, "version": VERSION, "settings": SETTINGS, "run": RUN, "stop_when": STOP_WHEN,
            "example": EXAMPLE, "text": ai_text()}


def _range(s):
    if "enum" in s:
        return "one of " + ", ".join(json.dumps(v) for v in s["enum"])
    if "min" in s:
        fmt = (lambda v: "{:,}".format(v)) if s["type"] == "integer" else (lambda v: repr(v))
        return "%s to %s" % (fmt(s["min"]), fmt(s["max"]))
    if "pattern" in s:
        return "letters, numbers, dots, dashes, underscores (up to 60)"
    return ""


def ai_text():
    """The whole specification as plain text: paste it into any AI chat with what you want, and it can write the file."""
    out = [
        "# nodeyard Train AI settings file (format %s, version %d)" % (json.dumps(FORMAT), VERSION),
        "",
        "Write ONE JSON object and nothing else (no comments, no trailing commas, numbers without quotes). It is "
        "uploaded on the Train AI page with \"Upload settings\". Every section and every key is optional: a key you "
        "leave out keeps the value already on the page. Unknown keys, wrong types and out-of-range values are "
        "rejected and nothing is applied, so use only the keys below.",
        "",
        "Top level: {\"format\": %s, \"version\": %d, \"note\": \"why these settings\", \"settings\": {...}, "
        "\"run\": {...}, \"stop_when\": {...}}" % (json.dumps(FORMAT), VERSION),
        "",
        "Useful sums: weights N ~= V*d + T*d + 12*L*d^2. Tokens per update = batch_size * context * grad_accum. "
        "Training memory ~= 16*N bytes plus activations. Passes over the text = steps * tokens per update / tokens "
        "in the data (about 1 token per 4 characters of English). dim must divide by heads.",
    ]
    titles = {"settings": "settings (the Settings card)", "run": "run (what to train and where)",
              "stop_when": "stop_when (the Stop when card)"}
    for name, keys in SECTIONS.items():
        out += ["", "## " + titles[name]]
        for s in keys:
            bits = [s["type"]]
            r = _range(s)
            if r:
                bits.append(r)
            if s.get("default") is not None or s["type"] in ("boolean", "number or null"):
                bits.append("default " + json.dumps(s.get("default")))
            line = "- %s (%s): %s" % (s["key"], "; ".join(bits), s["meaning"])
            if s.get("advice"):
                line += " Rule of thumb: " + s["advice"]
            out.append(line)
    out += ["", "## Example", json.dumps(EXAMPLE, indent=2)]
    return "\n".join(out) + "\n"


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _value(section, s, v, errors):
    """(True, the value cast to its type), or (False, None) after adding an error."""
    where = "%s.%s" % (section, s["key"])
    t = s["type"]
    if t == "boolean":
        if isinstance(v, bool):
            return True, v
        errors.append("%s must be true or false (not %s)." % (where, json.dumps(v)))
        return False, None
    if t == "number or null" and v is None:
        return True, None
    if t in ("integer", "number", "number or null"):
        if not _is_number(v):
            errors.append("%s must be a number without quotes (not %s)." % (where, json.dumps(v)))
            return False, None
        if t == "integer":
            if v != int(v):
                errors.append("%s must be a whole number (not %s)." % (where, json.dumps(v)))
                return False, None
            v = int(v)
        else:
            v = float(v)
        if "enum" in s and v not in s["enum"]:
            errors.append("%s must be %s (not %s)." % (where, _range(s), json.dumps(v)))
            return False, None
        if "min" in s and not (s["min"] <= v <= s["max"]):
            errors.append("%s must be %s (not %s)." % (where, _range(s), json.dumps(v)))
            return False, None
        return True, v
    if t == "string":
        if not isinstance(v, str):
            errors.append("%s must be a string in quotes (not %s)." % (where, json.dumps(v)))
            return False, None
        if "enum" in s and v not in s["enum"]:
            errors.append("%s must be %s (not %s)." % (where, _range(s), json.dumps(v)))
            return False, None
        if "pattern" in s and not re.match(s["pattern"], v):
            errors.append("%s must be %s (not %s)." % (where, _range(s), json.dumps(v)))
            return False, None
        return True, v
    if t == "array of strings":
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            errors.append("%s must be a list of names in quotes, like [\"a.txt\"]." % where)
            return False, None
        return True, list(dict.fromkeys(v))
    if t == "array of objects":
        if not isinstance(v, list) or len(v) > 100:
            errors.append("%s must be a list of up to 100 objects." % where)
            return False, None
        out = []
        for i, p in enumerate(v):
            if not isinstance(p, dict) or not isinstance(p.get("prompt"), str) or not p["prompt"].strip():
                errors.append("%s[%d] needs a \"prompt\" string." % (where, i))
                return False, None
            extra = sorted(set(p) - {"prompt", "expect", "answer"})
            if extra:
                errors.append("%s[%d] has unknown key(s) %s: use prompt, expect and answer." % (where, i, ", ".join(extra)))
                return False, None
            expect = p.get("expect", [])
            if isinstance(expect, str):
                expect = [x.strip() for x in expect.split(",")]
            if not isinstance(expect, list) or not all(isinstance(x, str) for x in expect):
                errors.append("%s[%d].expect must be a list of words." % (where, i))
                return False, None
            answer = p.get("answer", "")
            if not isinstance(answer, str):
                errors.append("%s[%d].answer must be a string." % (where, i))
                return False, None
            out.append({"prompt": p["prompt"][:1000], "expect": [x[:80] for x in expect if x][:10], "answer": answer[:1000]})
        return True, out
    raise ValueError(t)


def _unknown(section, key, known, errors):
    near = difflib.get_close_matches(key, known, n=1, cutoff=0.6)
    errors.append("Unknown key %s%s.%s" % (("%s." % section) if section else "", key,
                                          " Did you mean %s?" % near[0] if near else " Known keys: " + ", ".join(known) + "."))


def check(doc, current=None, datasets=(), models=(), machines=()):
    """Check an uploaded settings file. Nothing is applied when there are errors.

    current: the settings already on the page, so a file that changes only dim is checked against the page's heads.
    datasets, machines: names that exist; models: the saved models' info (name, vocabulary, context, width, ...).
    Returns {"settings": {...}, "run": {...}, "stop_when": {...}, "errors": [...], "warnings": [...]}; each section
    holds only the keys the file gives.
    """
    errors, warnings = [], []
    out = {"settings": {}, "run": {}, "stop_when": {}, "errors": errors, "warnings": warnings}
    if not isinstance(doc, dict):
        errors.append("The file must be one JSON object: {...}.")
        return out
    if "settings" not in doc and any(k in BY_KEY for k in doc):   # a bare settings object is fine too
        doc = {"settings": doc}
    if "format" in doc and doc["format"] != FORMAT:
        errors.append("format must be %s (not %s)." % (json.dumps(FORMAT), json.dumps(doc["format"])))
    if "version" in doc and (not _is_number(doc["version"]) or doc["version"] > VERSION):
        warnings.append("This file says version %s; this dashboard reads version %d, so newer keys may be rejected."
                        % (json.dumps(doc["version"]), VERSION))
    for k in doc:
        if k not in TOP_KEYS:
            _unknown("", k, list(TOP_KEYS), errors)
    for name, keys in SECTIONS.items():
        part = doc.get(name)
        if part is None:
            continue
        if not isinstance(part, dict):
            errors.append("%s must be an object: {...}." % name)
            continue
        by = {s["key"]: s for s in keys}
        for k, v in part.items():
            if k not in by:
                _unknown(name, k, list(by), errors)
                continue
            ok, got = _value(name, by[k], v, errors)
            if ok:
                out[name][k] = got

    s, run = out["settings"], out["run"]
    merged = dict(current or {}, **s)
    try:
        dim, heads = int(merged.get("dim")), int(merged.get("heads"))
        if heads > 0 and dim % heads and ("dim" in s or "heads" in s):
            errors.append("dim (%d) must divide evenly by heads (%d)." % (dim, heads))
    except (TypeError, ValueError):
        pass

    if "data" in run:
        have = set(datasets or ())
        missing = [n for n in run["data"] if n not in have]
        run["data"] = [n for n in run["data"] if n in have]
        if missing and run["data"]:
            warnings.append("Not in your data, so not ticked: %s." % ", ".join(missing))
        elif missing:
            warnings.append("None of run.data is in your data (%s): the ticks stay as they are." % ", ".join(missing))
        if not run["data"]:
            del run["data"]
    if "machines" in run:
        have = set(machines or ())
        missing = [n for n in run["machines"] if n not in have]
        if missing:
            warnings.append("No machine called %s: left out." % ", ".join(missing))
        run["machines"] = [n for n in run["machines"] if n in have]
        if not run["machines"]:
            del run["machines"]
    if run.get("continue"):
        info = next((m for m in models or () if m.get("name") == run.get("model")), None)
        if not run.get("model"):
            errors.append("run.continue is true, so run.model must name the saved model to continue.")
        elif info is None:
            errors.append("There's no saved model called %s to continue." % run["model"])
        else:
            differ = ["%s %s (it has %s)" % (k, s[k], info.get(f)) for k, f in MODEL_SHAPE.items()
                      if k in s and info.get(f) is not None and s[k] != info.get(f)]
            if differ:
                warnings.append("Continuing %s keeps its own shape, so these are ignored: %s." % (run["model"], "; ".join(differ)))
    elif run.get("model") and any(m.get("name") == run["model"] for m in models or ()):
        warnings.append("A saved model is already called %s: set run.continue to true to continue it, or pick another "
                        "name before pressing Start." % run["model"])
    sw = out["stop_when"]
    if "prompts" in sw:   # given prompts are graded by their answers (preset data) or expected words (your own)
        if not (sw.get("preset") == "data" and all(p["answer"] for p in sw["prompts"])):
            sw["preset"] = "own"
        if sw.get("on") and sw.get("target_loss") is None and not any(p["expect"] or p["answer"] for p in sw["prompts"]):
            errors.append("stop_when is on, but none of its prompts has an expect word or answer: add some, or a target_loss.")
    return out

