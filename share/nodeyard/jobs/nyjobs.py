#!/usr/bin/env python3
"""nodeyard jobs: train the same model on several machines over Tailscale, or locally.

Each training copy is a separate process. A copy goes to the machine with the most
free capacity, and a new copy starts as soon as a slot frees up, so the hardware is
the limit. When every copy has finished, the one with the lowest validation loss is
kept and the others are recorded in results.json.

Capacity: a machine runs as many copies as it has CPUs (one thread each, leaving one
CPU for the system when it has more than two) and as many as its available memory
allows at --mb-per-copy. Both numbers come from `probe`, or can be set by hand.

Transport: SSH over Tailscale (set NYJOBS_TRANSPORT=local to simulate hosts in
folders, for tests). Nothing is installed on a machine by this tool: it needs a
Python 3 with torch and numpy, and SSH key access (or Tailscale SSH).

Commands:
  nyjobs hosts                         list configured machines and their capacity
  nyjobs add-host NAME ADDRESS [--user U] [--mem-mb-per-copy N]
  nyjobs remove-host NAME
  nyjobs probe                        read-only check of every machine (arch, CPUs, memory, torch)
  nyjobs plan --copies N [--local]    show where copies would run, without running them
  nyjobs run --data FILE --copies N [--steps S] [--local] [--name JOB] [...training options]
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import sys
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRAINER = HERE / "train_bpe_gpt_torch.py"
SUPPORT_FILES = [HERE / "bpe_tokenizer.py", HERE / "sampling.py", HERE / "training_data.py"]
CONFIG_PATH = Path(os.environ.get("NODEYARD_JOBS_CONFIG", "~/.config/nodeyard/jobs.json")).expanduser()
JOBS_ROOT = Path(os.environ.get("NODEYARD_JOBS_OUTPUT", "~/nodeyard-jobs")).expanduser()
REMOTE_ROOT = "nodeyard-jobs"
DEFAULT_MB_PER_COPY = 900  # conservative: the 6.4M-weight model, its optimizer state, and activations
RESERVE_MB = 1024
POLL_SECONDS = 2
LR_GOVERNOR = HERE / "lr_governor.py"
MASTER_ACCESS_SCRIPT = HERE / "training-access.sh"


@dataclass
class Host:
    name: str
    address: str = ""
    user: str = ""
    port: int = 22
    python: str = ""
    local: bool = False
    cpus: int = 0
    mem_mb: int = 0
    arch: str = ""
    torch: str = ""
    gpu_count: int = 0
    gpu_name: str = ""
    cuda_ready: bool = False
    latency_ms: float = 0.0
    tokens_per_second: float = 0.0
    mb_per_copy: int = DEFAULT_MB_PER_COPY
    probed: bool = False
    limit: int = 0  # parallel copies chosen for this device; 0 = automatic

    @property
    def slots(self) -> int:
        if self.limit > 0:
            return self.limit
        if not self.probed or self.cpus <= 0:
            return 0
        cpu_slots = self.cpus - 1 if self.cpus > 2 else self.cpus
        mem_slots = max(0, (self.mem_mb - RESERVE_MB) // self.mb_per_copy)
        return max(0, min(cpu_slots + (self.gpu_count if self.cuda_ready else 0), mem_slots))

    @property
    def threads_per_copy(self) -> int:
        return max(1, (self.cpus - 1 if self.cpus > 2 else self.cpus) // max(1, self.slots))


# ---------------------------------------------------------------- configuration

def load_hosts() -> list[Host]:
    if not CONFIG_PATH.is_file():
        return []
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    hosts = []
    for entry in data.get("hosts", []):
        hosts.append(Host(name=entry["name"], address=entry.get("address", ""), user=entry.get("user", ""),
                          port=int(entry.get("port", 22)), python=entry.get("python", ""),
                          mb_per_copy=int(entry.get("mb_per_copy", DEFAULT_MB_PER_COPY)),
                          cpus=int(entry.get("cpus", 0)), mem_mb=int(entry.get("mem_mb", 0)),
                          arch=entry.get("arch", ""), torch=entry.get("torch", ""),
                          gpu_count=int(entry.get("gpu_count", 0)), gpu_name=entry.get("gpu_name", ""),
                          cuda_ready=bool(entry.get("cuda_ready", False)),
                          latency_ms=float(entry.get("latency_ms", 0.0)),
                          tokens_per_second=float(entry.get("tokens_per_second", 0.0)),
                          probed=bool(entry.get("probed", False)), limit=int(entry.get("limit", 0))))
    return hosts


def save_hosts(hosts: list[Host]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"hosts": [{"name": h.name, "address": h.address, "user": h.user, "port": h.port, "python": h.python,
                          "mb_per_copy": h.mb_per_copy,
                          "cpus": h.cpus, "mem_mb": h.mem_mb, "arch": h.arch, "torch": h.torch,
                          "gpu_count": h.gpu_count, "gpu_name": h.gpu_name,
                          "cuda_ready": h.cuda_ready,
                          "latency_ms": h.latency_ms, "tokens_per_second": h.tokens_per_second,
                          "probed": h.probed, "limit": h.limit} for h in hosts]}
    CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def merge_hosts(updated_hosts: list[Host]) -> None:
    """Persist refreshed device facts without dropping the master discovery alias."""
    merged = {host.name: host for host in load_hosts()}
    merged.update({host.name: host for host in updated_hosts})
    save_hosts(list(merged.values()))


def this_machine() -> Host:
    cpus = os.cpu_count() or 1
    mem_mb = 0
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip()
        mem_mb = int(out) // (1024 * 1024) if out.isdigit() else 0
    except OSError:
        pass
    if not mem_mb and Path("/proc/meminfo").exists():
        match = re.search(r"MemAvailable:\s+(\d+)", Path("/proc/meminfo").read_text())
        mem_mb = int(match.group(1)) // 1024 if match else 0
    gpu_count, gpu_name, device = 0, "", "cpu"
    python = os.environ.get("NYJOBS_PYTHON", sys.executable)
    try:
        result = subprocess.run([python, "-c", "import torch; "
                                "print('mps' if getattr(torch.backends,'mps',None) and torch.backends.mps.is_available() else ('cuda' if torch.cuda.is_available() else 'cpu'))"],
                               capture_output=True, text=True, timeout=8)
        device = result.stdout.strip()
        if device in ("mps", "cuda"):
            gpu_count, gpu_name = 1, ("Apple MPS" if device == "mps" else "CUDA")
    except (OSError, subprocess.SubprocessError):
        pass
    host = Host(name="this-machine", local=True, cpus=cpus, mem_mb=mem_mb, arch=os.uname().machine,
                gpu_count=gpu_count, gpu_name=gpu_name, cuda_ready=gpu_count > 0, probed=True)
    return host


# ---------------------------------------------------------------- transport

class SSHTransport:
    """Key-only SSH over Tailscale. BatchMode stops any password prompt, so a missing key fails at once.
    NYJOBS_IDENTITY, when set, is the private key to use (the Tutor app keeps its own key)."""

    def __init__(self, host: Host):
        self.target = f"{host.user}@{host.address}" if host.user else host.address
        self.port = host.port
        identity = os.environ.get("NYJOBS_IDENTITY", "")
        known_hosts = CONFIG_PATH.with_name("known_hosts")
        self.options = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                        "-o", f"UserKnownHostsFile={known_hosts}",
                        "-o", "StrictHostKeyChecking=accept-new"]
        if identity:
            self.options += ["-i", identity, "-o", "IdentitiesOnly=yes"]

    def run(self, command: str, timeout: int = 60, stdin_text: str | None = None) -> tuple[int, str]:
        result = subprocess.run(["ssh", *self.options, "-p", str(self.port), self.target, command],
                                capture_output=True, text=True, timeout=timeout, input=stdin_text)
        return result.returncode, result.stdout + result.stderr

    def put(self, local: Path, remote: str) -> None:
        last = None
        for attempt in range(4):
            try:
                subprocess.run(["scp", "-q", *self.options, "-P", str(self.port), str(local), f"{self.target}:{remote}"],
                               check=True, timeout=90)
                return
            except (subprocess.SubprocessError, OSError) as error:
                last = error
                time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(f"SCP to {self.target} failed after retries: {last}")

    def get(self, remote: str, local: Path) -> None:
        last = None
        for attempt in range(4):
            try:
                subprocess.run(["scp", "-q", "-r", *self.options, "-P", str(self.port), f"{self.target}:{remote}", str(local)],
                               check=True, timeout=90)
                return
            except (subprocess.SubprocessError, OSError) as error:
                last = error
                time.sleep(min(2 ** attempt, 8))
        raise RuntimeError(f"SCP from {self.target} failed after retries: {last}")


class LocalTransport:
    """Simulates a machine in a folder (NYJOBS_TRANSPORT=local). Used for tests and for this Mac."""

    def __init__(self, host: Host):
        self.root = Path(os.environ.get("NYJOBS_SIM_ROOT", "/tmp/nyjobs-sim")) / host.name
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, remote: str) -> Path:
        return self.root / remote.lstrip("~/")

    def run(self, command: str, timeout: int = 60, stdin_text: str | None = None) -> tuple[int, str]:
        result = subprocess.run(command, shell=True, cwd=self.root, capture_output=True, text=True, timeout=timeout,
                                input=stdin_text)
        return result.returncode, result.stdout + result.stderr

    def put(self, local: Path, remote: str) -> None:
        target = self._path(remote)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, target)

    def get(self, remote: str, local: Path) -> None:
        source = self._path(remote)
        if source.is_dir():
            shutil.copytree(source, local, dirs_exist_ok=True)
        else:
            local.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, local)


def transport_for(host: Host):
    if host.local or os.environ.get("NYJOBS_TRANSPORT") == "local":
        return LocalTransport(host)
    return SSHTransport(host)


# ---------------------------------------------------------------- probing

def probe_host(host: Host) -> Host:
    if host.local:
        return this_machine()
    transport = transport_for(host)
    python = shlex.quote(host.python or "python3")
    code, out = transport.run(
        "echo arch=$(uname -m); echo cpus=$(nproc 2>/dev/null || sysctl -n hw.ncpu); "
        "echo mem_mb=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo 2>/dev/null || echo $(( $(sysctl -n hw.memsize) / 1048576 ))); "
        "echo gpu_count=$(nvidia-smi -L 2>/dev/null | wc -l | tr -d ' ' || echo 0); "
        "echo gpu_name=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n 1 || echo none); "
        f"echo torch=$({python} -c 'import torch;print(torch.__version__)' 2>/dev/null || echo none)"
        f"; echo cuda=$({python} -c 'import torch;print(int(torch.cuda.is_available()))' 2>/dev/null || echo 0)"
    )
    fields = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if code != 0 or "arch" not in fields:
        host.probed = False
        host.arch = ""
        return host
    host.arch = fields["arch"].strip()
    host.cpus = int(fields.get("cpus", "0") or 0)
    host.mem_mb = int(fields.get("mem_mb", "0") or 0)
    host.torch = fields.get("torch", "none").strip()
    host.gpu_count = int(fields.get("gpu_count", "0") or 0)
    host.gpu_name = fields.get("gpu_name", "none").strip()
    host.cuda_ready = fields.get("cuda", "0").strip() == "1"
    try:
        started = time.perf_counter()
        ping = subprocess.run(["ping", "-c", "1", host.address], capture_output=True, text=True, timeout=4, check=False)
        match = re.search(r"time[=<]([0-9.]+)\s*ms", ping.stdout + ping.stderr)
        host.latency_ms = float(match.group(1)) if match else (time.perf_counter() - started) * 1000.0
    except (OSError, subprocess.SubprocessError):
        host.latency_ms = 0.0
    host.probed = host.torch != "none"
    return host


# ---------------------------------------------------------------- scheduling

@dataclass
class Assignment:
    copy: int
    host: Host


class Scheduler:
    """Hands copies to the machine with the most free slots; a copy waits until a slot frees."""

    def __init__(self, hosts: list[Host]):
        self.hosts = [h for h in hosts if h.slots > 0]
        self.free = {h.name: h.slots for h in self.hosts}
        self.free_gpus = {h.name: h.gpu_count if h.cuda_ready else 0 for h in self.hosts}
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)

    def acquire(self) -> tuple[Host, str]:
        with self.changed:
            while True:
                candidates = [h for h in self.hosts if self.free[h.name] > 0]
                if candidates:
                    host = max(candidates, key=lambda h: (
                        (h.tokens_per_second or (12000 if h.gpu_count and h.cuda_ready else h.cpus * 500)) /
                        (1.0 + max(0.0, h.latency_ms) / 50.0),
                        self.free[h.name], h.slots))
                    self.free[host.name] -= 1
                    if host.local and self.free_gpus[host.name] > 0:
                        self.free_gpus[host.name] -= 1
                        return host, "mps" if host.gpu_name == "Apple MPS" else "cuda:0"
                    if self.free_gpus[host.name] > 0:
                        self.free_gpus[host.name] -= 1
                        return host, "cuda:0"
                    return host, "cpu"
                if not self.hosts:
                    raise RuntimeError("No machine has capacity. Run `nyjobs probe` or add a machine.")
                self.changed.wait()

    def release(self, host: Host, device: str) -> None:
        with self.changed:
            self.free[host.name] += 1
            if device != "cpu":
                self.free_gpus[host.name] += 1
            self.changed.notify_all()


def plan(hosts: list[Host], copies: int) -> list[Assignment]:
    """Static plan: how copies would be spread at full capacity, most capacity first."""
    scheduler = Scheduler(hosts)
    assignments = []
    for index in range(1, copies + 1):
        candidates = [h for h in scheduler.hosts if scheduler.free[h.name] > 0]
        if not candidates:
            break
        host = max(candidates, key=lambda h: (
            (h.tokens_per_second or (12000 if h.cuda_ready else h.cpus * 500)) /
            (1.0 + max(0.0, h.latency_ms) / 50.0), scheduler.free[h.name], h.slots))
        scheduler.free[host.name] -= 1
        assignments.append(Assignment(copy=index, host=host))
    return assignments


# ---------------------------------------------------------------- running copies

def train_arguments(args, copy_index: int, device: str = "auto") -> list[str]:
    return ["--steps", str(args.steps), "--batch-size", str(args.batch_size), "--context", str(args.context),
            "--dim", str(args.dim), "--heads", str(args.heads), "--layers", str(args.layers),
            "--vocab-size", str(args.vocab_size), "--learning-rate", str(args.learning_rate),
            "--weight-decay", str(args.weight_decay), "--warmup", str(args.warmup),
            "--eval-every", str(args.eval_every), "--sync-every", str(args.sync_every),
            "--target-validation-loss", str(args.target_validation_loss), "--device", device,
            "--seed", str(args.seed + copy_index)] \
        + (["--assistant-only"] if args.assistant_only else []) \
        + (["--rollback-on-regression"] if args.rollback_on_regression else []) \
        + (["--rollback-step"] if args.rollback_step else [])


def _checkpoint_files(directory: Path) -> list[Path]:
    return [directory / name for name in ("model.safetensors", "model.json", "bpe.json")
            if (directory / name).is_file()]


def _install_best(source: Path, destination: Path) -> None:
    """Archive the old current BPE model, then atomically replace its shared files."""
    config_path = destination / "model.json"
    if config_path.is_file() and (destination / "model.safetensors").is_file():
        old = json.loads(config_path.read_text(encoding="utf-8"))
        model_id = str(old.get("model_id") or uuid.uuid4().hex[:12])
        archive = destination / "history" / f"model_{model_id}"
        if not archive.exists():
            archive.mkdir(parents=True, exist_ok=True)
            for file in _checkpoint_files(destination):
                shutil.copy2(file, archive / file.name)
    destination.mkdir(parents=True, exist_ok=True)
    for file in _checkpoint_files(source):
        temporary = destination / (file.name + ".remote-tmp")
        shutil.copy2(file, temporary)
        temporary.replace(destination / file.name)


class ParallelWeightSync:
    """At each barrier, broadcast the copy with the lowest validation loss."""

    def __init__(self, expected: int, job_dir: Path, log, target_loss: float = 0.0):
        self.expected = expected
        self.job_dir = job_dir
        self.log = log
        self.target_loss = target_loss
        self.condition = threading.Condition()
        self.pending: dict[int, list[dict]] = {}
        self.completed: dict[int, dict] = {}
        self.error: str | None = None

    def abort(self, reason: str) -> None:
        with self.condition:
            self.error = reason
            self.condition.notify_all()

    def arrive(self, copy_index: int, step: int, loss: float, learning_rate: float,
               host: Host, transport, remote_dir: str, model_dir: Path) -> dict:
        checkpoint = self.job_dir / "sync" / str(step) / f"copy_{copy_index}.safetensors"
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        if host.local:
            shutil.copy2(model_dir / "model.safetensors", checkpoint)
        else:
            transport.get(f"{remote_dir}/model/model.safetensors", checkpoint)
        entry = {"copy": copy_index, "loss": loss, "learning_rate": learning_rate,
                 "host": host, "transport": transport, "remote_dir": remote_dir,
                 "model_dir": model_dir, "checkpoint": checkpoint}
        with self.condition:
            if self.error:
                raise RuntimeError(self.error)
            entries = self.pending.setdefault(step, [])
            entries.append(entry)
            if len(entries) == self.expected:
                winner = min(entries, key=lambda item: item["loss"])
                info = {"best_loss": winner["loss"], "best_step": step,
                        "learning_rate": min(item["learning_rate"] for item in entries),
                        "winner": winner["copy"],
                        "stop": bool(self.target_loss > 0 and winner["loss"] <= self.target_loss)}
                marker = json.dumps(info, separators=(",", ":"))
                for item in entries:
                    if item["host"].local:
                        target = item["model_dir"] / "model.safetensors"
                        if target.resolve() != winner["checkpoint"].resolve():
                            shutil.copy2(winner["checkpoint"], target)
                        (item["model_dir"] / f"sync_{step}.json").write_text(marker, encoding="utf-8")
                    else:
                        item["transport"].put(winner["checkpoint"],
                                              f"{item['remote_dir']}/model/model.safetensors")
                        code, output = item["transport"].run(
                            f"printf %s {shlex.quote(marker)} > {shlex.quote(item['remote_dir'] + f'/model/sync_{step}.json')}",
                            timeout=30)
                        if code:
                            raise RuntimeError(f"Could not release synchronized copy {item['copy']}: {output[-160:]}")
                self.completed[step] = info
                self.log(f"Step {step}: copy {winner['copy']} had the best validation loss ({winner['loss']:.4f}); "
                         f"broadcasting its weights to all {self.expected} copies.")
                self.condition.notify_all()
            deadline = time.monotonic() + 900
            while step not in self.completed and not self.error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.error = f"Timed out waiting for all parallel copies at sync step {step}."
                    self.condition.notify_all()
                    break
                self.condition.wait(timeout=min(remaining, 10))
            if self.error:
                raise RuntimeError(self.error)
            return self.completed[step]


def run_copy_on(host: Host, copy_index: int, args, data: Path, job_dir: Path, log,
                sync: ParallelWeightSync | None = None, device: str = "cpu") -> dict:
    if host.local:
        return run_copy_locally(copy_index, args, data, job_dir, host.threads_per_copy, log, sync, device)
    transport = transport_for(host)
    remote_dir = f"{REMOTE_ROOT}/{args.job}/copy_{copy_index}"
    threads = host.threads_per_copy
    python = os.environ.get("NYJOBS_PYTHON", host.python or "python3")
    remote_data = "data" + (data.suffix or ".txt")
    transport.run(f"mkdir -p {shlex.quote(remote_dir)}")
    transport.put(TRAINER, f"{remote_dir}/train_bpe_gpt_torch.py")
    for support in SUPPORT_FILES + [LR_GOVERNOR]:
        transport.put(support, f"{remote_dir}/{support.name}")
    transport.put(data, f"{remote_dir}/{remote_data}")
    if args.resume_from:
        transport.run(f"mkdir -p {shlex.quote(remote_dir)}/model")
        for file in _checkpoint_files(Path(args.resume_from).expanduser()):
            transport.put(file, f"{remote_dir}/model/{file.name}")
        args_for_copy = train_arguments(args, copy_index, device) + ["--resume"]
    else:
        args_for_copy = train_arguments(args, copy_index, device)
    command = (f"cd {shlex.quote(remote_dir)}; NODEYARD_THREADS={threads} nohup {python} -u train_bpe_gpt_torch.py train "
               f"--data-file {shlex.quote(remote_data)} --app-data model {' '.join(shlex.quote(a) for a in args_for_copy)} "
               f"--threads {threads} --result-file result.json </dev/null > train.log 2>&1 & echo $! > train.pid; echo STARTED")
    # Fully detach stdin/stdout/stderr before SSH exits, or OpenSSH waits on the
    # background child's inherited descriptors until the client times out.
    transport.run(command, timeout=20)
    log(f"copy {copy_index} started on {host.name} ({threads} thread(s))")
    seen = 0
    poll_failures = 0
    while True:
        time.sleep(POLL_SECONDS)
        try:
            code, out = transport.run(
                f"cd {shlex.quote(remote_dir)} && tail -n +{seen + 1} train.log 2>/dev/null; echo __NY_STATE__; "
                f"(test -f result.json && echo RESULT) || (kill -0 $(cat train.pid) 2>/dev/null && echo RUNNING || echo STOPPED)",
                timeout=25)
        except (subprocess.SubprocessError, OSError) as error:
            poll_failures += 1
            if poll_failures in (1, 3, 6):
                log(f"copy {copy_index} on {host.name}: temporary SSH polling interruption; retry {poll_failures}/12 ({error})")
            if poll_failures >= 12:
                return {"copy": copy_index, "host": host.name, "ok": False,
                        "error": "SSH polling failed repeatedly; the remote process may still be running."}
            continue
        if code != 0 or "__NY_STATE__" not in out:
            poll_failures += 1
            if poll_failures in (1, 3, 6):
                log(f"copy {copy_index} on {host.name}: remote status check failed; retry {poll_failures}/12")
            if poll_failures >= 12:
                return {"copy": copy_index, "host": host.name, "ok": False, "error": out[-240:]}
            continue
        poll_failures = 0
        new_lines, _, state = out.partition("__NY_STATE__")
        for line in new_lines.splitlines():
            seen += 1
            if line.strip():
                log(f"[run {copy_index}] {line.rstrip()}")
                speed = re.search(r"([\d,]+) tokens/sec", line)
                if speed:
                    host.tokens_per_second = float(speed.group(1).replace(",", ""))
                match = re.match(r"SYNC_WAIT \| (\d+) \| ([0-9.]+) \| ([0-9.eE+-]+)", line.strip())
                if match and sync:
                    selected = sync.arrive(copy_index, int(match.group(1)), float(match.group(2)), float(match.group(3)),
                                           host, transport, remote_dir, job_dir / f"copy_{copy_index}" / "model")
                    log(f"[run {copy_index}] SYNC_SELECTED | {match.group(1)} | shared best loss "
                        f"{selected['best_loss']:.4f} from copy {selected['winner']}")
        state = state.strip()
        if "RESULT" in state:
            break
        if "STOPPED" in state:
            log(f"copy {copy_index} on {host.name} stopped without a result.")
            return {"copy": copy_index, "host": host.name, "ok": False}
    (job_dir / f"copy_{copy_index}").mkdir(parents=True, exist_ok=True)
    transport.get(f"{remote_dir}/model", job_dir / f"copy_{copy_index}" / "model")
    transport.get(f"{remote_dir}/result.json", job_dir / f"copy_{copy_index}" / "result.json")
    result = json.loads((job_dir / f"copy_{copy_index}" / "result.json").read_text(encoding="utf-8"))
    return {"copy": copy_index, "host": host.name, "ok": True, **result}


def run_copy_locally(copy_index: int, args, data: Path, job_dir: Path, threads: int, log,
                     sync: ParallelWeightSync | None = None, device: str = "auto") -> dict:
    out_dir = job_dir / f"copy_{copy_index}"
    out_dir.mkdir(parents=True, exist_ok=True)
    result_path = out_dir / "result.json"
    python = os.environ.get("NYJOBS_PYTHON", sys.executable)
    command = [python, "-u", str(TRAINER), "train", "--data-file", str(data), "--app-data", str(out_dir / "model"),
               "--threads", str(threads), "--result-file", str(result_path)] + train_arguments(args, copy_index, device)
    if args.resume_from:
        model_dir = out_dir / "model"
        model_dir.mkdir(parents=True, exist_ok=True)
        for file in _checkpoint_files(Path(args.resume_from).expanduser()):
            shutil.copy2(file, model_dir / file.name)
        command.append("--resume")
    log(f"copy {copy_index} started on this machine ({threads} thread(s))")
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    (out_dir / "train.pid").write_text(str(process.pid), encoding="utf-8")
    try:
        with (out_dir / "train.log").open("w", encoding="utf-8") as handle:
            for line in process.stdout:
                handle.write(line)
                if line.strip():
                    log(f"[run {copy_index}] {line.rstrip()}")
                    speed = re.search(r"([\d,]+) tokens/sec", line)
                    if speed:
                        this_machine().tokens_per_second = float(speed.group(1).replace(",", ""))
                    match = re.match(r"SYNC_WAIT \| (\d+) \| ([0-9.]+) \| ([0-9.eE+-]+)", line.strip())
                    if match and sync:
                        selected = sync.arrive(copy_index, int(match.group(1)), float(match.group(2)), float(match.group(3)),
                                               this_machine(), None, "", out_dir / "model")
                        log(f"[run {copy_index}] SYNC_SELECTED | {match.group(1)} | shared best loss "
                            f"{selected['best_loss']:.4f} from copy {selected['winner']}")
        process.wait()
    except Exception:
        process.terminate()
        raise
    if process.returncode != 0 or not result_path.is_file():
        log(f"copy {copy_index} failed locally; see {out_dir / 'train.log'}")
        return {"copy": copy_index, "host": "this-machine", "ok": False}
    result = json.loads(result_path.read_text(encoding="utf-8"))
    return {"copy": copy_index, "host": "this-machine", "ok": True, **result}


def run_job(args, hosts: list[Host]) -> int:
    data = Path(args.data).expanduser().resolve()
    if not data.is_file():
        raise FileNotFoundError(f"Training text does not exist: {data}")
    job_dir = JOBS_ROOT / args.job
    job_dir.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    manifest_lock = threading.Lock()
    manifest_entries: dict[str, dict] = {}

    def save_manifest() -> None:
        temporary = job_dir / "manifest.json.tmp"
        temporary.write_text(json.dumps({"job": args.job, "copies": manifest_entries}, indent=2), encoding="utf-8")
        temporary.replace(job_dir / "manifest.json")

    def log(message: str) -> None:
        with lock:
            print(message, flush=True)

    scheduler = Scheduler(hosts)
    total_slots = sum(h.slots for h in hosts)
    if args.sync_every > 0 and args.copies > total_slots:
        log(f"The every-{args.sync_every}-step weight sync needs all copies to run at once: "
            f"requested {args.copies}, but selected devices have {total_slots} total slots. "
            "Lower copies per device or disable synchronization with --sync-every 0.")
        return 1
    if args.copies <= 1:
        args.sync_every = 0
    sync = ParallelWeightSync(args.copies, job_dir, log, args.target_validation_loss) if args.sync_every > 0 else None
    log(f"Job {args.job}: {args.copies} copies on {len(scheduler.hosts)} machine(s), {total_slots} slot(s) in total.")
    results = []

    def one(copy_index: int) -> dict:
        host, device = scheduler.acquire()
        try:
            with manifest_lock:
                manifest_entries[str(copy_index)] = {
                    "host": host.name, "address": host.address, "user": host.user, "port": host.port,
                    "local": host.local, "remote_dir": f"{REMOTE_ROOT}/{args.job}/copy_{copy_index}",
                    "local_dir": str(job_dir / f"copy_{copy_index}"), "pid_file": "train.pid",
                }
                save_manifest()
            try:
                return run_copy_on(host, copy_index, args, data, job_dir, log, sync, device)
            except Exception as error:
                log(f"copy {copy_index} on {host.name} failed to start or finish: {error}")
                if sync:
                    sync.abort(f"copy {copy_index} failed: {error}")
                return {"copy": copy_index, "host": host.name, "ok": False, "error": str(error)}
        finally:
            scheduler.release(host, device)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, total_slots)) as pool:
        futures = [pool.submit(one, index) for index in range(1, args.copies + 1)]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda r: r["copy"])
    successful = [r for r in results if r.get("ok")]
    merge_hosts(hosts)
    (job_dir / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    if not successful:
        log("Every copy failed. Nothing was kept.")
        return 1
    best = min(successful, key=lambda r: r["validation_loss"])
    best_dir = job_dir / f"copy_{best['copy']}" / "model"
    shutil.copytree(best_dir, job_dir / "best", dirs_exist_ok=True)
    if args.install_best_to:
        _install_best(job_dir / "best", Path(args.install_best_to).expanduser())
        log(f"Installed the winning checkpoint into {Path(args.install_best_to).expanduser()}")
    for r in results:
        line = (f"copy {r['copy']} on {r['host']}: validation loss {r['validation_loss']:.4f}"
                if r.get("ok") else f"copy {r['copy']} on {r['host']}: failed")
        log(line)
    log(f"Best: copy {best['copy']} on {best['host']} with validation loss {best['validation_loss']:.4f}. "
        f"Saved to {job_dir / 'best'}.")
    return 0


# ---------------------------------------------------------------- commands

def cmd_hosts(_args) -> int:
    hosts = load_hosts()
    if not hosts:
        print("No machines configured. Add one with: nyjobs add-host NAME ADDRESS --user USER")
    for host in [this_machine()] + hosts:
        kind = "this machine" if host.local else host.address
        status = f"{host.cpus} CPU, {host.mem_mb} MB free, {host.arch}, torch {host.torch or 'unknown'}" if host.probed \
            else "not probed (run nyjobs probe)"
        print(f"{host.name:<16} {kind:<18} {status}  slots: {host.slots}")
    return 0


def cmd_add_host(args) -> int:
    hosts = [h for h in load_hosts() if h.name != args.name]
    hosts.append(Host(name=args.name, address=args.address, user=args.user, port=args.port, python=args.python,
                      mb_per_copy=args.mem_mb_per_copy))
    save_hosts(hosts)
    print(f"Added {args.name} ({args.address}). Run: nyjobs probe")
    return 0


def identity_path() -> Path:
    configured = os.environ.get("NYJOBS_IDENTITY", "")
    return Path(configured).expanduser() if configured else Path("~/.ssh/id_ed25519").expanduser()


def ensure_key(key: Path) -> str:
    """Create the Mac's key (no passphrase, so runs need no typing) if missing; return the public key."""
    if not key.exists():
        key.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "nodeyard-mac", "-f", str(key)],
                       check=True)
    return Path(str(key) + ".pub").read_text(encoding="utf-8").strip()


