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
import concurrent.futures
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRAINER = HERE / "train_bpe_gpt_torch.py"
SUPPORT_FILES = [HERE / "bpe_tokenizer.py", HERE / "sampling.py"]
CONFIG_PATH = Path(os.environ.get("NODEYARD_JOBS_CONFIG", "~/.config/nodeyard/jobs.json")).expanduser()
JOBS_ROOT = Path(os.environ.get("NODEYARD_JOBS_OUTPUT", "~/nodeyard-jobs")).expanduser()
REMOTE_ROOT = "nodeyard-jobs"
DEFAULT_MB_PER_COPY = 900  # conservative: the 6.4M-weight model, its optimizer state, and activations
RESERVE_MB = 1024
POLL_SECONDS = 5
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
    mb_per_copy: int = DEFAULT_MB_PER_COPY
    probed: bool = False

    @property
    def slots(self) -> int:
        if not self.probed or self.cpus <= 0:
            return 0
        cpu_slots = self.cpus - 1 if self.cpus > 2 else self.cpus
        mem_slots = max(0, (self.mem_mb - RESERVE_MB) // self.mb_per_copy)
        return max(0, min(cpu_slots, mem_slots))

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
                          probed=bool(entry.get("probed", False))))
    return hosts


def save_hosts(hosts: list[Host]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"hosts": [{"name": h.name, "address": h.address, "user": h.user, "port": h.port, "python": h.python,
                          "mb_per_copy": h.mb_per_copy,
                          "cpus": h.cpus, "mem_mb": h.mem_mb, "arch": h.arch, "torch": h.torch,
                          "probed": h.probed} for h in hosts]}
    CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


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
    host = Host(name="this-machine", local=True, cpus=cpus, mem_mb=mem_mb, arch=os.uname().machine, probed=True)
    return host


# ---------------------------------------------------------------- transport

class SSHTransport:
    """Key-only SSH over Tailscale. BatchMode stops any password prompt, so a missing key fails at once.
    NYJOBS_IDENTITY, when set, is the private key to use (the Tutor app keeps its own key)."""

    def __init__(self, host: Host):
        self.target = f"{host.user}@{host.address}" if host.user else host.address
        self.port = host.port
        identity = os.environ.get("NYJOBS_IDENTITY", "")
        self.options = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
        if identity:
            self.options += ["-i", identity, "-o", "IdentitiesOnly=yes"]

    def run(self, command: str, timeout: int = 60, stdin_text: str | None = None) -> tuple[int, str]:
        result = subprocess.run(["ssh", *self.options, "-p", str(self.port), self.target, command],
                                capture_output=True, text=True, timeout=timeout, input=stdin_text)
        return result.returncode, result.stdout + result.stderr

    def put(self, local: Path, remote: str) -> None:
        subprocess.run(["scp", "-q", *self.options, "-P", str(self.port), str(local), f"{self.target}:{remote}"],
                       check=True)

    def get(self, remote: str, local: Path) -> None:
        subprocess.run(["scp", "-q", "-r", *self.options, "-P", str(self.port), f"{self.target}:{remote}", str(local)],
                       check=True)


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
    code, out = transport.run(
        "echo arch=$(uname -m); echo cpus=$(nproc 2>/dev/null || sysctl -n hw.ncpu); "
        "echo mem_mb=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo 2>/dev/null || echo $(( $(sysctl -n hw.memsize) / 1048576 ))); "
        "echo torch=$(python3 -c 'import torch;print(torch.__version__)' 2>/dev/null || echo none)"
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
    host.probed = True
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
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)

    def acquire(self) -> Host:
        with self.changed:
            while True:
                candidates = [h for h in self.hosts if self.free[h.name] > 0]
                if candidates:
                    host = max(candidates, key=lambda h: (self.free[h.name], h.slots))
                    self.free[host.name] -= 1
                    return host
                if not self.hosts:
                    raise RuntimeError("No machine has capacity. Run `nyjobs probe` or add a machine.")
                self.changed.wait()

    def release(self, host: Host) -> None:
        with self.changed:
            self.free[host.name] += 1
            self.changed.notify_all()


def plan(hosts: list[Host], copies: int) -> list[Assignment]:
    """Static plan: how copies would be spread at full capacity, most capacity first."""
    scheduler = Scheduler(hosts)
    assignments = []
    for index in range(1, copies + 1):
        candidates = [h for h in scheduler.hosts if scheduler.free[h.name] > 0]
        if not candidates:
            break
        host = max(candidates, key=lambda h: (scheduler.free[h.name], h.slots))
        scheduler.free[host.name] -= 1
        assignments.append(Assignment(copy=index, host=host))
    return assignments


# ---------------------------------------------------------------- running copies

def train_arguments(args, copy_index: int) -> list[str]:
    return ["--steps", str(args.steps), "--batch-size", str(args.batch_size), "--context", str(args.context),
            "--dim", str(args.dim), "--heads", str(args.heads), "--layers", str(args.layers),
            "--vocab-size", str(args.vocab_size), "--learning-rate", str(args.learning_rate),
            "--weight-decay", str(args.weight_decay), "--warmup", str(args.warmup),
            "--eval-every", str(args.eval_every), "--seed", str(args.seed + copy_index)] \
        + (["--assistant-only"] if args.assistant_only else [])


