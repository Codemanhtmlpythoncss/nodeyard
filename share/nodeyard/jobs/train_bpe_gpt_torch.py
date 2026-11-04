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
import json
import math
import os
import random
import struct
import time
import uuid
from pathlib import Path

import numpy as np
import torch
from torch import nn

from bpe_tokenizer import BPETokenizer
from lr_governor import LearningRateGovernor
from sampling import sample_token

MODEL_FILE = "model.safetensors"
LEGACY_MODEL_FILE = "model.pt"
CONFIG_FILE = "model.json"
TOKENIZER_FILE = "bpe.json"
RESULT_FILE = "result.json"


def emit(message: str) -> None:
    print(message, flush=True)


class MultiHeadAttention(nn.Module):
    """Parameter layout intentionally matches MLX MultiHeadAttention for portable resume."""

    def __init__(self, dim: int, heads: int):
        super().__init__()
        if dim % heads:
            raise ValueError("Embedding width must be divisible by the attention-head count.")
        self.heads = heads
        self.head_dim = dim // heads
        self.query_proj = nn.Linear(dim, dim, bias=False)
        self.key_proj = nn.Linear(dim, dim, bias=False)
        self.value_proj = nn.Linear(dim, dim, bias=False)
        self.out_proj = nn.Linear(dim, dim, bias=False)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        batch, length, dim = x.shape
        def split(layer):
            return layer(x).view(batch, length, self.heads, self.head_dim).transpose(1, 2)
        q, k, v = split(self.query_proj), split(self.key_proj), split(self.value_proj)
        # PyTorch dispatches this to its fused/Flash-SDPA kernel when the device supports it.
        attended = nn.functional.scaled_dot_product_attention(q, k, v, is_causal=True, dropout_p=0.0)
        attended = attended.transpose(1, 2).contiguous().view(batch, length, dim)
        return self.out_proj(attended)