def ssh_with_password(address: str, user: str, port: int, command: str, password: str, timeout: int = 90):
    """One-time password login, used only by setup-master. The password reaches ssh through an askpass
    helper (mode 700) that is deleted straight away, so it never appears on a command line or in a log."""
    handle, askpass = tempfile.mkstemp(prefix="nyjobs-askpass-")
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write('#!/bin/sh\nprintf "%s\\n" "$NYJOBS_PASSWORD"\n')
        os.chmod(askpass, 0o700)
        env = dict(os.environ, SSH_ASKPASS=askpass, SSH_ASKPASS_REQUIRE="force", NYJOBS_PASSWORD=password)
        return subprocess.run(["ssh", "-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no",
                               "-o", "NumberOfPasswordPrompts=1", "-o", "ConnectTimeout=10",
                               "-o", "StrictHostKeyChecking=accept-new", "-p", str(port), f"{user}@{address}", command],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, env=env)
    finally:
        if os.path.exists(askpass):
            os.remove(askpass)


def cmd_setup_master(args) -> int:
    """Set up the master from this Mac, in one command: key, one-time password login, key install,
    the training SSH service (port 2222, keys only), the PyTorch runtime, and the host entry."""
    password = ""
    if args.password_file:
        password_path = Path(args.password_file)
        try:
            password = password_path.read_text(encoding="utf-8").rstrip("\n")
        finally:
            if password_path.exists():
                os.remove(password_path)
    key = identity_path()
    public = ensure_key(key)
    os.environ["NYJOBS_IDENTITY"] = str(key)
    print("SETUP | 1/5 | this Mac's key is ready", flush=True)

    if password:
        login = ssh_with_password(args.address, args.user, args.ssh_port,
                                  "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && "
                                  f"(grep -qxF {shlex.quote(public)} ~/.ssh/authorized_keys || echo {shlex.quote(public)} >> ~/.ssh/authorized_keys) && "
                                  "chmod 600 ~/.ssh/authorized_keys && echo KEY_INSTALLED", password)
        if "KEY_INSTALLED" not in login.stdout:
            print("SETUP | failed | the password login was refused. Check the address, username and password.", flush=True)
            return 1
    print("SETUP | 2/5 | the key is installed on the master", flush=True)

    transport = SSHTransport(Host(name=args.name, address=args.address, user=args.user, port=args.ssh_port))
    code, out = transport.run("echo KEY_LOGIN_OK")
    if "KEY_LOGIN_OK" not in out:
        print(f"SETUP | failed | key login does not work yet: {out.strip()[-160:]}", flush=True)
        return 1
    print("SETUP | 3/5 | key login works (no password needed)", flush=True)

    transport.put(MASTER_ACCESS_SCRIPT, "/tmp/nodeyard-training-access.sh")
    sudo_password = (password + "\n") if password else None
    code, out = transport.run(
        f"sudo -S -p '' bash /tmp/nodeyard-training-access.sh setup --public-key {shlex.quote(public)} "
        f"--user {shlex.quote(args.user)} --port {args.port} 2>&1 | tail -n 20", timeout=180, stdin_text=sudo_password)
    if "Training SSH is up" not in out and "config:" not in out:
        print(f"SETUP | failed | the master's SSH service did not start: {out.strip()[-300:]}", flush=True)
        return 1
    print(f"SETUP | 4/5 | the training SSH service is up on port {args.port} (keys only, tailnet only)", flush=True)

    code, home = transport.run("echo $HOME")
    home = home.strip() or f"/home/{args.user}"
    code, torch_state = transport.run(f"{home}/nodeyard-train/bin/python -c 'import torch;print(torch.__version__)' 2>/dev/null || echo MISSING")
    if "MISSING" in torch_state:
        transport.run("nohup bash /tmp/nodeyard-training-access.sh runtime --venv ~/nodeyard-train > ~/nodeyard-runtime.log 2>&1 &")
        print("SETUP | 5/5 | PyTorch is installing on the master in the background (several minutes). "
              "Run: nyjobs runtime-status", flush=True)
    else:
        print(f"SETUP | 5/5 | PyTorch {torch_state.strip()} is already on the master", flush=True)

    print("SETUP | workers | configuring Ready amd64/arm64 nodes through the master's Kubernetes API", flush=True)
    setup_cluster_workers(Host(name=args.name, address=args.address, user=args.user, port=args.port),
                         args.user, public, emit=lambda line: print(line, flush=True))

    hosts = [h for h in load_hosts() if h.name != args.name]
    entry = Host(name=args.name, address=args.address, user=args.user, port=args.port,
                 python=f"{home}/nodeyard-train/bin/python")
    hosts.append(entry)
    save_hosts(hosts)
    probed = probe_host(entry)
    save_hosts([probed if h.name == args.name else h for h in load_hosts()])
    if probed.probed:
        print(f"SETUP | done | {args.name}: {probed.arch}, {probed.cpus} CPU, {probed.mem_mb} MB free, {probed.slots} slot(s)",
              flush=True)
    else:
        print("SETUP | done | the master is configured; probing will retry when it is reachable", flush=True)
    return 0


