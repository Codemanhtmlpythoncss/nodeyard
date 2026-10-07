#!/usr/bin/env python3
"""nodeyard speed test for one machine: memory copy speed, CPU speed and disk
speed. Standard library only. Prints one line: BENCH {json}.

The disk test writes a temporary file (at most 1 GiB, and at most a quarter of
the room it may use) into BENCH_DIR, reads it back with the page cache dropped
for that file, and deletes it. Nothing else on the machine is touched.
"""
import json
import multiprocessing as mp
import os
import random
import time

MiB = 1 << 20


def mem_copy(seconds=1.2, size=64 * MiB):
    """Bytes copied per second by one core (memcpy through CPython's bytes())."""
    buf = bytearray(os.urandom(MiB)) * (size // MiB)
    bytes(buf)  # warm up
    n, t0 = 0, time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        bytes(buf)
        n += 1
    return n * size / (time.perf_counter() - t0)


def cpu_score(seconds=1.0):
    """Simple integer work per second (relative score: higher is faster)."""
    n, t0, x = 0, time.perf_counter(), 1
    while True:
        for i in range(20000):
            x = (x * 1103515245 + 12345 + i) & 0xFFFFFFFF
        n += 20000
        if time.perf_counter() - t0 >= seconds:
            break
    return n / (time.perf_counter() - t0) / 1e6


def _par(fn, procs):
    with mp.Pool(procs) as pool:
        return sum(pool.map(fn, [None] * procs))


def _mem_one(_):
    return mem_copy(1.2, 32 * MiB)


def _cpu_one(_):
    return cpu_score(1.0)


def drop(fd):
    """Forget this file's cached pages (Linux), so reads really hit the disk."""
    if hasattr(os, "posix_fadvise"):
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)


def disk(path, room):
    st = os.statvfs(path)
    free = st.f_bavail * st.f_frsize
    limit = min(free // 4, room // 4 if room else free // 4, 1024 * MiB)
    size = (limit // (4 * MiB)) * 4 * MiB
    if size < 64 * MiB:
        return {"skipped": "not enough free disk to test safely"}
    f = os.path.join(path, ".nodeyard-bench.tmp")
    block = os.urandom(4 * MiB)
    try:
        fd = os.open(f, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        t0 = time.perf_counter()
        for _ in range(size // len(block)):
            os.write(fd, block)
        os.fsync(fd)
        wt = time.perf_counter() - t0
        os.close(fd)
        fd = os.open(f, os.O_RDONLY)
        drop(fd)  # read from the disk, not memory
        t0 = time.perf_counter()
        while os.read(fd, 4 * MiB):
            pass
        rt = time.perf_counter() - t0
        drop(fd)
        # random 4 KiB reads (what loading scattered data feels like)
        n, t0 = 0, time.perf_counter()
        blocks = size // 4096
        while time.perf_counter() - t0 < 1.5 and n < 20000:
            os.pread(fd, 4096, random.randrange(blocks) * 4096)
            n += 1
        iops = n / (time.perf_counter() - t0)
        os.close(fd)
        return {"write_mbs": round(size / wt / MiB, 1), "read_mbs": round(size / rt / MiB, 1), "rand_read_iops": round(iops), "test_size": size}
    finally:
        try:
            os.remove(f)
        except OSError:
            pass


def main():
    try:
        os.nice(19)  # everything else on the machine comes first
    except OSError:
        pass
    cores = os.cpu_count() or 1
    out = {"time": time.time(), "cores": cores}
    out["mem_copy_1core_gbs"] = round(mem_copy() / 1e9, 2)
    out["mem_copy_all_gbs"] = round(_par(_mem_one, cores) / 1e9, 2) if cores > 1 else out["mem_copy_1core_gbs"]
    out["cpu_1core"] = round(cpu_score(), 1)
    out["cpu_all"] = round(_par(_cpu_one, cores), 1) if cores > 1 else out["cpu_1core"]
    if os.environ.get("SKIP_DISK") == "1":
        out["disk"] = {"skipped": "control-plane node: the cluster database lives on this disk"}
        print("BENCH " + json.dumps(out, separators=(",", ":")), flush=True)
        return
    try:
        out["disk"] = disk(os.environ.get("BENCH_DIR", "/bench"), int(os.environ.get("ROOM", "0") or 0))
    except OSError as e:
        out["disk"] = {"error": str(e)}
    print("BENCH " + json.dumps(out, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