class TransformerEncoderLayer(nn.Module):
    """Pre-norm ReLU block matching mlx.nn.TransformerEncoderLayer defaults."""

    def __init__(self, dim: int, heads: int):
        super().__init__()
        self.attention = MultiHeadAttention(dim, heads)
        self.ln1 = nn.LayerNorm(dim, eps=1e-5)
        self.ln2 = nn.LayerNorm(dim, eps=1e-5)
        self.linear1 = nn.Linear(dim, 4 * dim)
        self.linear2 = nn.Linear(4 * dim, dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + self.attention(self.ln1(x))
        return x + self.linear2(torch.relu(self.linear1(self.ln2(x))))


class TransformerEncoder(nn.Module):
    def __init__(self, dim: int, heads: int, layers: int):
        super().__init__()
        self.layers = nn.ModuleList([TransformerEncoderLayer(dim, heads) for _ in range(layers)])
        self.ln = nn.LayerNorm(dim, eps=1e-5)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        for layer in self.layers:
            x = layer(x)
        return self.ln(x)


class GPT(nn.Module):
    def __init__(self, vocab_size: int, context: int, dim: int, heads: int, layers: int):
        super().__init__()
        self.token_embedding = nn.Embedding(vocab_size, dim)
        self.position_embedding = nn.Embedding(context, dim)
        self.blocks = TransformerEncoder(dim, heads, layers)
        self.norm = nn.LayerNorm(dim, eps=1e-5)
        self.head = nn.Linear(dim, vocab_size)

    def forward(self, tokens: torch.Tensor) -> torch.Tensor:
        length = tokens.shape[1]
        positions = torch.arange(length, device=tokens.device)
        hidden = self.token_embedding(tokens) + self.position_embedding(positions)
        hidden = self.blocks(hidden)
        return self.head(self.norm(hidden))


def load_safetensors(path: Path, device: torch.device) -> dict[str, torch.Tensor]:
    """Read standard F32/F16 safetensors without requiring an extra package."""
    raw = path.read_bytes()
    header_length = struct.unpack("<Q", raw[:8])[0]
    header = json.loads(raw[8:8 + header_length])
    data = memoryview(raw)[8 + header_length:]
    numpy_dtypes = {"F32": np.float32, "F16": np.float16, "I64": np.int64, "I32": np.int32}
    tensors = {}
    for name, metadata in header.items():
        if name == "__metadata__":
            continue
        dtype = numpy_dtypes.get(metadata["dtype"])
        if dtype is None:
            raise ValueError(f"Unsupported checkpoint tensor dtype: {metadata['dtype']}")
        start, end = metadata["data_offsets"]
        array = np.frombuffer(data[start:end], dtype=dtype).reshape(metadata["shape"]).copy()
        tensors[name] = torch.from_numpy(array).to(device)
    return tensors


def save_safetensors(path: Path, tensors: dict[str, torch.Tensor]) -> None:
    """Write F32/F16 PyTorch weights in the safetensors format used by the Mac app."""
    header, chunks, offset = {}, [], 0
    dtype_names = {torch.float32: "F32", torch.float16: "F16", torch.int64: "I64", torch.int32: "I32"}
    for name, tensor in tensors.items():
        cpu = tensor.detach().cpu().contiguous()
        if cpu.dtype not in dtype_names:
            raise ValueError(f"Unsupported checkpoint tensor dtype: {cpu.dtype}")
        chunk = cpu.numpy().tobytes(order="C")
        header[name] = {"dtype": dtype_names[cpu.dtype], "shape": list(cpu.shape),
                        "data_offsets": [offset, offset + len(chunk)]}
        chunks.append(chunk)
        offset += len(chunk)
    encoded = json.dumps(header, separators=(",", ":")).encode("utf-8")
    encoded += b" " * ((8 - len(encoded) % 8) % 8)
    with path.open("wb") as stream:
        stream.write(struct.pack("<Q", len(encoded)))
        stream.write(encoded)
        for chunk in chunks:
            stream.write(chunk)


def pick_device(requested: str) -> torch.device:
    if requested == "gpu":
        requested = "auto"
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
        line_ids = tokenizer.encode(line)
        ids.extend(line_ids)
        weights.extend([0.0 if (assistant_only and in_user_turn) else 1.0] * len(line_ids))
    return np.asarray(ids, dtype=np.int64), np.asarray(weights, dtype=np.float32)


def sample_windows(tokens, weights, context, batch, rng, device):
    starts = np.array([rng.randrange(0, len(tokens) - context - 1) for _ in range(batch)])
    offsets = torch.arange(context + 1, device=device)[None, :]
    starts_tensor = torch.as_tensor(starts, device=device)[:, None]
    block = tokens[starts_tensor + offsets]
    target_weights = weights[starts_tensor + offsets][:, 1:]
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
    texts = []
    for path in args.data_files:
        path = Path(path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Training text does not exist: {path}")
        texts.append(path.read_text(encoding="utf-8", errors="ignore"))
    text = "\n\n".join(texts)
    if len(text) < 1000:
        raise ValueError("Please choose text with at least 1,000 characters.")
    app_data = args.app_data.expanduser()
    app_data.mkdir(parents=True, exist_ok=True)
    emit(f"Device: {device} · threads: {torch.get_num_threads()} · PyTorch {torch.__version__}")

    previous_steps = 0
    tokenizer_path = app_data / TOKENIZER_FILE
    source_dir = Path(args.resume_from).expanduser() if args.resume_from else app_data
    continuing = args.resume or bool(args.resume_from)
    source_config = source_dir / CONFIG_FILE
    source_tokenizer = source_dir / TOKENIZER_FILE
    source_weights = source_dir / MODEL_FILE
    if not source_weights.is_file() and (source_dir / LEGACY_MODEL_FILE).is_file():
        source_weights = source_dir / LEGACY_MODEL_FILE
    if continuing:
        if not (source_config.is_file() and source_tokenizer.is_file() and source_weights.is_file()):
            raise FileNotFoundError(f"Saved BPE checkpoint is incomplete: {source_dir}")
        config = json.loads(source_config.read_text(encoding="utf-8"))
        tokenizer = BPETokenizer.load(source_tokenizer)
        # The checkpoint's tokenizer and architecture are authoritative across backends.
        args.context = int(config["context"])
        args.dim = int(config.get("width", config.get("dim")))
        args.heads = int(config["heads"])
        args.layers = int(config["layers"])
        args.vocab_size = int(config.get("vocabulary", config.get("vocab_size")))
        previous_steps = int(config.get("trained_steps", config.get("steps", 0)))
        emit(f"Continuing from the saved model after {previous_steps:,} training steps (fresh optimizer state).")
    else:
        emit(f"Learning a {args.vocab_size:,}-token BPE vocabulary from {len(text):,} characters...")
        tokenizer = BPETokenizer.train(text, args.vocab_size)
        tokenizer.save(tokenizer_path)

    tokens, weights = encode_with_roles(tokenizer, text, args.assistant_only)
    split = int(len(tokens) * 0.95)
    train_tokens, valid_tokens = tokens[:split], tokens[split:]
    train_weights, valid_weights = weights[:split], weights[split:]
    emit(f"Tokens: {len(tokens):,} ({len(text) / len(tokens):.2f} characters per token); "
         f"{len(train_tokens):,} to train, {len(valid_tokens):,} to validate.")

    model = GPT(tokenizer.vocab_size, args.context, args.dim, args.heads, args.layers).to(device)
    if continuing:
        if source_weights.suffix == ".safetensors":
            model.load_state_dict(load_safetensors(source_weights, device), strict=True)
        else:
            model.load_state_dict(torch.load(source_weights, map_location=device, weights_only=True), strict=True)
    parameters = sum(p.numel() for p in model.parameters())
    emit(f"Model initialized: {parameters:,} trainable parameters; {args.layers} blocks, {args.heads} attention heads, "
         f"width {args.dim}, context {args.context}.")
    train_token_tensor = torch.as_tensor(train_tokens, dtype=torch.long, device=device)
    train_weight_tensor = torch.as_tensor(train_weights, dtype=torch.float32, device=device)
    valid_token_tensor = torch.as_tensor(valid_tokens, dtype=torch.long, device=device)
    valid_weight_tensor = torch.as_tensor(valid_weights, dtype=torch.float32, device=device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate, betas=(0.9, 0.95),
                                  weight_decay=args.weight_decay)
    peak = args.learning_rate
    governor = LearningRateGovernor(args.learning_rate)
    started = time.perf_counter()
    model.train()
    best_state = None
    best_loss = math.inf
    best_step = 0
    worst_state = None
    worst_loss = -math.inf
    worst_step = 0
    for step in range(1, args.steps + 1):
        rate = learning_rate(step, args.steps, peak, args.warmup)
        for group in optimizer.param_groups:
            group["lr"] = rate
        inputs, targets, target_weights = sample_windows(train_token_tensor, train_weight_tensor, args.context,
                                                         args.batch_size, rng, device)
        loss = loss_fn(model, inputs, targets, target_weights)
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
        total_step = previous_steps + step
        if step % 10 == 0 or step == args.steps:
            value = float(loss.item())
            if not math.isfinite(value):
                raise RuntimeError(f"Training diverged at step {step} (loss {value}). Nothing was saved.")
            elapsed = max(1e-9, time.perf_counter() - started)
            emit(f"Step {step}/{args.steps} | train loss {value:.4f} | {step / elapsed:.2f} steps/sec | "
                 f"{step * args.batch_size * args.context / elapsed:,.0f} tokens/sec | "
                 f"total training step {total_step:,} | learning rate {rate:.8g}")
        sync_step = bool(args.sync_every and step % args.sync_every == 0)
        if step % args.eval_every == 0 or step == args.steps or sync_step:
            validation = evaluate(model, valid_token_tensor, valid_weight_tensor, args.context, args.batch_size, device)
            emit(f"Validation at step {step}: loss {validation:.4f} | total training step {total_step:,}")
            if validation < best_loss:
                best_loss, best_step = validation, total_step
                best_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
                emit(f"Best validation snapshot: {best_loss:.4f} at step {best_step}.")
            elif validation > worst_loss:
                worst_loss, worst_step = validation, total_step
                worst_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
            new_rate = governor.observe(validation, peak, total_step, emit)
            if new_rate is not None:
                peak = new_rate
            if sync_step:
                # Save at each population barrier. The runner chooses the lowest
                # validation loss and puts that exact tensor set on every worker.
                save_safetensors(app_data / MODEL_FILE, model.state_dict())
                emit(f"SYNC_WAIT | {total_step} | {validation:.6f} | {peak:.8g}")
                sync_file = app_data / f"sync_{total_step}.json"
                deadline = time.monotonic() + args.sync_timeout
                while not sync_file.is_file():
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for the parallel weight sync at step {total_step}.")
                    time.sleep(0.1)
                sync_info = json.loads(sync_file.read_text(encoding="utf-8"))
                model.load_state_dict(load_safetensors(app_data / MODEL_FILE, device), strict=True)
                best_loss = float(sync_info["best_loss"])
                best_step = int(sync_info["best_step"])
                peak = float(sync_info["learning_rate"])
                best_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
                optimizer = torch.optim.AdamW(model.parameters(), lr=peak, betas=(0.9, 0.95),
                                              weight_decay=args.weight_decay)
                governor.best = best_loss
                governor.stalled = 0
                sync_file.unlink()
                emit(f"SYNC_SELECTED | {total_step} | shared best loss {best_loss:.4f} from step {best_step}; "
                     f"all weights synchronized, optimizer moments reset")

    final_validation = evaluate(model, valid_token_tensor, valid_weight_tensor, args.context, args.batch_size, device, batches=20)
    if best_state is not None and final_validation > best_loss:
        model.load_state_dict(best_state, strict=True)
        emit(f"Restored the best validation weights from step {best_step} ({best_loss:.4f}) instead of regressed final weights.")
        final_validation = best_loss
    save_safetensors(app_data / MODEL_FILE, model.state_dict())
    records_dir = app_data / "records"
    records_dir.mkdir(parents=True, exist_ok=True)
    if best_state is not None:
        save_safetensors(records_dir / "best_params.safetensors", best_state)
    if worst_state is not None:
        save_safetensors(records_dir / "worst_params.safetensors", worst_state)
    (records_dir / "records.json").write_text(json.dumps({
        "best": {"loss": best_loss, "step": best_step},
        "worst": {"loss": worst_loss, "step": worst_step} if worst_state is not None else None,
        "learning_rate_reductions": governor.reductions,
    }, indent=2), encoding="utf-8")
    model_id = uuid.uuid4().hex[:12]
    config = {
        "name": "BPE-token GPT (portable)", "model_id": model_id,
        "model_name": f"BPE-token GPT · model {model_id[:8]}", "tokenizer": "bpe",
        "backend": "portable", "optimizer": "AdamW", "precision": "float32",
        "parameters": parameters, "vocabulary": tokenizer.vocab_size, "layers": args.layers,
        "width": args.dim, "heads": args.heads, "context": args.context, "batch_size": args.batch_size,
        "learning_rate": args.learning_rate, "weight_decay": args.weight_decay,
        "assistant_only": bool(args.assistant_only), "trained_steps": previous_steps + args.steps,
        "validation_loss": final_validation, "compute_device": str(device),
    }
    (app_data / CONFIG_FILE).write_text(json.dumps(config, indent=2), encoding="utf-8")
    if args.result_file:
        Path(args.result_file).write_text(json.dumps({"validation_loss": final_validation,
                                                      "total_steps": previous_steps + args.steps}), encoding="utf-8")
    emit(f"Saved checkpoint after {previous_steps + args.steps:,} total training steps to {app_data / MODEL_FILE}")
    emit(f"Training finished. Final validation loss {final_validation:.4f}.")
    return 0


@torch.no_grad()
def generate(args) -> int:
    app_data = args.app_data.expanduser()
    config = json.loads((app_data / CONFIG_FILE).read_text(encoding="utf-8"))
    device = pick_device(args.device)
    tokenizer = BPETokenizer.load(app_data / TOKENIZER_FILE)
    model = GPT(config["vocabulary"], config["context"], config["width"], config["heads"], config["layers"]).to(device)
    weights = app_data / MODEL_FILE
    state = load_safetensors(weights, device) if weights.suffix == ".safetensors" else \
        torch.load(weights, map_location=device, weights_only=True)
    model.load_state_dict(state, strict=True)
    model.eval()
    rng = np.random.default_rng(args.seed)
    prompt_ids = tokenizer.encode(args.prompt) or [ord("\n")]
    generated: list[int] = []
    for step in range(1, args.max_tokens + 1):
        context_ids = (prompt_ids + generated)[-config["context"]:]
        logits = model(torch.as_tensor([context_ids], device=device))[0, -1].float().cpu().numpy()
        generated.append(sample_token(logits, rng, recent=generated[-64:], temperature=args.temperature,
                                      top_k=args.top_k, top_p=args.top_p, repetition_penalty=args.repetition_penalty))
        if args.chat_mode and step % 16 == 0 and "\nUser:" in tokenizer.decode(generated):
            break
    reply = tokenizer.decode(generated).split("\nUser:")[0].rstrip()
    if args.chat_mode:
        emit("CHAT_REPLY_BEGIN")
        emit(reply)
        emit("CHAT_REPLY_END")
    else:
        emit(f"Prompt: {args.prompt}")
        emit("Generated continuation:")
        emit(reply)
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
    train_parser.add_argument("--resume-from", type=Path, default=None,
                              help="resume from an MLX/PyTorch BPE model directory")
    train_parser.add_argument("--result-file", default=None)
    train_parser.add_argument("--sync-every", type=int, default=0,
                              help="synchronize all parallel copies to the best validation weights every N steps")
    train_parser.add_argument("--sync-timeout", type=int, default=900)
    train_parser.set_defaults(handler=train)
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
    args, _unknown = parser.parse_known_args()
    return args.handler(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        emit(f"ERROR: {error}")
        raise SystemExit(1)