def cmd_runtime_status(_args) -> int:
    try:
        discover_cluster_devices()
    except Exception as error:
        print(f"RUNTIME | cluster discovery failed: {error}")
    for host in load_hosts():
        if host.local or not host.address:
            continue
        transport = transport_for(host)
        python = host.python or "python3"
        code, out = transport.run(f"{python} -c 'import torch;print(torch.__version__)' 2>/dev/null || echo MISSING")
        if "MISSING" in out:
            _, log_tail = transport.run("tail -n 2 ~/nodeyard-runtime.log 2>/dev/null || true")
            print(f"RUNTIME | {host.name} | installing or missing: {log_tail.strip()[-160:] or 'not started'}")
        else:
            print(f"RUNTIME | {host.name} | ready, torch {out.strip()}")
    return 0


def cmd_access_key(_args) -> int:
    """Print this Mac's public key so it can be pasted into the master's training access setup."""
    for name in ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub"):
        path = Path("~/.ssh", name).expanduser()
        if path.is_file():
            print(path.read_text(encoding="utf-8").strip())
            return 0
    print("No SSH key found. Create one with: ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519", file=sys.stderr)
    return 1


def cmd_remove_host(args) -> int:
    hosts = load_hosts()
    kept = [h for h in hosts if h.name != args.name]
    save_hosts(kept)
    print(f"Removed {args.name}." if len(kept) != len(hosts) else f"No machine named {args.name}.")
    return 0


