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
import signal
import shutil
import struct
import time
import uuid
from datetime import timedelta
from pathlib import Path

import numpy as np
import torch
import torch.distributed as dist
from torch import nn

from bpe_tokenizer import BPETokenizer
from lr_governor import LearningRateGovernor
from sampling import sample_token
from training_data import iter_training_text

MODEL_FILE = "model.safetensors"
LEGACY_MODEL_FILE = "model.pt"
CONFIG_FILE = "model.json"
TOKENIZER_FILE = "bpe.json"
RESULT_FILE = "result.json"
_PAUSE_REQUESTED = False


def emit(message: str) -> None:
    print(message, flush=True)


def _request_pause(_signum, _frame) -> None:
    global _PAUSE_REQUESTED
    _PAUSE_REQUESTED = True


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


def allreduce_gradients(model: nn.Module, world_size: int) -> None:
    """Average one flattened CPU gradient buffer across heterogeneous CPU/CUDA ranks."""
    parameters = [parameter for parameter in model.parameters() if parameter.grad is not None]
    flat = torch.cat([parameter.grad.detach().to("cpu").contiguous().view(-1) for parameter in parameters])
    dist.all_reduce(flat, op=dist.ReduceOp.SUM)
    flat.div_(world_size)
    offset = 0
    for parameter in parameters:
        count = parameter.numel()
        parameter.grad.copy_(flat[offset:offset + count].view_as(parameter).to(parameter.device))
        offset += count


def allreduce_scalar(value: float, device: torch.device, world_size: int) -> float:
    tensor = torch.tensor([value], dtype=torch.float64, device="cpu")
    dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
    return float(tensor.item() / world_size)


def learning_rate(step: int, total: int, peak: float, warmup: int) -> float:
    if step <= warmup:
        return peak * step / max(1, warmup)
    progress = (step - warmup) / max(1, total - warmup)
    return peak * (0.1 + 0.9 * 0.5 * (1 + math.cos(math.pi * min(1.0, progress))))


@torch.no_grad()
def evaluate(model, tokens, weights, context, batch, device, batches=10, seed=1234) -> float:
    rng = random.Random(seed)
    model.eval()
    total = 0.0
    for _ in range(batches):
        inputs, targets, target_weights = sample_windows(tokens, weights, context, batch, rng, device)
        total += float(loss_fn(model, inputs, targets, target_weights).item())
    model.train()
    return total / batches


def archive_existing(app_data: Path) -> None:
    """Keep the previous current BPE model in its history folder before a fresh run."""
    config_path = app_data / CONFIG_FILE
    if not (app_data / MODEL_FILE).is_file() or not config_path.is_file():
        return
    old = json.loads(config_path.read_text(encoding="utf-8"))
    model_id = str(old.get("model_id") or uuid.uuid4().hex[:12])
    archive = app_data / "history" / f"model_{model_id}"
    if archive.exists():
        return
    archive.mkdir(parents=True, exist_ok=True)
    for name in (MODEL_FILE, CONFIG_FILE, TOKENIZER_FILE):
        if (app_data / name).is_file():
            shutil.copy2(app_data / name, archive / name)


