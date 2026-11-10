#!/usr/bin/env python3
"""Portable BPE-token GPT trainer for nodeyard (PyTorch: x86_64 and arm64, CPU or GPU).

Same recipe and same outputs as the Apple MLX trainer in NeuralNetworkTutor
(train_bpe_gpt.py): BPE tokens, learned positional embeddings, causal attention,
pre-norm transformer blocks, AdamW with weight decay, warmup then cosine decay,
gradient clipping, float32. The log lines match the app's parser, so a remote run
can be read like a local one.

DO NOT CHANGE the tokenizer (bpe_tokenizer.py), the file names, the model.json keys,
or the log formats without updating the app and the nodeyard jobs runner.
bpe_tokenizer.py and sampling.py are copies of the app's modules, so this directory
runs on a node with nothing else installed except torch and numpy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import signal
import time
import uuid
from pathlib import Path

import numpy as np
import torch
from torch import nn

from bpe_tokenizer import BPETokenizer
from lr_governor import LearningRateGovernor
from sampling import sample_token
from training_data import load_files

MODEL_FILE = "model.pt"
OPTIMIZER_FILE = "optimizer.pt"
CONFIG_FILE = "model.json"
TOKENIZER_FILE = "bpe.json"
RESULT_FILE = "result.json"


def emit(message: str) -> None:
    print(message, flush=True)


class GPT(nn.Module):
    def __init__(self, vocab_size: int, context: int, dim: int, heads: int, layers: int, dropout: float = 0.0):
        super().__init__()
        self.token_embedding = nn.Embedding(vocab_size, dim)
        self.position_embedding = nn.Embedding(context, dim)
        # Dropout (training only) zeroes a random fraction of values in each block, so the model can't lean on any
        # one path: it memorises less when the text is small. It has no weights, so it doesn't change the shape.
        block = nn.TransformerEncoderLayer(dim, heads, dim_feedforward=4 * dim, dropout=dropout,
                                           activation="gelu", batch_first=True, norm_first=True)
        self.blocks = nn.TransformerEncoder(block, num_layers=layers, enable_nested_tensor=False)
        self.norm = nn.LayerNorm(dim)
        self.head = nn.Linear(dim, vocab_size)

    def forward(self, tokens: torch.Tensor) -> torch.Tensor:
        length = tokens.shape[1]
        positions = torch.arange(length, device=tokens.device)
        hidden = self.token_embedding(tokens) + self.position_embedding(positions)
        mask = torch.triu(torch.full((length, length), float("-inf"), device=tokens.device), diagonal=1)
        hidden = self.blocks(hidden, mask=mask, is_causal=True)
        return self.head(self.norm(hidden))


def pick_device(requested: str) -> torch.device:
    if requested != "auto":
        return torch.device(requested)
    if torch.cuda.is_available():
        return torch.device("cuda")
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def encode_with_roles(tokenizer: BPETokenizer, text: str, assistant_only: bool):
    ids: list[int] = []
    weights: list[float] = []
    in_user_turn = False
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("User:"):
            in_user_turn = True
        elif stripped.startswith("Assistant:"):
            in_user_turn = False
        marker = ROLE_MARKER.match(stripped) if assistant_only else None
        if marker:
            # The role marker itself is read but not graded: the chat prompt always supplies "Assistant:", and a model
            # that learns to write markers prints them inside replies.
            head = line[:len(line) - len(stripped) + marker.end()]
            head_ids = tokenizer.encode(head)
            ids.extend(head_ids)
            weights.extend([0.0] * len(head_ids))
            line = line[len(head):]
        line_ids = tokenizer.encode(line)
        ids.extend(line_ids)
        weights.extend([0.0 if (assistant_only and in_user_turn) else 1.0] * len(line_ids))
    return np.asarray(ids, dtype=np.int64), np.asarray(weights, dtype=np.float32)


def cached_tokens(app_data: Path, tokenizer_path: Path, data_files, assistant_only: bool, encode):
    """Encoding with BPE is slow in Python, and a resumed or round-based run reads the same text again: keep the
    token ids next to the model, keyed by the tokenizer, the data files (name, size, time) and assistant-only."""
    key = hashlib.sha256()
    key.update(tokenizer_path.read_bytes())
    for name in data_files:
        st = os.stat(name)
        key.update(f"{Path(name).name}|{st.st_size}|{int(st.st_mtime)}|".encode())
    key.update(b"assistant-only-v2" if assistant_only else b"all")   # v2: role markers aren't graded
    digest = key.hexdigest()[:16]
    cache = app_data / "tokens.npz"
    try:
        with np.load(cache) as saved:
            if str(saved["key"]) == digest:
                emit("Using the tokens cached from the last run (same text and tokenizer).")
                return saved["tokens"], saved["weights"]
    except (OSError, KeyError, ValueError):
        pass
    tokens, weights = encode()
    try:
        tmp = app_data / "tokens.tmp.npz"
        np.savez(tmp, key=np.array(digest), tokens=tokens, weights=weights)
        os.replace(tmp, cache)
    except OSError:
        pass
    return tokens, weights


def average(args) -> int:
    """Weighted mean of several copies of the same model (training shared across machines).

    Each machine trained its own copy for a while on different random windows; averaging the weights combines what
    they learned: w = Σ_i a_i·w_i / Σ_i a_i, with a_i the tokens machine i trained on.
    """
    parts = []
    for spec in args.parts:
        path, _, weight = spec.rpartition(":")
        parts.append((Path(path), float(weight)))
    total = sum(w for _, w in parts)
    if not parts or total <= 0:
        raise ValueError("Nothing to average.")
    merged = None
    for path, weight in parts:
        state = torch.load(path, map_location="cpu")
        if merged is None:
            merged = {k: v.double() * (weight / total) for k, v in state.items()}
        else:
            if state.keys() != merged.keys():
                raise ValueError(f"{path.name} is a different model shape.")
            for k, v in state.items():
                merged[k] += v.double() * (weight / total)
    out = args.out / MODEL_FILE
    tmp = args.out / (MODEL_FILE + ".tmp")
    torch.save({k: v.float() for k, v in merged.items()}, tmp)
    os.replace(tmp, out)
    emit("Averaged " + str(len(parts)) + " copies: " + ", ".join(f"{p.name} × {w / total:.3f}" for p, w in parts))
    return 0


def sample_windows(tokens, weights, context, batch, rng, device):
    starts = np.array([rng.randrange(0, len(tokens) - context - 1) for _ in range(batch)])
    offsets = np.arange(context + 1)[None, :]
    block = torch.as_tensor(tokens[starts[:, None] + offsets], device=device)
    target_weights = torch.as_tensor(weights[starts[:, None] + offsets][:, 1:], device=device)
    return block[:, :-1], block[:, 1:], target_weights


def loss_fn(model, inputs, targets, weights):
    logits = model(inputs)
    per_token = nn.functional.cross_entropy(logits.reshape(-1, logits.shape[-1]), targets.reshape(-1), reduction="none")
    per_token = per_token.reshape(targets.shape)
    return (per_token * weights).sum() / torch.clamp(weights.sum(), min=1.0)


def learning_rate(step: int, total: int, peak: float, warmup: int) -> float:
    if step <= warmup:
        return peak * step / max(1, warmup)
    progress = (step - warmup) / max(1, total - warmup)
    return peak * (0.1 + 0.9 * 0.5 * (1 + math.cos(math.pi * min(1.0, progress))))


@torch.no_grad()
def evaluate(model, tokens, weights, context, batch, device, batches=10) -> float:
    rng = random.Random(1234)
    model.eval()
    total = 0.0
    for _ in range(batches):
        inputs, targets, target_weights = sample_windows(tokens, weights, context, batch, rng, device)
        total += float(loss_fn(model, inputs, targets, target_weights).item())
    model.train()
    return total / batches


def train(args) -> int:
    torch.manual_seed(args.seed)
    if args.threads:
        torch.set_num_threads(args.threads)
    device = pick_device(args.device)
    rng = random.Random(args.seed)
    text, reports = load_files(args.data_files)
    for report in reports:
        emit(f"Data {report['file']}: {report['format']} format, {report['records']:,} records"
             f"{', ' + format(report['skipped'], ',') + ' skipped' if report['skipped'] else ''}"
             f"{', conversations found' if report['chat'] else ''}.")
        for problem in report["problems"][:5]:
            emit(f"  skipped: {problem}")
    if len(text) < 1000:
        raise ValueError("Please choose text with at least 1,000 characters.")
    app_data = args.app_data.expanduser()
    app_data.mkdir(parents=True, exist_ok=True)
    emit(f"Device: {device} · threads: {torch.get_num_threads()} · PyTorch {torch.__version__}")

    previous_steps = 0
    saved: dict = {}
    tokenizer_path = app_data / TOKENIZER_FILE
    if args.resume:
        if not ((app_data / MODEL_FILE).is_file() and tokenizer_path.is_file()):
            raise FileNotFoundError("There is no saved BPE model to continue. Train a new model first.")
        tokenizer = BPETokenizer.load(tokenizer_path)
        saved = json.loads((app_data / CONFIG_FILE).read_text(encoding="utf-8"))
        previous_steps = int(saved.get("trained_steps", 0))
        # The saved weights fix the shape: use it, whatever the command line says.
        for key, attr in (("layers", "layers"), ("width", "dim"), ("heads", "heads"), ("context", "context")):
            if key in saved and int(saved[key]) != getattr(args, attr):
                emit(f"Using the saved model's {key} ({saved[key]}) instead of {getattr(args, attr)}.")
                setattr(args, attr, int(saved[key]))
        emit(f"Continuing from the saved model after {previous_steps:,} training steps.")
    else:
        emit(f"Learning a {args.vocab_size:,}-token BPE vocabulary from {len(text):,} characters...")
        tokenizer = BPETokenizer.train(text, args.vocab_size)
        tokenizer.save(tokenizer_path)

    tokens, weights = cached_tokens(app_data, tokenizer_path, args.data_files, args.assistant_only,
                                    lambda: encode_with_roles(tokenizer, text, args.assistant_only))
    split = int(len(tokens) * 0.95)
    if len(tokens) - split < args.context + 2:
        if args.resume:
            raise ValueError(f"Not enough text to check the model: the held-out 5% is {len(tokens) - split:,} tokens, "
                             f"less than the saved model's context of {args.context}. Add more text.")
        shorter = max(8, (len(tokens) - split) - 2)
        if shorter < 8 or len(tokens) - split < 10:
            raise ValueError("Not enough text: add more (a few hundred kilobytes is a sensible minimum).")
        emit(f"The text is short, so the context is lowered from {args.context} to {shorter} tokens.")
        args.context = shorter
    train_tokens, valid_tokens = tokens[:split], tokens[split:]
    train_weights, valid_weights = weights[:split], weights[split:]
    emit(f"Tokens: {len(tokens):,} ({len(text) / len(tokens):.2f} characters per token); "
         f"{len(train_tokens):,} to train, {len(valid_tokens):,} to validate.")

    model = GPT(tokenizer.vocab_size, args.context, args.dim, args.heads, args.layers, args.dropout).to(device)
    if args.resume:
        model.load_state_dict(torch.load(app_data / MODEL_FILE, map_location=device))
    parameters = sum(p.numel() for p in model.parameters())
    emit(f"Model initialized: {parameters:,} trainable parameters; {args.layers} blocks, {args.heads} attention heads, "
         f"width {args.dim}, context {args.context}{f', dropout {args.dropout:g}' if args.dropout else ''}.")
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate, betas=(0.9, 0.95),
                                  weight_decay=args.weight_decay)
    if args.resume and (app_data / OPTIMIZER_FILE).is_file():
        try:
            optimizer.load_state_dict(torch.load(app_data / OPTIMIZER_FILE, map_location=device))
            emit("Restored AdamW's running averages (m and v) from the last save.")
        except (RuntimeError, ValueError, KeyError) as error:
            emit(f"Couldn't restore the optimizer state ({error}); starting AdamW fresh.")
    elif args.resume:
        emit("No saved optimizer state: AdamW starts fresh, so warm-up protects the model for the first steps.")
    peak = args.learning_rate
    governor = LearningRateGovernor(args.learning_rate)
    quality = parse_quality(args.quality)
    chat_data = "\nAssistant:" in "\n" + text
    best = {"loss": None, "since": 0}
    model_id = (saved.get("model_id") if args.resume else None) or uuid.uuid4().hex[:12]
    stop = {"asked": False}

    def ask_to_stop(signum, _frame):
        stop["asked"] = True
        emit("Stop requested: finishing this step, then saving.")

    signal.signal(signal.SIGINT, ask_to_stop)
    signal.signal(signal.SIGTERM, ask_to_stop)

    def save(done_steps: int, validation: float | None) -> None:
        tmp = app_data / (MODEL_FILE + ".tmp")
        torch.save({k: v.detach().cpu() for k, v in model.state_dict().items()}, tmp)
        os.replace(tmp, app_data / MODEL_FILE)
        tmp = app_data / (OPTIMIZER_FILE + ".tmp")
        torch.save(optimizer.state_dict(), tmp)
        os.replace(tmp, app_data / OPTIMIZER_FILE)
        config = {
            "name": "BPE-token GPT (PyTorch, portable)", "model_id": model_id,
            "model_name": f"BPE-token GPT · model {model_id[:8]}", "tokenizer": "bpe",
            "backend": "torch", "optimizer": "AdamW", "precision": "float32",
            "parameters": parameters, "vocabulary": tokenizer.vocab_size, "layers": args.layers,
            "width": args.dim, "heads": args.heads, "context": args.context, "batch_size": args.batch_size,
            "learning_rate": args.learning_rate, "weight_decay": args.weight_decay,
            "assistant_only": bool(args.assistant_only), "dropout": args.dropout, "trained_steps": previous_steps + done_steps,
            "validation_loss": validation, "compute_device": str(device),
            "data_files": [Path(p).name for p in args.data_files], "saved_at": time.time(),
        }
        tmp = app_data / (CONFIG_FILE + ".tmp")
        tmp.write_text(json.dumps(config, indent=2), encoding="utf-8")
        os.replace(tmp, app_data / CONFIG_FILE)

    started = time.perf_counter()
    model.train()
    last_validation = None
    done = 0
    for step in range(1, args.steps + 1):
        if args.lr_progress:
            # Round-based training across machines: this run covers progress a..b of the whole job's cosine curve
            # (warm-up already happened in the first round).
            a, b = args.lr_progress
            progress = a + (b - a) * step / max(1, args.steps)
            rate = peak * (0.1 + 0.9 * 0.5 * (1 + math.cos(math.pi * min(1.0, progress))))
        else:
            rate = learning_rate(step, args.steps, peak, args.warmup)
        for group in optimizer.param_groups:
            group["lr"] = rate
        inputs, targets, target_weights = sample_windows(train_tokens, train_weights, args.context,
                                                         args.batch_size, rng, device)
        loss = loss_fn(model, inputs, targets, target_weights)
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        grad_norm = float(torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0))
        optimizer.step()
        value = float(loss.item())
        done = step
        if not math.isfinite(value):
            raise RuntimeError(f"Training diverged at step {step} (loss {value}). Nothing was saved.")
        total_step = previous_steps + step
        if step % 10 == 0 or step == args.steps:
            elapsed = max(1e-9, time.perf_counter() - started)
            emit(f"Step {step}/{args.steps} | train loss {value:.4f} | {step / elapsed:.2f} steps/sec | "
                 f"{step * args.batch_size * args.context / elapsed:,.0f} tokens/sec | "
                 f"total training step {total_step:,} | learning rate {rate:.8g}")
        if step % args.eval_every == 0 or step == args.steps:
            validation = evaluate(model, valid_tokens, valid_weights, args.context, args.batch_size, device)
            emit(f"Validation at step {step}: loss {validation:.4f} | total training step {total_step:,}")
            emit(f"Gradient norm at step {step}: {grad_norm:.4f} (clipped to 1.0 when larger)")
            last_validation = validation
            if quality:
                judge_quality(model, tokenizer, args, device, quality, chat_data, validation, total_step, best, stop)
            if args.lr_progress or step > args.warmup:   # during warm-up the rate is still climbing: nothing to judge
                new_rate = governor.observe(validation, peak, total_step, emit)
                if new_rate is not None:
                    peak = new_rate
        if args.save_every and step % args.save_every == 0 and step != args.steps and not stop["asked"]:
            save(step, last_validation)
            emit(f"Checkpoint saved at total training step {total_step:,}.")
        if stop["asked"]:
            break

    if done == 0:
        emit("Stopped before the first step finished; nothing was changed.")
        return 0
    final_validation = evaluate(model, valid_tokens, valid_weights, args.context, args.batch_size, device, batches=20)
    save(done, final_validation)
    if args.result_file:
        Path(args.result_file).write_text(json.dumps({"validation_loss": final_validation,
                                                      "total_steps": previous_steps + done}), encoding="utf-8")
    emit(f"Saved checkpoint after {previous_steps + done:,} total training steps to {app_data / MODEL_FILE}")
    if stop["asked"]:
        emit(f"Training stopped early and saved. Final validation loss {final_validation:.4f}.")
    else:
        emit(f"Training finished. Final validation loss {final_validation:.4f}.")
    return 0


REPLY_ENDS = ("\nUser:", "\nAssistant:", "\nSystem:", "\n\n")
ROLE_MARKER = re.compile(r"(User|Assistant|System):")


def end_of_reply(text: str):
    """Where a chat reply ends: the next turn, the blank line that ends a conversation in the training data, or a
    role marker anywhere (a small model can write "…Which Assistant:" mid-line; that's never part of a reply)."""
    body = text.lstrip()
    offset = len(text) - len(body)
    found = [body.find(end) for end in REPLY_ENDS if body.find(end) > 0]
    marker = ROLE_MARKER.search(body, 1)
    if marker:
        found.append(marker.start())
    return offset + min(found) if found else None


def clean_reply(text: str) -> str:
    """The reply without a role marker the model may have started it with ("Assistant: …")."""
    text = text.strip()
    while True:
        m = ROLE_MARKER.match(text)
        if not m:
            return text
        text = text[m.end():].strip()


@torch.no_grad()
def judge_quality(model, tokenizer, args, device, quality, chat_data, validation, total_step, best, stop):
    """After a validation check: test the replies, then stop if the model is good enough, or if it stopped improving."""
    results, score = check_quality(model, tokenizer, args.context, device, quality, chat_data) if quality["prompts"] else ([], None)
    emit("QUALITY " + json.dumps({"step": total_step, "score": score, "loss": validation, "results": results}))
    wanted = []
    if score is not None:
        wanted.append(score >= quality["pass"] - 1e-9)
    if quality["target_loss"] is not None:
        wanted.append(validation <= quality["target_loss"])
    if wanted and all(wanted):
        parts = []
        if score is not None:
            parts.append(f"{sum(1 for r in results if r['ok'])} of {sum(1 for r in results if r['ok'] is not None)} test prompts pass")
        if quality["target_loss"] is not None:
            parts.append(f"validation loss {validation:.4f} ≤ {quality['target_loss']}")
        emit(f"Good enough at total training step {total_step:,}: {' and '.join(parts)}.")
        stop["asked"] = True
        return
    if best["loss"] is None or validation < best["loss"] * 0.999:
        best["loss"], best["since"] = validation, 0
    else:
        best["since"] += 1
    if quality["give_up"] and best["since"] >= quality["give_up"]:
        emit(f"Stopped improving before it was good enough: validation hasn't beaten {best['loss']:.4f} in {best['since']} checks. "
             "More steps won't help now: it needs more (or cleaner) data, a bigger model, or an easier target.")
        stop["asked"] = True


@torch.no_grad()
def write_reply(model, tokenizer, context: int, prompt: str, device, rng, chat: bool, max_tokens: int = 160,
                temperature: float = 0.7, top_k: int = 40, top_p: float = 0.9, repetition_penalty: float = 1.1) -> str:
    prompt_ids = tokenizer.encode(prompt) or [ord("\n")]
    generated: list[int] = []
    for step in range(1, max_tokens + 1):
        context_ids = (prompt_ids + generated)[-context:]
        logits = model(torch.as_tensor([context_ids], device=device))[0, -1].float().cpu().numpy()
        generated.append(sample_token(logits, rng, recent=generated[-64:], temperature=temperature,
                                      top_k=top_k, top_p=top_p, repetition_penalty=repetition_penalty))
        if chat and step % 4 == 0 and end_of_reply(tokenizer.decode(generated)) is not None:
            break
    text = tokenizer.decode(generated)
    if not chat:
        return text.strip()
    text = clean_reply(text)
    cut = end_of_reply(text)
    return (text[:cut] if cut is not None else text).strip()


def parse_quality(spec: str | None):
    """--quality JSON: {"prompts": [{"prompt": "...", "expect": ["word", ...]}], "pass": 0.8, "target_loss": 2.0,
    "give_up": 10}. A prompt passes when its reply contains any expected word (any case); prompts without
    expected words are only shown. Training stops when the pass rate and the target loss (whichever are set) are
    both reached, or after `give_up` checks in a row without a better validation loss."""
    if not spec:
        return None
    q = json.loads(spec)
    prompts = []
    for p in (q.get("prompts") or [])[:20]:
        if isinstance(p, dict) and str(p.get("prompt") or "").strip():
            prompts.append({"prompt": str(p["prompt"])[:500],
                            "expect": [str(e).strip() for e in (p.get("expect") or []) if str(e).strip()][:10]})
    target = q.get("target_loss")
    return {"prompts": prompts, "pass": min(1.0, max(0.0, float(q.get("pass", 1.0)))),
            "target_loss": float(target) if target not in (None, "") else None, "give_up": max(0, int(q.get("give_up", 10)))}


@torch.no_grad()
def check_quality(model, tokenizer, context: int, device, quality: dict, chat: bool):
    """Ask the model every test prompt (low temperature, fixed seed) and grade the replies."""
    model.eval()
    rng = np.random.default_rng(0)
    results = []
    for p in quality["prompts"]:
        prompt = f"User: {p['prompt']}\nAssistant:" if chat else p["prompt"]
        reply = write_reply(model, tokenizer, context, prompt, device, rng, chat, max_tokens=60, temperature=0.3, top_k=10)
        ok = any(e.lower() in reply.lower() for e in p["expect"]) if p["expect"] else None
        results.append({"prompt": p["prompt"], "reply": reply[:400], "expect": p["expect"], "ok": ok})
    model.train()
    graded = [r for r in results if r["ok"] is not None]
    score = sum(1 for r in graded if r["ok"]) / len(graded) if graded else None
    return results, score


@torch.no_grad()
def generate(args) -> int:
    app_data = args.app_data.expanduser()
    config = json.loads((app_data / CONFIG_FILE).read_text(encoding="utf-8"))
    device = pick_device(args.device)
    tokenizer = BPETokenizer.load(app_data / TOKENIZER_FILE)
    model = GPT(config["vocabulary"], config["context"], config["width"], config["heads"], config["layers"]).to(device)
    model.load_state_dict(torch.load(app_data / MODEL_FILE, map_location=device))
    model.eval()
    rng = np.random.default_rng(args.seed)
    reply = write_reply(model, tokenizer, config["context"], args.prompt, device, rng, args.chat_mode, args.max_tokens,
                        args.temperature, args.top_k, args.top_p, args.repetition_penalty)
    if args.chat_mode:
        emit("CHAT_REPLY_BEGIN")
        emit(reply)
        emit("CHAT_REPLY_END")
    else:
        emit(f"Prompt: {args.prompt}")
        emit("Generated continuation:")
        emit(reply)
    return 0


def _r(x, digits: int = 6):
    """Round a number (or nested list of numbers) so the page gets readable JSON."""
    if isinstance(x, torch.Tensor):
        x = x.detach().float().cpu().tolist()
    if isinstance(x, list):
        return [_r(v, digits) for v in x]
    return float(f"{float(x):.{digits}g}")


def _stats(t: torch.Tensor) -> dict:
    t = t.detach().float()
    return {"mean": _r(t.mean()), "std": _r(t.std()) if t.numel() > 1 else 0.0,
            "min": _r(t.min()), "max": _r(t.max()), "norm": _r(t.norm())}


def explain(args) -> int:
    """Run one real forward and backward pass by hand and print every intermediate value as JSON.

    The forward pass is written out step by step (the same maths PyTorch's layers do) and checked
    against the model's own output, so the numbers on the page are exactly what training uses.
    """
    torch.manual_seed(0)
    app_data = args.app_data.expanduser() if args.app_data else None
    trained = bool(app_data and (app_data / MODEL_FILE).is_file() and (app_data / CONFIG_FILE).is_file())
    if trained:
        config = json.loads((app_data / CONFIG_FILE).read_text(encoding="utf-8"))
        tokenizer = BPETokenizer.load(app_data / TOKENIZER_FILE)
    else:
        # No model yet: a tiny untrained one on single bytes, so the maths can still be shown.
        config = {"vocabulary": 256, "context": 32, "width": 16, "heads": 2, "layers": 1,
                  "learning_rate": 1e-3, "weight_decay": 0.1}
        tokenizer = BPETokenizer([])
    device = torch.device("cpu")
    model = GPT(config["vocabulary"], config["context"], config["width"], config["heads"], config["layers"]).to(device)
    if trained:
        model.load_state_dict(torch.load(app_data / MODEL_FILE, map_location=device))
    model.eval()
    ids = tokenizer.encode(args.prompt or "Hello there")[: min(args.max_tokens, config["context"])]
    if len(ids) < 2:
        ids = ids + tokenizer.encode(" .")
    T, d, H = len(ids), config["width"], config["heads"]
    hd = d // H
    show = min(args.show_dims, d)
    tokens = torch.tensor([ids])
    out: dict = {"trained": trained, "config": {k: config.get(k) for k in (
        "vocabulary", "context", "width", "heads", "layers", "parameters", "trained_steps", "validation_loss",
        "learning_rate", "weight_decay", "batch_size")}, "show_dims": show, "head_dim": hd}
    out["tokens"] = [{"id": int(i), "text": tokenizer.decode([i])} for i in ids]

    with torch.no_grad():
        te = model.token_embedding.weight[tokens[0]]
        pe = model.position_embedding.weight[:T]
        x = te + pe
        out["embedding"] = {
            "token_rows": _r(te[:, :show]), "position_rows": _r(pe[:, :show]), "sum_rows": _r(x[:, :show]),
            "token_matrix_shape": list(model.token_embedding.weight.shape),
            "position_matrix_shape": list(model.position_embedding.weight.shape)}
        mask = torch.triu(torch.full((T, T), float("-inf")), diagonal=1)
        layers = []
        for index, layer in enumerate(model.blocks.layers):
            info: dict = {"index": index}
            # 1. Layer norm before attention: (x - mean) / sqrt(var + eps) * gamma + beta, per position.
            mu = x.mean(-1, keepdim=True)
            var = x.var(-1, unbiased=False, keepdim=True)
            xhat = (x - mu) / torch.sqrt(var + layer.norm1.eps)
            h = xhat * layer.norm1.weight + layer.norm1.bias
            info["norm1"] = {"mean": _r(mu[:, 0]), "variance": _r(var[:, 0]), "eps": layer.norm1.eps,
                             "normalized_last": _r(xhat[-1, :show]), "gamma": _r(layer.norm1.weight[:show]),
                             "beta": _r(layer.norm1.bias[:show]), "out_last": _r(h[-1, :show])}
            # 2. Attention: Q = hWq + bq, K = hWk + bk, V = hWv + bv, split into heads.
            W, b = layer.self_attn.in_proj_weight, layer.self_attn.in_proj_bias
            q, k, v = (h @ W.T + b).split(d, dim=-1)
            heads = []
            mixed = []
            for head in range(H):
                sl = slice(head * hd, (head + 1) * hd)
                scores = q[:, sl] @ k[:, sl].T / math.sqrt(hd)
                weights = torch.softmax(scores + mask, dim=-1)
                mixed.append(weights @ v[:, sl])
                if head < 4:
                    heads.append({"q": _r(q[:, sl][:, :show]), "k": _r(k[:, sl][:, :show]), "v": _r(v[:, sl][:, :show]),
                                  "scores": _r(torch.where(mask == 0, scores, torch.full_like(scores, float("nan"))).nan_to_num(-1e9)),
                                  "weights": _r(weights)})
            concat = torch.cat(mixed, dim=-1)
            attn = concat @ layer.self_attn.out_proj.weight.T + layer.self_attn.out_proj.bias
            x = x + attn
            info["attention"] = {"heads": heads, "scale": _r(1 / math.sqrt(hd)), "out_last": _r(attn[-1, :show]),
                                 "residual_last": _r(x[-1, :show])}
            # 3. Feed-forward: GELU(h W1 + b1) W2 + b2 after the second layer norm.
            mu2 = x.mean(-1, keepdim=True)
            var2 = x.var(-1, unbiased=False, keepdim=True)
            h2 = (x - mu2) / torch.sqrt(var2 + layer.norm2.eps) * layer.norm2.weight + layer.norm2.bias
            pre = h2 @ layer.linear1.weight.T + layer.linear1.bias
            act = torch.nn.functional.gelu(pre)
            ff = act @ layer.linear2.weight.T + layer.linear2.bias
            x = x + ff
            info["feed_forward"] = {"hidden_size": int(pre.shape[-1]), "pre_last": _r(pre[-1, :show]),
                                    "gelu_last": _r(act[-1, :show]), "active_fraction": _r((pre > 0).float().mean()),
                                    "out_last": _r(ff[-1, :show]), "residual_last": _r(x[-1, :show])}
            layers.append(info)
        out["layers"] = layers
        hf = (x - x.mean(-1, keepdim=True)) / torch.sqrt(x.var(-1, unbiased=False, keepdim=True) + model.norm.eps)
        hf = hf * model.norm.weight + model.norm.bias
        logits = hf @ model.head.weight.T + model.head.bias
        reference = model(tokens)[0]
        out["check_max_difference"] = _r((reference - logits).abs().max())
        probs = torch.softmax(logits, dim=-1)
        top = torch.topk(probs[-1], k=min(10, probs.shape[-1]))
        out["output"] = {"logits_shape": [T, int(logits.shape[-1])],
                         "top_next": [{"id": int(i), "text": tokenizer.decode([int(i)]), "logit": _r(logits[-1, i]),
                                       "probability": _r(p)} for p, i in zip(top.values, top.indices)],
                         "logsumexp_last": _r(torch.logsumexp(logits[-1], -1))}
        per_position = []
        for t in range(T - 1):
            target = ids[t + 1]
            per_position.append({"position": t, "target": target, "target_text": tokenizer.decode([target]),
                                 "probability": _r(probs[t, target]), "loss": _r(-torch.log(probs[t, target]))})
        out["loss"] = {"per_position": per_position,
                       "mean": _r(sum(p["loss"] for p in per_position) / max(1, len(per_position))),
                       "random_guess": _r(math.log(config["vocabulary"]))}

    # Backward pass: gradients of the loss with respect to every weight.
    model.train()
    model.zero_grad()
    logits = model(tokens[:, :-1])
    loss = nn.functional.cross_entropy(logits.reshape(-1, logits.shape[-1]), tokens[:, 1:].reshape(-1))
    loss.backward()
    grad_norm = math.sqrt(sum(float(p.grad.pow(2).sum()) for p in model.parameters() if p.grad is not None))
    params = []
    for name, p in model.named_parameters():
        g = p.grad if p.grad is not None else torch.zeros_like(p)
        flat, gflat = p.detach().reshape(-1), g.reshape(-1)
        pick = int(gflat.abs().argmax())
        params.append({"name": name, "shape": list(p.shape), "count": p.numel(), "value": _stats(p),
                       "grad_norm": _r(g.norm()), "sample": {"index": pick, "value": _r(flat[pick]), "grad": _r(gflat[pick])}})
    out["parameters"] = params
    out["total_parameters"] = sum(p["count"] for p in params)
    # One AdamW update of the single weight with the biggest gradient, with every number shown.
    biggest = max(params, key=lambda p: abs(p["sample"]["grad"]))
    lr, wd, b1, b2, eps = float(config.get("learning_rate") or 1e-3), float(config.get("weight_decay") or 0.1), 0.9, 0.95, 1e-8
    clip = min(1.0, 1.0 / max(grad_norm, 1e-12))
    g = biggest["sample"]["grad"] * clip
    w = biggest["sample"]["value"]
    m = (1 - b1) * g
    v = (1 - b2) * g * g
    m_hat, v_hat = m / (1 - b1), v / (1 - b2)
    step = lr * m_hat / (math.sqrt(v_hat) + eps)
    decay = lr * wd * w
    out["adamw"] = {"name": biggest["name"], "index": biggest["sample"]["index"], "w": w, "raw_grad": biggest["sample"]["grad"],
                    "grad_norm": _r(grad_norm), "clip_scale": _r(clip), "g": _r(g), "lr": lr, "weight_decay": wd,
                    "beta1": b1, "beta2": b2, "eps": eps, "m": _r(m), "v": _r(v), "m_hat": _r(m_hat), "v_hat": _r(v_hat),
                    "step": _r(step), "decay": _r(decay), "w_new": _r(w - step - decay), "loss": _r(loss)}
    emit("EXPLAIN_JSON " + json.dumps(out))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Train a BPE-token GPT from scratch with PyTorch (portable).")
    commands = parser.add_subparsers(dest="command", required=True)
    train_parser = commands.add_parser("train")
    train_parser.add_argument("--data-file", dest="data_files", action="append", required=True)
    train_parser.add_argument("--app-data", required=True, type=Path)
    train_parser.add_argument("--steps", type=int, default=3000)
    train_parser.add_argument("--batch-size", type=int, default=64)
    train_parser.add_argument("--context", type=int, default=128)
    train_parser.add_argument("--dim", type=int, default=200)
    train_parser.add_argument("--heads", type=int, default=5)
    train_parser.add_argument("--layers", type=int, default=5)
    train_parser.add_argument("--vocab-size", "--vocabulary-size", dest="vocab_size", type=int, default=10_000)
    train_parser.add_argument("--learning-rate", type=float, default=1e-3)
    train_parser.add_argument("--weight-decay", type=float, default=0.1)
    train_parser.add_argument("--warmup", type=int, default=100)
    train_parser.add_argument("--eval-every", type=int, default=250)
    train_parser.add_argument("--seed", type=int, default=17)
    train_parser.add_argument("--device", default="auto", help="auto, cpu, cuda, or mps")
    train_parser.add_argument("--threads", type=int, default=int(os.environ.get("NODEYARD_THREADS", "0")))
    train_parser.add_argument("--assistant-only", action="store_true")
    train_parser.add_argument("--resume", action="store_true")
    train_parser.add_argument("--result-file", default=None)
    train_parser.add_argument("--save-every", type=int, default=500, help="save a checkpoint every N steps (0: only at the end)")
    train_parser.add_argument("--dropout", type=float, default=0.0, help="fraction of values zeroed while training (0-0.5)")
    train_parser.add_argument("--quality", default=None, help="JSON: test prompts and when the model is good enough (see parse_quality)")
    train_parser.add_argument("--lr-progress", nargs=2, type=float, default=None, metavar=("FROM", "TO"),
                              help="follow the cosine schedule from FROM to TO (0..1) of a longer job, with no warm-up")
    train_parser.set_defaults(handler=train)
    average_parser = commands.add_parser("average", help="weighted mean of copies of one model")
    average_parser.add_argument("--out", required=True, type=Path)
    average_parser.add_argument("--part", dest="parts", action="append", required=True, help="PATH:WEIGHT")
    average_parser.set_defaults(handler=average)
    generate_parser = commands.add_parser("generate")
    generate_parser.add_argument("--app-data", required=True, type=Path)
    generate_parser.add_argument("--prompt", default="")
    generate_parser.add_argument("--max-tokens", type=int, default=160)
    generate_parser.add_argument("--temperature", type=float, default=0.8)
    generate_parser.add_argument("--top-k", type=int, default=40)
    generate_parser.add_argument("--top-p", type=float, default=0.95)
    generate_parser.add_argument("--repetition-penalty", type=float, default=1.1)
    generate_parser.add_argument("--seed", type=int, default=21)
    generate_parser.add_argument("--device", default="auto")
    generate_parser.add_argument("--chat-mode", action="store_true")
    generate_parser.set_defaults(handler=generate)
    explain_parser = commands.add_parser("explain", help="print every value of one forward and backward pass as JSON")
    explain_parser.add_argument("--app-data", type=Path, default=None)
    explain_parser.add_argument("--prompt", default="Hello there")
    explain_parser.add_argument("--max-tokens", type=int, default=12)
    explain_parser.add_argument("--show-dims", type=int, default=8)
    explain_parser.set_defaults(handler=explain)
    args, _unknown = parser.parse_known_args()
    return args.handler(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        emit(f"ERROR: {error}")
        raise SystemExit(1)