def cmd_probe(_args) -> int:
    hosts = load_hosts()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(hosts))) as pool:
        probed = list(pool.map(probe_host, hosts))
    save_hosts(probed)
    for host in probed:
        state = f"{host.arch}, {host.cpus} CPU, {host.mem_mb} MB free, slots {host.slots}" if host.probed \
            else "unreachable (check SSH key access and that the machine is online)"
        print(f"{host.name:<16} {state}")
    for host in probed:
        print(device_line(host))
    return 0


def device_line(host: Host) -> str:
    return (f"DEVICE | {host.name} | {host.address or 'this machine'} | {host.arch or '?'} | {host.cpus} | "
            f"{host.mem_mb} | {host.slots} | {1 if host.probed and host.slots else 0} | ready")


def _quantity_mb(value: str) -> int:
    match = re.fullmatch(r"([0-9.]+)(Ki|Mi|Gi|Ti)?", str(value))
    if not match:
        return 0
    amount = float(match.group(1))
    unit = match.group(2) or ""
    return int(amount * {"": 1 / (1024 * 1024), "Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024 * 1024}[unit])


def discover_cluster_devices() -> list[dict]:
    """Read Kubernetes nodes and Tailscale peers from the configured master.

    A Kubernetes node is displayed even when offline or not yet provisioned for
    training. It is selectable only after its tailnet SSH service and PyTorch
    runtime are reachable.
    """
    hosts = load_hosts()
    master = next((h for h in hosts if h.name in ("master", "k8s-control")), None)
    if master is None:
        raise RuntimeError("No master configured. Run `nyjobs setup-master` first.")
    transport = transport_for(master)
    code, nodes_text = transport.run("sudo k3s kubectl get nodes -o json 2>/dev/null", timeout=40)
    if code != 0:
        raise RuntimeError(f"Could not read Kubernetes nodes from the master: {nodes_text.strip()[-300:]}")
    code, tailscale_text = transport.run("tailscale status --json 2>/dev/null", timeout=20)
    if code != 0:
        raise RuntimeError(f"Could not read the master's Tailscale peers: {tailscale_text.strip()[-200:]}")

    node_items = json.loads(nodes_text).get("items", [])
    tailnet = json.loads(tailscale_text)
    peers = []
    self_peer = tailnet.get("Self", {})
    if self_peer:
        peers.append(self_peer)
    peers.extend((tailnet.get("Peer") or {}).values())
    devices = []

    for item in node_items:
        metadata = item.get("metadata", {})
        status = item.get("status", {})
        name = metadata.get("name", "unknown")
        addresses = {entry.get("type"): entry.get("address", "") for entry in status.get("addresses", [])}
        internal_ip = addresses.get("InternalIP", "")
        peer = None
        if name in ("k8s-control", master.name):
            peer = self_peer
        else:
            candidates = [p for p in peers if str(p.get("HostName", "")).lower() == name.lower()]
            if not candidates:
                candidates = [p for p in peers if str(p.get("HostName", "")).lower().startswith(name.lower() + "-")]
            peer = next((p for p in candidates if p.get("Online")), candidates[0] if candidates else None)
        online = bool(peer and peer.get("Online"))
        tailnet_ip = (peer.get("TailscaleIPs") or [""])[0] if peer else ""
        if name in ("k8s-control", master.name):
            tailnet_ip = master.address
            online = True

        conditions = {condition.get("type"): condition.get("status") for condition in status.get("conditions", [])}
        node_ready = conditions.get("Ready") == "True"
        allocatable = status.get("allocatable", {})
        cpu_text = str(allocatable.get("cpu", "0"))
        cpus = int(float(cpu_text[:-1]) / 1000) if cpu_text.endswith("m") else int(float(cpu_text or 0))
        mem_mb = _quantity_mb(allocatable.get("memory", "0"))
        arch = (metadata.get("labels") or {}).get("kubernetes.io/arch", "?")

        cpu_python = f"/home/{master.user}/nodeyard-train/bin/python"
        cuda_python = f"/home/{master.user}/nodeyard-train-cuda/bin/python"
        device = Host(name=name, address=tailnet_ip, user=master.user, port=master.port,
                      python=cpu_python,
                      cpus=cpus, mem_mb=mem_mb, arch=arch, probed=False,
                      mb_per_copy=master.mb_per_copy)
        if node_ready and online and tailnet_ip:
            tested = probe_host(device)
            # Use the CUDA environment only after verifying its driver/runtime.
            if tested.gpu_count:
                cuda_test = probe_host(replace(tested, python=cuda_python))
                if cuda_test.cuda_ready:
                    tested = cuda_test
            # probe_host also checks that PyTorch imports, so this is the actual selectable state.
            if tested.probed:
                device = tested
            else:
                # Keep the Kubernetes inventory facts visible even if this node
                # has not yet received its training SSH service/runtime.
                device.arch = arch
                device.cpus = cpus
                device.mem_mb = mem_mb
        if not node_ready:
            reason = "Kubernetes NotReady"
        elif not online or not tailnet_ip:
            reason = "not connected to Tailscale"
        elif arch not in ("amd64", "arm64"):
            reason = f"unsupported {arch or 'unknown architecture'} (CPU PyTorch wheel unavailable)"
        elif not device.probed:
            reason = f"training access/runtime not ready on port {master.port}"
        elif not device.slots:
            reason = "no free capacity"
        elif device.gpu_count and device.cuda_ready:
            reason = f"ready · {device.gpu_name} CUDA plus CPU"
        elif device.gpu_count:
            reason = f"CPU ready · {device.gpu_name} CUDA runtime unavailable"
        else:
            reason = "ready · CPU"
        if device.probed and device.latency_ms:
            reason += f" · {device.latency_ms:.0f} ms RTT"
        if device.tokens_per_second:
            reason += f" · previous {device.tokens_per_second:,.0f} tokens/sec"
        devices.append({"host": device, "state": reason, "internal_ip": internal_ip,
                        "online": online, "node_ready": node_ready})

    # Keep a stable control-plane alias: discovery itself needs the master connection.
    cluster_names = {device["host"].name for device in devices}
    configured = [h for h in hosts if h.name not in cluster_names and h.name not in ("master", "k8s-control")]
    control = next((h for h in hosts if h.name in ("master", "k8s-control")), None)
    if control:
        configured.append(replace(control, name="master"))
    configured.extend(device["host"] for device in devices if device["host"].probed and device["host"].slots > 0)
    save_hosts(configured)
    return devices


def run_on_kubernetes_node(master: Host, node_name: str, script: str, deadline: int = 90) -> str:
    """Run a short root-level host probe through the same privileged helper-pod
    pattern nodeyard uses for its Hardware and Memory pages. The pod is pinned
    to one node, deleted immediately afterwards, and does not expose a service.
    """
    transport = transport_for(master)
    transport.run("sudo k3s kubectl create namespace nodeyard-shell --dry-run=client -o yaml | sudo k3s kubectl apply -f -",
                   timeout=40)
    pod = "nodeyard-training-probe-" + re.sub(r"[^a-z0-9-]+", "-", node_name.lower()).strip("-")[:32] + "-" + uuid.uuid4().hex[:6]
    wrapper = ('out=$(nsenter -t 1 -m -u -i -n -p -- sh -c "$0" 2>&1); rc=$?; '
               'printf "%s\\n" "$out" > /dev/termination-log; printf "%s\\n" "$out"; exit $rc')
    pod_spec = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": pod, "namespace": "nodeyard-shell", "labels": {"app": "nodeyard-training-probe"}},
        "spec": {"nodeName": node_name, "hostPID": True, "restartPolicy": "Never",
                 "activeDeadlineSeconds": deadline, "automountServiceAccountToken": False,
                 "terminationGracePeriodSeconds": 0,
                 "tolerations": [{"operator": "Exists"}],
                 "containers": [{"name": "probe", "image": "busybox:1.36", "imagePullPolicy": "IfNotPresent",
                                 "command": ["sh", "-c", wrapper, script],
                                 "resources": {"requests": {"cpu": "50m", "memory": "32Mi"},
                                               "limits": {"memory": "512Mi"}},
                                 "securityContext": {"privileged": True}}]}}
    try:
        code, out = transport.run("sudo k3s kubectl apply -f -", timeout=40, stdin_text=json.dumps(pod_spec))
        if code:
            raise RuntimeError(f"Could not start the nodeyard helper on {node_name}: {out.strip()[-240:]}")
        end = time.time() + deadline + 20
        while time.time() < end:
            time.sleep(2)
            code, out = transport.run(f"sudo k3s kubectl -n nodeyard-shell get pod {shlex.quote(pod)} -o json", timeout=30)
            if code:
                continue
            pod_status = json.loads(out).get("status", {})
            if pod_status.get("phase") in ("Succeeded", "Failed"):
                statuses = pod_status.get("containerStatuses") or []
                terminated = ((statuses[0].get("state") or {}).get("terminated") or {}) if statuses else {}
                message = terminated.get("message", "")
                if not message or terminated.get("exitCode", 0):
                    _, logs = transport.run(f"sudo k3s kubectl -n nodeyard-shell logs {shlex.quote(pod)}", timeout=20)
                    if logs.strip():
                        message = (message + "\n" + logs).strip()
                if terminated.get("exitCode", 0):
                    raise RuntimeError(message.strip()[-300:] or f"The helper failed on {node_name}.")
                return message.strip()
        raise RuntimeError(f"The nodeyard helper did not finish on {node_name} within {deadline}s.")
    finally:
        transport.run(f"sudo k3s kubectl -n nodeyard-shell delete pod {shlex.quote(pod)} --ignore-not-found --wait=false",
                      timeout=20)


def setup_cluster_workers(master: Host, username: str, public_key: str, emit=print) -> None:
    """Provision Ready Kubernetes workers through a temporary privileged helper pod.
    Supported 64-bit nodes receive the runtime; 32-bit nodes get key-only access
    but remain unavailable for PyTorch training.
    """
    transport = transport_for(master)
    code, nodes_text = transport.run("sudo k3s kubectl get nodes -o json", timeout=40)
    if code:
        emit(f"SETUP | workers | couldn't read cluster nodes: {nodes_text.strip()[-180:]}")
        return
    script_b64 = base64.b64encode(MASTER_ACCESS_SCRIPT.read_bytes()).decode("ascii")
    nodes = json.loads(nodes_text).get("items", [])
    for node in nodes:
        metadata = node.get("metadata", {})
        name = metadata.get("name", "")
        if not name or name in ("k8s-control", master.name):
            continue
        status = node.get("status", {})
        ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in status.get("conditions", []))
        arch = (metadata.get("labels") or {}).get("kubernetes.io/arch", "")
        if not ready:
            emit(f"SETUP | {name} | skipped: Kubernetes reports NotReady")
            continue
        supported = arch in ("amd64", "arm64")
        home = f"/home/{username}"
        runtime_setup = f"""if ! python3 -m venv /tmp/nodeyard-venv-check >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    dpkg --configure -a
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-pip
  else
    echo 'Python venv support is missing and this node has no apt-get.'
    exit 1
  fi
fi
rm -rf /tmp/nodeyard-venv-check
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
  TRAIN_VENV={shlex.quote(home + "/nodeyard-train-cuda")}
  if "$TRAIN_VENV/bin/python" -c 'import torch; assert torch.cuda.is_available()' >/dev/null 2>&1; then NEED_RUNTIME=0; else rm -rf "$TRAIN_VENV"; NEED_RUNTIME=1; fi
else
  TRAIN_VENV={shlex.quote(home + "/nodeyard-train")}
  if "$TRAIN_VENV/bin/python" -c 'import torch' >/dev/null 2>&1; then NEED_RUNTIME=0; else rm -rf "$TRAIN_VENV"; NEED_RUNTIME=1; fi
fi
if [ "$NEED_RUNTIME" = 1 ]; then
  su - {shlex.quote(username)} -c "nohup bash /tmp/nodeyard-training-access.sh runtime --venv '$TRAIN_VENV' > {shlex.quote(home + "/nodeyard-runtime.log")} 2>&1 </dev/null &"
else
  echo TRAINING_RUNTIME_READY
fi
""" if supported else "echo TRAINING_UNSUPPORTED_32BIT_ARM\n"
        host_script = f"""set -eu
printf '%s' {shlex.quote(script_b64)} | base64 -d > /tmp/nodeyard-training-access.sh
chmod 755 /tmp/nodeyard-training-access.sh
if ! id {shlex.quote(username)} >/dev/null 2>&1; then useradd --create-home --shell /bin/bash {shlex.quote(username)}; fi
bash /tmp/nodeyard-training-access.sh setup --public-key {shlex.quote(public_key)} --user {shlex.quote(username)} --port 2222
{runtime_setup}
echo WORKER_SETUP_OK
"""
        try:
            output = run_on_kubernetes_node(master, name, host_script, deadline=600)
            if "Training SSH is up" in output and "WORKER_SETUP_OK" in output and supported:
                emit(f"SETUP | {name} | key-only Tailscale SSH installed; PyTorch CPU runtime installing in background")
            elif "Training SSH is up" in output and "TRAINING_UNSUPPORTED_32BIT_ARM" in output:
                emit(f"SETUP | {name} | key-only Tailscale SSH installed; remote training unsupported on {arch}")
            else:
                emit(f"SETUP | {name} | setup incomplete: {output[-220:]}")
        except Exception as error:
            emit(f"SETUP | {name} | setup failed: {error}")