def train(args) -> int:
    global _PAUSE_REQUESTED
    _PAUSE_REQUESTED = False
    signal.signal(signal.SIGUSR1, _request_pause)
    rank = int(os.environ.get("RANK", args.rank))
    world_size = int(os.environ.get("WORLD_SIZE", args.world_size))
    args.master_addr = os.environ.get("MASTER_ADDR", args.master_addr)
    args.master_port = int(os.environ.get("MASTER_PORT", args.master_port))
    distributed = world_size > 1
    if distributed:
        args.sync_every = 0  # distributed ranks synchronize gradients every update
        dist.init_process_group(backend="gloo", init_method=f"tcp://{args.master_addr}:{args.master_port}",
                               rank=rank, world_size=world_size,
                               timeout=timedelta(seconds=args.distributed_timeout))
    report = emit if rank == 0 else (lambda _message: None)
    torch.manual_seed(args.seed)
    if args.threads:
        torch.set_num_threads(args.threads)
    device = pick_device(args.device)
    rng = random.Random(args.seed + rank)
    texts = []
    for path in args.data_files:
        path = Path(path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Training text does not exist: {path}")
        # BPE must learn and train on the record content, not JSON keys/braces.
        if path.suffix.lower() == ".jsonl":
            report(f"Normalizing JSONL records from {path.name}: extracting speaker/text and chat content before BPE.")
        texts.append("".join(iter_training_text(path)))
    text = "\n\n".join(texts)
    if len(text) < 1000:
        raise ValueError("Please choose text with at least 1,000 characters.")
    app_data = args.app_data.expanduser()
    app_data.mkdir(parents=True, exist_ok=True)
    report(f"Device: {device} · threads: {torch.get_num_threads()} · PyTorch {torch.__version__}")
    if distributed and rank == 0:
        report(f"Distributed training: {world_size} ranks, Gloo all-reduce over Tailscale; per-rank batch {args.batch_size}.")

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
        report(f"Continuing from the saved model after {previous_steps:,} training steps (fresh optimizer state).")
    else:
        report(f"Learning a {args.vocab_size:,}-token BPE vocabulary from {len(text):,} characters...")
        archive_existing(app_data)
        tokenizer = BPETokenizer.train(text, args.vocab_size)
        tokenizer.save(tokenizer_path)

    tokens, weights = encode_with_roles(tokenizer, text, args.assistant_only)
    split = int(len(tokens) * 0.95)
    train_tokens, valid_tokens = tokens[:split], tokens[split:]
    train_weights, valid_weights = weights[:split], weights[split:]
    report(f"Tokens: {len(tokens):,} ({len(text) / len(tokens):.2f} characters per token); "
           f"{len(train_tokens):,} to train, {len(valid_tokens):,} to validate.")

    model = GPT(tokenizer.vocab_size, args.context, args.dim, args.heads, args.layers).to(device)
    if continuing:
        if source_weights.suffix == ".safetensors":
            model.load_state_dict(load_safetensors(source_weights, device), strict=True)
        else:
            model.load_state_dict(torch.load(source_weights, map_location=device, weights_only=True), strict=True)
    parameters = sum(p.numel() for p in model.parameters())
    report(f"Model initialized: {parameters:,} trainable parameters; {args.layers} blocks, {args.heads} attention heads, "
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
    completed_steps = 0
    paused = False
    reached_target = False
    for step in range(1, args.steps + 1):
        rate = learning_rate(step, args.steps, peak, args.warmup)
        for group in optimizer.param_groups:
            group["lr"] = rate
        inputs, targets, target_weights = sample_windows(train_token_tensor, train_weight_tensor, args.context,
                                                         args.batch_size, rng, device)
        loss = loss_fn(model, inputs, targets, target_weights)
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        if distributed:
            allreduce_gradients(model, world_size)
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
        total_step = previous_steps + step
        value = float(loss.detach().cpu())
        if distributed:
            value = allreduce_scalar(value, device, world_size)
        if not math.isfinite(value):
            raise RuntimeError(f"Training diverged at step {step} (loss {value}). Nothing was saved.")
        elapsed = max(1e-9, time.perf_counter() - started)
        report(f"Step {step}/{args.steps} | train loss {value:.4f} | {step / elapsed:.2f} steps/sec | "
               f"{step * args.batch_size * args.context * world_size / elapsed:,.0f} tokens/sec | "
               f"total training step {total_step:,} | learning rate {rate:.8g}")
        sync_step = bool(args.sync_every and step % args.sync_every == 0)
        if step % args.eval_every == 0 or step == args.steps or sync_step:
            validation = evaluate(model, valid_token_tensor, valid_weight_tensor, args.context, args.batch_size,
                                  device, batches=(2 if (args.rollback_on_regression or sync_step) else 10),
                                  seed=1234 + rank)
            if distributed:
                validation = allreduce_scalar(validation, device, world_size)
            report(f"Validation at step {step}: loss {validation:.4f} | total training step {total_step:,}")
            if validation < best_loss:
                best_loss, best_step = validation, total_step
                best_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
                report(f"Best validation snapshot: {best_loss:.4f} at step {best_step}.")
            elif validation > worst_loss:
                worst_loss, worst_step = validation, total_step
                worst_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
            if args.rollback_on_regression and best_state is not None and validation >= best_loss:
                # Validation is the held-out progress signal. On any non-improving
                # check, undo the step window and restart AdamW's moments at a lower rate.
                if validation > best_loss or total_step > best_step:
                    model.load_state_dict(best_state, strict=True)
                    mx_rate = max(args.learning_rate * 0.02, peak * 0.7)
                    peak = mx_rate
                    optimizer = torch.optim.AdamW(model.parameters(), lr=peak, betas=(0.9, 0.95),
                                                  weight_decay=args.weight_decay)
                    report(f"ROLLBACK | {total_step} | validation did not improve; restored step {best_step}, "
                           f"reset AdamW and lowered learning rate to {peak:.8g}")
            if args.target_validation_loss > 0 and validation <= args.target_validation_loss:
                reached_target = True
                report(f"TARGET_LOSS_REACHED | {total_step} | validation {validation:.4f} <= "
                       f"target {args.target_validation_loss:.4f}; saving this model and stopping.")
            new_rate = governor.observe(validation, peak, total_step, report)
            if new_rate is not None:
                peak = new_rate
        if sync_step:
            pause_sync_file = app_data / "pause.sync"
            if pause_sync_file.exists():
                _PAUSE_REQUESTED = True
                pause_sync_file.unlink(missing_ok=True)
            else:
                # Save at each population barrier. The runner chooses the lowest
                # validation loss and puts that exact tensor set on every worker.
                save_safetensors(app_data / MODEL_FILE, model.state_dict())
                report(f"SYNC_WAIT | {total_step} | {validation:.6f} | {peak:.8g}")
                sync_file = app_data / f"sync_{total_step}.json"
                deadline = time.monotonic() + args.sync_timeout
                while not sync_file.is_file() and not pause_sync_file.is_file():
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for the parallel weight sync at step {total_step}.")
                    time.sleep(0.1)
                if pause_sync_file.is_file():
                    _PAUSE_REQUESTED = True
                    pause_sync_file.unlink(missing_ok=True)
                else:
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
                    reached_target = bool(sync_info.get("stop", False))
                    report(f"SYNC_SELECTED | {total_step} | shared best loss {best_loss:.4f} from step {best_step}; "
                           f"all weights synchronized, optimizer moments reset")
        completed_steps = step
        pause = _PAUSE_REQUESTED
        if distributed:
            pause_tensor = torch.tensor([1 if pause else 0], dtype=torch.int32)
            dist.all_reduce(pause_tensor, op=dist.ReduceOp.MAX)
            pause = bool(pause_tensor.item())
        if pause:
            paused = True
            report(f"Pause requested; checkpointing after step {total_step}.")
            break
        if reached_target:
            break

    final_validation = evaluate(model, valid_token_tensor, valid_weight_tensor, args.context, args.batch_size,
                               device, batches=20, seed=9000 + rank)
    if distributed:
        final_validation = allreduce_scalar(final_validation, device, world_size)
    if best_state is not None and final_validation > best_loss:
        model.load_state_dict(best_state, strict=True)
        report(f"Restored the best validation weights from step {best_step} ({best_loss:.4f}) instead of regressed final weights.")
        final_validation = best_loss
    if distributed:
        dist.barrier()
    if rank == 0:
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
            "distributed_world_size": world_size,
        }, indent=2), encoding="utf-8")
        model_id = uuid.uuid4().hex[:12]
        config = {
            "name": "BPE-token GPT (portable)", "model_id": model_id,
            "model_name": f"BPE-token GPT · model {model_id[:8]}", "tokenizer": "bpe",
            "backend": "portable", "optimizer": "AdamW", "precision": "float32",
            "parameters": parameters, "vocabulary": tokenizer.vocab_size, "layers": args.layers,
            "width": args.dim, "heads": args.heads, "context": args.context, "batch_size": args.batch_size,
            "learning_rate": peak, "weight_decay": args.weight_decay,
            "assistant_only": bool(args.assistant_only), "trained_steps": previous_steps + completed_steps,
            "training_data_format": "JSONL normalized to role-prefixed text" if any(Path(p).suffix.lower() == ".jsonl" for p in args.data_files) else "plain text",
            "validation_loss": final_validation, "compute_device": str(device),
            "distributed_world_size": world_size,
        }
        (app_data / CONFIG_FILE).write_text(json.dumps(config, indent=2), encoding="utf-8")
        if args.result_file:
            Path(args.result_file).write_text(json.dumps({"validation_loss": final_validation,
                                                          "total_steps": previous_steps + completed_steps}), encoding="utf-8")
        report(f"Saved checkpoint after {previous_steps + completed_steps:,} total training steps to {app_data / MODEL_FILE}")
        report((f"Training paused. Checkpoint saved at step {previous_steps + completed_steps}."
                if paused else f"Training finished. Final validation loss {final_validation:.4f}."))
    else:
        (app_data / f"rank_{rank}.done.json").write_text(json.dumps({"rank": rank, "steps": completed_steps}), encoding="utf-8")
    if distributed:
        dist.barrier()
        dist.destroy_process_group()
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
    if reply.startswith("Assistant:"):
        reply = reply[len("Assistant:"):].lstrip()
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
    train_parser.add_argument("--rank", type=int, default=0)
    train_parser.add_argument("--world-size", type=int, default=1)
    train_parser.add_argument("--master-addr", default="127.0.0.1")
    train_parser.add_argument("--master-port", type=int, default=29500)
    train_parser.add_argument("--distributed-timeout", type=int, default=900)
    train_parser.add_argument("--assistant-only", action="store_true")
    train_parser.add_argument("--rollback-on-regression", action="store_true",
                              help="restore the best validation weights whenever the next validation does not improve")
    train_parser.add_argument("--resume", action="store_true")
    train_parser.add_argument("--resume-from", type=Path, default=None,
                              help="resume from an MLX/PyTorch BPE model directory")
    train_parser.add_argument("--result-file", default=None)
    train_parser.add_argument("--sync-every", type=int, default=0,
                              help="synchronize all parallel copies to the best validation weights every N steps")
    train_parser.add_argument("--sync-timeout", type=int, default=900)
    train_parser.add_argument("--target-validation-loss", type=float, default=3.5,
                              help="save and stop once held-out loss reaches this value (0 disables)")
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