def run_copy_on(host: Host, copy_index: int, args, data: Path, job_dir: Path, log) -> dict:
    if host.local:
        return run_copy_locally(copy_index, args, data, job_dir, host.threads_per_copy, log)
    transport = transport_for(host)
    remote_dir = f"{REMOTE_ROOT}/{args.job}/copy_{copy_index}"
    threads = host.threads_per_copy
    python = os.environ.get("NYJOBS_PYTHON", host.python or "python3")
    transport.run(f"mkdir -p {shlex.quote(remote_dir)}")
    transport.put(TRAINER, f"{remote_dir}/train_bpe_gpt_torch.py")
    for support in SUPPORT_FILES + [LR_GOVERNOR]:
        transport.put(support, f"{remote_dir}/{support.name}")
    transport.put(data, f"{remote_dir}/data.txt")
    command = (f"cd {shlex.quote(remote_dir)} && NODEYARD_THREADS={threads} nohup {python} -u train_bpe_gpt_torch.py train "
               f"--data-file data.txt --app-data model {' '.join(shlex.quote(a) for a in train_arguments(args, copy_index))} "
               f"--threads {threads} --result-file result.json > train.log 2>&1 & echo $! > train.pid")
    transport.run(command)
    log(f"copy {copy_index} started on {host.name} ({threads} thread(s))")
    seen = 0
    while True:
        time.sleep(POLL_SECONDS)
        code, out = transport.run(
            f"cd {shlex.quote(remote_dir)} && tail -n +{seen + 1} train.log 2>/dev/null; echo __NY_STATE__; "
            f"(test -f result.json && echo RESULT) || (kill -0 $(cat train.pid) 2>/dev/null && echo RUNNING || echo STOPPED)")
        new_lines, _, state = out.partition("__NY_STATE__")
        for line in new_lines.splitlines():
            seen += 1
            if line.strip():
                log(f"[run {copy_index}] {line.rstrip()}")
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


def run_copy_locally(copy_index: int, args, data: Path, job_dir: Path, threads: int, log) -> dict:
    out_dir = job_dir / f"copy_{copy_index}"
    out_dir.mkdir(parents=True, exist_ok=True)
    result_path = out_dir / "result.json"
    command = [sys.executable, "-u", str(TRAINER), "train", "--data-file", str(data), "--app-data", str(out_dir / "model"),
               "--threads", str(threads), "--result-file", str(result_path)] + train_arguments(args, copy_index)
    log(f"copy {copy_index} started on this machine ({threads} thread(s))")
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    with (out_dir / "train.log").open("w", encoding="utf-8") as handle:
        for line in process.stdout:
            handle.write(line)
            if line.strip():
                log(f"[run {copy_index}] {line.rstrip()}")
    process.wait()
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

    def log(message: str) -> None:
        with lock:
            print(message, flush=True)

    scheduler = Scheduler(hosts)
    total_slots = sum(h.slots for h in hosts)
    log(f"Job {args.job}: {args.copies} copies on {len(scheduler.hosts)} machine(s), {total_slots} slot(s) in total.")
    results = []

    def one(copy_index: int) -> dict:
        host = scheduler.acquire()
        try:
            return run_copy_on(host, copy_index, args, data, job_dir, log)
        finally:
            scheduler.release(host)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, total_slots)) as pool:
        futures = [pool.submit(one, index) for index in range(1, args.copies + 1)]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda r: r["copy"])
    successful = [r for r in results if r.get("ok")]
    (job_dir / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    if not successful:
        log("Every copy failed. Nothing was kept.")
        return 1
    best = min(successful, key=lambda r: r["validation_loss"])
    best_dir = job_dir / f"copy_{best['copy']}" / "model"
    shutil.copytree(best_dir, job_dir / "best", dirs_exist_ok=True)
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
    return 0


def cmd_plan(args) -> int:
    hosts = [this_machine()] if args.local else load_hosts()
    if not args.local:
        hosts = [h for h in hosts if h.probed] or hosts
    for assignment in plan(hosts, args.copies):
        print(f"copy {assignment.copy} -> {assignment.host.name} ({assignment.host.slots} slots there)")
    return 0


def cmd_run(args) -> int:
    if not args.local:
        configured = load_hosts()
        refreshed = [probe_host(h) if (not h.probed and not h.local) else h for h in configured]
        save_hosts(refreshed)
    hosts = [this_machine()] if args.local else [h for h in load_hosts() if h.probed]
    if not args.local and not hosts:
        print("No probed machines. Run nyjobs probe, or use --local.", file=sys.stderr)
        return 1
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
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("--copies", type=int, required=True)
    plan_parser.add_argument("--local", action="store_true")
    plan_parser.set_defaults(handler=cmd_plan)
    run = commands.add_parser("run")
    run.add_argument("--data", required=True)
    run.add_argument("--copies", type=int, default=1)
    run.add_argument("--job", default=f"job-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}")
    run.add_argument("--local", action="store_true", help="run on this machine only")
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
    run.set_defaults(handler=cmd_run)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