def cmd_discover(_args) -> int:
    for entry in discover_cluster_devices():
        host = entry["host"]
        print(f"DEVICE | {host.name} | {host.address or entry['internal_ip']} | {host.arch} | {host.cpus} | "
              f"{host.mem_mb} | {host.slots} | {1 if host.probed and host.slots else 0} | {entry['state']}")
    return 0


UNLOAD_SCRIPT = r'''if command -v ollama >/dev/null 2>&1; then n=0; for m in $(ollama ps 2>/dev/null | awk 'NR>1 {print $1}'); do ollama stop "$m" >/dev/null 2>&1 && n=$((n+1)); done; echo "UNLOADED $n"; else echo "UNLOADED 0 (no Ollama installed)"; fi'''


def unload_models(host: Host) -> None:
    """Stop any AI models loaded on the device so training gets the memory and CPU."""
    if host.local:
        output = subprocess.run(["sh", "-c", UNLOAD_SCRIPT], capture_output=True, text=True).stdout
    else:
        output = transport_for(host).run(UNLOAD_SCRIPT, timeout=180)[1]
    match = re.search(r"UNLOADED (\d+)", output)
    detail = match.group(1) if match else "unknown"
    print(f"UNLOAD | {host.name} | stopped {detail} model(s)", flush=True)


def cmd_plan(args) -> int:
    hosts = [this_machine()] if args.local else load_hosts()
    if not args.local:
        hosts = [h for h in hosts if h.probed] or hosts
    for assignment in plan(hosts, args.copies):
        print(f"copy {assignment.copy} -> {assignment.host.name} ({assignment.host.slots} slots there)")
    return 0


def cmd_pause(args) -> int:
    """Ask every running copy/rank in a job to save at its next step boundary."""
    manifest_path = JOBS_ROOT / args.job / "manifest.json"
    if not manifest_path.is_file():
        print(f"No active job manifest for {args.job}.", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("copies", {})
    sent = 0
    for _key, entry in entries.items():
        pid_path = entry.get("pid_file", "train.pid")
        if entry.get("local"):
            pid_file = Path(entry.get("local_dir", "")) / pid_path
            try:
                (Path(entry.get("local_dir", "")) / "model" / "pause.sync").touch()
                os.kill(int(pid_file.read_text(encoding="utf-8").strip()), signal.SIGUSR1)
                sent += 1
                print(f"PAUSE | {entry.get('host')} | requested a checkpoint")
            except (OSError, ValueError, FileNotFoundError) as error:
                print(f"PAUSE | {entry.get('host')} | not running: {error}")
        else:
            host = Host(name=entry["host"], address=entry["address"], user=entry.get("user", ""),
                        port=int(entry.get("port", 22)))
            remote_pid = f"{entry['remote_dir']}/{pid_path}"
            code, output = transport_for(host).run(
                f"touch {shlex.quote(entry['remote_dir'] + '/model/pause.sync')} && "
                f"pid=$(cat {shlex.quote(remote_pid)} 2>/dev/null) && kill -USR1 $pid && echo PAUSE_SENT",
                timeout=20)
            if code == 0 and "PAUSE_SENT" in output:
                sent += 1
                print(f"PAUSE | {entry['host']} | requested a checkpoint")
            else:
                print(f"PAUSE | {entry['host']} | not running: {output.strip()[-160:]}")
    print(f"Requested checkpoints from {sent}/{len(entries)} process(es). They finish the current step, save, then exit.")
    return 0 if sent else 1


def run_distributed_job(args, hosts: list[Host]) -> int:
    """Train one shared model with one rank per selected CPU/GPU worker.

    Gloo averages flattened gradients every step. A CUDA rank is assigned before
    CPU ranks on a device, so `debian-1:2` means one GTX rank plus one CPU rank.
    """
    data = Path(args.data).expanduser().resolve()
    if not data.is_file():
        raise FileNotFoundError(f"Training text does not exist: {data}")
    rank_hosts = []
    remaining = args.copies
    ordered_hosts = sorted(hosts, key=lambda h: (
        bool(h.cuda_ready),
        (h.tokens_per_second or (12000 if h.cuda_ready else h.cpus * 500)) /
        (1.0 + max(0.0, h.latency_ms) / 50.0),
    ), reverse=True)
    for host in ordered_hosts:
        count = min(remaining, host.limit if host.limit > 0 else host.slots)
        if count <= 0:
            continue
        gpu_ranks = min(count, host.gpu_count if host.cuda_ready else 0)
        for index in range(gpu_ranks):
            rank_hosts.append({"host": host, "device": f"cuda:{index}", "gpu": True})
        for _ in range(count - gpu_ranks):
            rank_hosts.append({"host": host, "device": "cpu", "gpu": False})
        remaining -= count
        if remaining == 0:
            break
    if remaining:
        raise RuntimeError(f"Selected devices have too few slots for {args.copies} distributed ranks.")
    world_size = len(rank_hosts)
    if world_size < 2:
        print("Split training needs at least two selected CPU/GPU ranks.", file=sys.stderr)
        return 1

    # Rendezvous on the best measured/accelerated selected node.
    master_rank_index = max(range(world_size), key=lambda i: (
        rank_hosts[i]["gpu"],
        (rank_hosts[i]["host"].tokens_per_second or (12000 if rank_hosts[i]["gpu"] else rank_hosts[i]["host"].cpus * 500)) /
        (1.0 + max(0.0, rank_hosts[i]["host"].latency_ms) / 50.0),
    ))
    if master_rank_index != 0:
        rank_hosts[0], rank_hosts[master_rank_index] = rank_hosts[master_rank_index], rank_hosts[0]
    rendezvous_host = rank_hosts[0]["host"]
    master_addr = rendezvous_host.address
    master_port = 29500 + int(hashlib.sha256(args.job.encode()).hexdigest()[:4], 16) % 100

    job_dir = JOBS_ROOT / args.job / "distributed"
    job_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = job_dir.parent / "manifest.json"
    manifest_entries: dict[str, dict] = {}
    manifest_lock = threading.Lock()

    def save_manifest() -> None:
        temporary = manifest_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({"job": args.job, "mode": "split", "copies": manifest_entries}, indent=2),
                             encoding="utf-8")
        temporary.replace(manifest_path)

    lock = threading.Lock()

    def log(message: str) -> None:
        with lock:
            print(message, flush=True)

    log(f"Distributed job {args.job}: one model, {world_size} ranks, rendezvous {rendezvous_host.name}:{master_port}.")
    log("Each update averages gradients from every rank; CUDA and CPU ranks use Gloo over the tailnet.")
    launched = []
    try:
        # Launch rank 0 first; its TCP store waits for the remaining ranks.
        for rank, assignment in enumerate(rank_hosts):
            host = assignment["host"]
            transport = transport_for(host)
            rank_dir = f"{REMOTE_ROOT}/{args.job}/distributed/rank_{rank}"
            remote_data = "data" + (data.suffix or ".txt")
            transport.run(f"mkdir -p {shlex.quote(rank_dir)}/model")
            transport.put(TRAINER, f"{rank_dir}/train_bpe_gpt_torch.py")
            for support in SUPPORT_FILES + [LR_GOVERNOR]:
                transport.put(support, f"{rank_dir}/{support.name}")
            transport.put(data, f"{rank_dir}/{remote_data}")
            if args.resume_from:
                source_checkpoint = Path(args.resume_from).expanduser()
                files = _checkpoint_files(source_checkpoint)
                if len(files) != 3:
                    raise FileNotFoundError(f"The resume model is incomplete: {source_checkpoint}")
                for file in files:
                    transport.put(file, f"{rank_dir}/model/{file.name}")
            local_cpus = max(1, host.cpus - 1 if host.cpus > 2 else host.cpus)
            ranks_on_host = sum(entry["host"].name == host.name for entry in rank_hosts)
            threads = max(1, local_cpus // max(1, ranks_on_host))
            rank_args = train_arguments(args, 0, assignment["device"]) + [
                "--rank", str(rank), "--world-size", str(world_size),
                "--master-addr", master_addr, "--master-port", str(master_port),
                "--distributed-timeout", str(args.distributed_timeout), "--sync-every", "0",
            ]
            if args.resume_from:
                rank_args.append("--resume")
            python = host.python or os.environ.get("NYJOBS_PYTHON", "python3")
            command = (f"cd {shlex.quote(rank_dir)}; GLOO_SOCKET_IFNAME=tailscale0 NODEYARD_THREADS={threads} "
                       f"nohup {shlex.quote(python)} -u train_bpe_gpt_torch.py train --data-file {shlex.quote(remote_data)} "
                       f"--app-data model {' '.join(shlex.quote(value) for value in rank_args)} "
                       f"--threads {threads} --result-file result.json </dev/null > train.log 2>&1 & "
                       f"echo $! > train.pid; echo STARTED")
            transport.run(command, timeout=20)
            launched.append({"rank": rank, "host": host, "transport": transport, "rank_dir": rank_dir,
                             "local_dir": job_dir / f"rank_{rank}", "seen": 0})
            with manifest_lock:
                manifest_entries[str(rank)] = {"host": host.name, "address": host.address, "user": host.user,
                                               "port": host.port, "local": False, "remote_dir": rank_dir,
                                               "pid_file": "train.pid"}
                save_manifest()
            log(f"rank {rank} started on {host.name} ({assignment['device']}, {threads} CPU thread(s))")

        def monitor(item: dict) -> tuple[dict, dict | None]:
            rank = item["rank"]
            done_file = "result.json" if rank == 0 else f"model/rank_{rank}.done.json"
            failures = 0
            while True:
                time.sleep(POLL_SECONDS)
                try:
                    code, output = item["transport"].run(
                        f"cd {shlex.quote(item['rank_dir'])} && tail -n +{item['seen'] + 1} train.log 2>/dev/null; "
                        f"echo __NY_STATE__; (test -f {shlex.quote(done_file)} && echo DONE) || "
                        f"(kill -0 $(cat train.pid) 2>/dev/null && echo RUNNING || echo STOPPED)", timeout=25)
                except (subprocess.SubprocessError, OSError) as error:
                    failures += 1
                    if failures in (1, 3, 6):
                        log(f"rank {rank} on {item['host'].name}: temporary SSH interruption; retry {failures}/12")
                    if failures >= 12:
                        raise RuntimeError(f"rank {rank} SSH polling failed repeatedly: {error}")
                    continue
                if code or "__NY_STATE__" not in output:
                    failures += 1
                    if failures >= 12:
                        raise RuntimeError(f"rank {rank} status check failed repeatedly: {output[-200:]}")
                    continue
                failures = 0
                log_text, _, state = output.partition("__NY_STATE__")
                for line in log_text.splitlines():
                    item["seen"] += 1
                    if not line.strip():
                        continue
                    if rank == 0:
                        log(f"[run 1] {line.rstrip()}")
                        speed = re.search(r"([\d,]+) tokens/sec", line)
                        if speed:
                            item["host"].tokens_per_second = float(speed.group(1).replace(",", ""))
                    elif "ERROR:" in line or "Traceback" in line:
                        log(f"[rank {rank}] {line.rstrip()}")
                state = state.strip()
                if "DONE" in state:
                    rank_result = None
                    if rank == 0:
                        code, result_text = item["transport"].run(
                            f"cat {shlex.quote(item['rank_dir'] + '/result.json')}", timeout=20)
                        if code == 0:
                            rank_result = json.loads(result_text)
                    return item, rank_result
                if "STOPPED" in state:
                    raise RuntimeError(f"distributed rank {rank} on {item['host'].name} stopped without a checkpoint")

        errors = []
        rank_zero_result = None
        with concurrent.futures.ThreadPoolExecutor(max_workers=world_size) as pool:
            futures = [pool.submit(monitor, item) for item in launched]
            for future in concurrent.futures.as_completed(futures):
                try:
                    item, result = future.result()
                    if item["rank"] == 0:
                        rank_zero_result = result
                except Exception as error:
                    errors.append(str(error))
        if errors or not rank_zero_result:
            raise RuntimeError("Distributed training failed: " + "; ".join(errors or ["rank 0 produced no result"]))

        rank0 = launched[0]
        (job_dir / "best").mkdir(parents=True, exist_ok=True)
        rank0["transport"].get(f"{rank0['rank_dir']}/model", job_dir / "best")
        if args.install_best_to:
            _install_best(job_dir / "best", Path(args.install_best_to).expanduser())
            log(f"Installed the distributed model into {Path(args.install_best_to).expanduser()}")
        result = {"validation_loss": rank_zero_result["validation_loss"], "world_size": world_size,
                  "total_steps": rank_zero_result["total_steps"]}
        (job_dir / "results.json").write_text(json.dumps([result], indent=2), encoding="utf-8")
        log(f"Distributed model saved to {job_dir / 'best'} with validation loss {result['validation_loss']:.4f}.")
        return 0
    except Exception as error:
        log(f"Distributed training failed: {error}; stopping its ranks.")
        for item in launched:
            item["transport"].run(f"kill -TERM $(cat {shlex.quote(item['rank_dir'] + '/train.pid')}) 2>/dev/null || true",
                                  timeout=15)
        return 1


def cmd_run(args) -> int:
    if args.rollback_on_regression or args.rollback_step or args.target_validation_loss > 0:
        args.eval_every = min(args.eval_every, 10)
    configured = load_hosts()
    if not args.local:
        configured = [probe_host(h) if (not h.probed and not h.local) else h for h in configured]
        save_hosts(configured)
        hosts = [h for h in configured if h.probed]
        if args.device:
            try:
                discovered = discover_cluster_devices()
                hosts = [entry["host"] for entry in discovered
                         if entry["host"].probed and entry["host"].slots > 0]
            except Exception as error:
                print(f"Cluster discovery failed: {error}", file=sys.stderr)
                return 1
            chosen: dict[str, int] = {}
            for item in args.device:
                name, _, count = item.partition(":")
                chosen[name] = int(count) if count else 0
            known = {h.name for h in hosts}
            missing = [name for name in chosen if name not in known]
            if missing:
                print(f"No probed device named {', '.join(missing)}. Run nyjobs probe first.", file=sys.stderr)
                return 1
            hosts = [h for h in hosts if h.name in chosen]
            for host in hosts:
                host.limit = chosen[host.name]
        if not hosts:
            print("No probed devices. Run nyjobs probe, or use --local.", file=sys.stderr)
            return 1
    else:
        hosts = [this_machine()]
    if not args.keep_models:
        for host in hosts:
            unload_models(host)
    if args.split:
        return run_distributed_job(args, hosts)
    return run_job(args, hosts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nyjobs", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("hosts").set_defaults(handler=cmd_hosts)
    add = commands.add_parser("add-host")
    add.add_argument("name")
    add.add_argument("address")
    add.add_argument("--user", default="")
    add.add_argument("--port", type=int, default=22, help="SSH port (the master's training service uses 2222)")
    add.add_argument("--python", default="", help="Python with torch on the machine (default: python3)")
    add.add_argument("--mem-mb-per-copy", type=int, default=DEFAULT_MB_PER_COPY)
    add.set_defaults(handler=cmd_add_host)
    commands.add_parser("access-key").set_defaults(handler=cmd_access_key)
    setup = commands.add_parser("setup-master", help="set up a master from this Mac (one command, one password)")
    setup.add_argument("--address", required=True)
    setup.add_argument("--user", required=True)
    setup.add_argument("--name", default="master")
    setup.add_argument("--ssh-port", type=int, default=22, help="the normal SSH port used for the one-time login")
    setup.add_argument("--port", type=int, default=2222, help="the training SSH port to use from now on")
    setup.add_argument("--password-file", default=None, help="file holding the one-time password; deleted after reading")
    setup.set_defaults(handler=cmd_setup_master)
    commands.add_parser("runtime-status").set_defaults(handler=cmd_runtime_status)
    remove = commands.add_parser("remove-host")
    remove.add_argument("name")
    remove.set_defaults(handler=cmd_remove_host)
    commands.add_parser("probe").set_defaults(handler=cmd_probe)
    commands.add_parser("discover", help="list Kubernetes nodes and their tailnet/training availability").set_defaults(handler=cmd_discover)
    pause = commands.add_parser("pause", help="save and stop all copies/ranks in an active job")
    pause.add_argument("--job", required=True)
    pause.set_defaults(handler=cmd_pause)
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("--copies", type=int, required=True)
    plan_parser.add_argument("--local", action="store_true")
    plan_parser.set_defaults(handler=cmd_plan)
    run = commands.add_parser("run")
    run.add_argument("--data", required=True)
    run.add_argument("--copies", type=int, default=1)
    run.add_argument("--split", action="store_true",
                     help="train one shared model with synchronized gradients across selected CPU/GPU devices")
    run.add_argument("--distributed-timeout", type=int, default=900,
                     help="seconds to wait for distributed ranks to rendezvous")
    run.add_argument("--sync-every", type=int, default=10,
                     help="every N steps, synchronize all copies to the lowest validation loss (0 disables)")
    run.add_argument("--target-validation-loss", type=float, default=3.5,
                     help="save and stop when held-out loss reaches this value (0 disables)")
    run.add_argument("--job", default=f"job-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}")
    run.add_argument("--local", action="store_true", help="run on this machine only")
    run.add_argument("--device", action="append", default=[], metavar="NAME[:PARALLEL]",
                     help="use only this device, with this many parallel copies (repeat for more devices)")
    run.add_argument("--keep-models", action="store_true", help="do not unload AI models before training")
    run.add_argument("--resume-from", type=Path, default=None,
                     help="continue from a local BPE checkpoint directory (MLX or PyTorch)")
    run.add_argument("--install-best-to", type=Path, default=None,
                     help="install the winning checkpoint into this local model directory after success")
    run.add_argument("--steps", type=int, default=3000)
    run.add_argument("--batch-size", type=int, default=64)
    run.add_argument("--context", type=int, default=128)
    run.add_argument("--dim", type=int, default=200)
    run.add_argument("--heads", type=int, default=5)
    run.add_argument("--layers", type=int, default=5)
    run.add_argument("--vocab-size", type=int, default=10_000)
    run.add_argument("--learning-rate", type=float, default=1e-3)
    run.add_argument("--weight-decay", type=float, default=0.1)
    run.add_argument("--warmup", type=int, default=100)
    run.add_argument("--eval-every", type=int, default=250)
    run.add_argument("--seed", type=int, default=17)
    run.add_argument("--assistant-only", action="store_true")
    run.add_argument("--rollback-on-regression", action="store_true",
                     help="restore the best validation weights whenever the next validation does not improve")
    run.add_argument("--rollback-step", action="store_true",
                     help="undo a training step if loss on that same batch increases after the update")
    run.set_defaults(handler=cmd_run)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
