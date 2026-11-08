"""Size a split model's context (KV cache) to the memory really free across the
nodes, and spread that cache over them.

The context length a model can hold is bounded by its KV cache, which grows with
the number of tokens.  With llama.cpp RPC the cache for a layer lives on whatever
node holds that layer, so the cache is already *physically* spread across the
cluster in proportion to each node's share of the layers.  What was missing was
the arithmetic: nodeyard never worked out how big that per-node cache is, so it
reserved a fixed amount on the main node only and a large context could overflow
a worker.

This module supplies that arithmetic as pure, testable functions:

  * ``gguf_params`` reads the handful of architecture numbers from a GGUF file's
    header (block_count, head_count_kv, key/value length, context_length).
  * ``kv_bytes_per_token`` turns those into bytes of KV cache per token for the
    whole model, at a chosen cache precision (f16 / q8_0 / q4_0).
  * ``max_ctx`` solves for the largest context whose cache fits every node's free
    memory once the cache is spread by layer share -- "the max for the RAM".
  * ``per_node_kv_mib`` reports how much of a given context lands on each node.

It is also a small CLI so ``ai_split.sh`` (bash) can call it; see ``main``.
"""

import json
import struct
import sys

# Bytes per stored element for llama.cpp KV-cache types.  f16 is two bytes; the
# quantized caches store 32-element blocks (q8_0: 34 bytes -> 1.0625 B/elem,
# q4_0: 18 bytes -> 0.5625 B/elem), which is what ``--kv-cache`` selects.
KV_ELEM_BYTES = {"f16": 2.0, "q8_0": 34.0 / 32.0, "q4_0": 18.0 / 32.0}

MIB = 1024 * 1024
CTX_STEP = 256        # round contexts to a whole number of 256-token pages
CTX_MIN = 512
CTX_HARD_CAP = 1048576

# ---------------------------------------------------------------------------
# GGUF header reading (just enough of the format to get the attention shape)
# ---------------------------------------------------------------------------

# GGUF scalar value-type ids -> (struct format, byte size).  Type 9 is an array
# and 8 is a string; both are handled specially in ``_read_value``.
_GGUF_SCALAR = {
    0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2),
    4: ("<I", 4), 5: ("<i", 4), 6: ("<f", 4), 7: ("<?", 1),
    10: ("<Q", 8), 11: ("<q", 8), 12: ("<d", 8),
}


class GGUFError(ValueError):
    """The bytes given are not a GGUF header we can read."""


def _read_value(buf, off, vtype):
    """Return (value, new_offset) for a GGUF metadata value of type ``vtype``."""
    if vtype == 8:  # string: u64 length then raw bytes
        (length,) = struct.unpack_from("<Q", buf, off)
        off += 8
        raw = buf[off:off + length]
        if len(raw) < length:
            raise GGUFError("string runs past the end of the header")
        return raw.decode("utf-8", "replace"), off + length
    if vtype == 9:  # array: elem type (u32), count (u64), then the elements
        (elem_type,) = struct.unpack_from("<I", buf, off)
        (count,) = struct.unpack_from("<Q", buf, off + 4)
        off += 12
        out = []
        for _ in range(count):
            value, off = _read_value(buf, off, elem_type)
            out.append(value)
        return out, off
    spec = _GGUF_SCALAR.get(vtype)
    if spec is None:
        raise GGUFError("unknown GGUF value type %r" % (vtype,))
    fmt, size = spec
    (value,) = struct.unpack_from(fmt, buf, off)
    return value, off + size


def _skip_value(buf, off, vtype):
    """Advance past a GGUF value without materializing it -- so a huge tokenizer
    array (megabytes) costs nothing and parsing still reaches the architecture keys."""
    if vtype == 8:  # string
        (length,) = struct.unpack_from("<Q", buf, off)
        return off + 8 + length
    if vtype == 9:  # array
        (elem_type,) = struct.unpack_from("<I", buf, off)
        (count,) = struct.unpack_from("<Q", buf, off + 4)
        off += 12
        if elem_type in (8, 9):            # arrays of strings or arrays: skip one at a time
            for _ in range(count):
                off = _skip_value(buf, off, elem_type)
            return off
        return off + _GGUF_SCALAR[elem_type][1] * count
    return off + _GGUF_SCALAR[vtype][1]


# The architecture keys we need for KV sizing (matched by suffix, after the arch prefix).
_WANTED = ("block_count", "attention.head_count", "attention.head_count_kv",
           "attention.key_length", "attention.value_length", "embedding_length", "context_length")


def gguf_metadata(header):
    """Parse a GGUF header's metadata, keeping only general.architecture and the
    attention-shape keys and skipping everything else (tokenizers, etc.).

    Only the leading metadata block is needed, so a prefix of the file is enough.
    Tolerant of a truncated prefix: it returns whatever it read before the cut, so
    a short read still works when the wanted keys came early.
    """
    if header[:4] != b"GGUF":
        raise GGUFError("not a GGUF file (bad magic)")
    version, = struct.unpack_from("<I", header, 4)
    if version not in (2, 3):
        raise GGUFError("unsupported GGUF version %d" % version)
    # v2/v3: u32 magic, u32 version, u64 tensor count, u64 metadata kv count
    _tensor_count, n_kv = struct.unpack_from("<QQ", header, 8)
    off = 24
    meta = {}
    try:
        for _ in range(n_kv):
            (klen,) = struct.unpack_from("<Q", header, off)
            off += 8
            key = header[off:off + klen].decode("utf-8", "replace")
            off += klen
            (vtype,) = struct.unpack_from("<I", header, off)
            off += 4
            if key == "general.architecture" or any(key.endswith("." + s) for s in _WANTED):
                value, off = _read_value(header, off, vtype)
                meta[key] = value
            else:
                off = _skip_value(header, off, vtype)
    except (struct.error, IndexError, UnicodeError, OverflowError, ValueError):
        pass  # the header was cut short or malformed; use whatever we collected before the cut
    return meta


def gguf_params(header):
    """Pull the attention shape needed for KV sizing out of a GGUF header.

    Returns a dict with n_layer, n_head_kv, head_dim (K and V lengths), and
    n_ctx_train (the context the model was trained for, an upper bound).  Raises
    GGUFError if the architecture keys are missing.
    """
    meta = gguf_metadata(header)
    arch = meta.get("general.architecture")
    if not isinstance(arch, str) or not arch:
        raise GGUFError("no general.architecture in header")

    def pick(suffix, *, required=True, default=None):
        value = meta.get("%s.%s" % (arch, suffix))
        if isinstance(value, list):  # per-layer arrays: the largest is the safe bound
            value = max(value) if value else None
        if value is None:
            if required:
                raise GGUFError("missing %s.%s in GGUF header" % (arch, suffix))
            return default
        return int(value)

    n_layer = pick("block_count")
    n_head = pick("attention.head_count")
    n_head_kv = pick("attention.head_count_kv", required=False, default=n_head)
    embed = pick("embedding_length", required=False, default=0)
    default_dim = (embed // n_head) if (embed and n_head) else 0
    key_length = pick("attention.key_length", required=False, default=default_dim)
    value_length = pick("attention.value_length", required=False, default=key_length)
    if not key_length:
        raise GGUFError("cannot determine head dimension from GGUF header")
    n_ctx_train = pick("context_length", required=False, default=0)
    return {
        "arch": arch,
        "n_layer": n_layer,
        "n_head_kv": n_head_kv or n_head,
        "key_length": key_length,
        "value_length": value_length or key_length,
        "n_ctx_train": n_ctx_train,
    }


# ---------------------------------------------------------------------------
# KV-cache sizing and the "max context for the RAM" solver
# ---------------------------------------------------------------------------

def kv_bytes_per_token(params, kv_cache="f16"):
    """Bytes of KV cache the whole model adds for each token of context.

    = n_layer * n_head_kv * (key_length + value_length) * bytes-per-element.
    """
    elem = KV_ELEM_BYTES.get(kv_cache)
    if elem is None:
        raise ValueError("kv_cache must be one of %s" % ", ".join(sorted(KV_ELEM_BYTES)))
    per_tok = params["n_layer"] * params["n_head_kv"] * (params["key_length"] + params["value_length"]) * elem
    return int(round(per_tok))


def _round_ctx(ctx, cap):
    """Clamp a context to [CTX_MIN, cap] and round down to a whole CTX_STEP."""
    cap = min(cap, CTX_HARD_CAP)
    ctx = min(ctx, cap)
    ctx = (int(ctx) // CTX_STEP) * CTX_STEP
    if ctx < CTX_MIN:
        ctx = CTX_MIN if cap >= CTX_MIN else (int(cap) // CTX_STEP) * CTX_STEP
    return max(ctx, 0)


def max_ctx(nodes, kv_per_tok, *, headroom=0.9, reserve_mib=0.0, cap_ctx=0):
    """Largest context whose spread-out KV cache fits every node.

    ``nodes`` is a list of ``(free_mib, share)`` where ``share`` is the node's
    fraction of the model's layers (its KV fraction); the shares need not sum to
    exactly 1.  Each node can spend ``free_mib * headroom - reserve_mib`` on KV,
    and holds ``share`` of the per-token cache, so its own ceiling is that budget
    divided by ``share * kv_per_tok``.  The model's context is uniform, so the
    answer is the smallest ceiling across the nodes, capped at ``cap_ctx`` (the
    trained context) when given.
    """
    if kv_per_tok <= 0:
        raise ValueError("kv_per_tok must be positive")
    cap = cap_ctx if cap_ctx and cap_ctx > 0 else CTX_HARD_CAP
    best = None
    for free_mib, share in nodes:
        if share <= 0:
            continue  # holds no layers -> no KV, never the binding node
        budget_bytes = (float(free_mib) * headroom - reserve_mib) * MIB
        if budget_bytes <= 0:
            return 0  # a layer-holding node has no room at all
        ceiling = budget_bytes / (share * kv_per_tok)
        if best is None or ceiling < best:
            best = ceiling
    if best is None:
        return 0
    return _round_ctx(best, cap)


def per_node_kv_mib(ctx, kv_per_tok, shares):
    """MiB of KV cache a context of ``ctx`` tokens puts on each node (by share)."""
    return [round(ctx * kv_per_tok * max(share, 0.0) / MIB, 1) for share in shares]


# ---------------------------------------------------------------------------
# CLI used by ai_split.sh (bash): numbers in, one number or JSON out
# ---------------------------------------------------------------------------

def _parse_nodes(spec):
    """"free:share,free:share,..." -> [(free_mib, share), ...]."""
    nodes = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        free_s, share_s = item.split(":")
        nodes.append((float(free_s), float(share_s)))
    return nodes


def main(argv):
    import argparse
    p = argparse.ArgumentParser(prog="ctxplan", description="Size a split model's context to free RAM.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("params", help="read attention shape from a GGUF header file")
    sp.add_argument("--header", required=True, help="path to a file holding the GGUF header bytes")

    sk = sub.add_parser("kvpertok", help="KV bytes per token from a GGUF header")
    sk.add_argument("--header", required=True)
    sk.add_argument("--kv", default="f16", choices=sorted(KV_ELEM_BYTES))

    sm = sub.add_parser("maxctx", help="largest context that fits the nodes")
    sm.add_argument("--header", required=True)
    sm.add_argument("--kv", default="f16", choices=sorted(KV_ELEM_BYTES))
    sm.add_argument("--nodes", required=True, help='"freeMiB:share,freeMiB:share,..."')
    sm.add_argument("--headroom", type=float, default=0.9)
    sm.add_argument("--reserve-mib", type=float, default=0.0)
    sm.add_argument("--cap-ctx", type=int, default=0, help="override the trained-context cap")

    pk = sub.add_parser("pernodekv", help="per-node KV MiB for a context")
    pk.add_argument("--header", required=True)
    pk.add_argument("--kv", default="f16", choices=sorted(KV_ELEM_BYTES))
    pk.add_argument("--ctx", type=int, required=True)
    pk.add_argument("--shares", required=True, help='"s1,s2,..."')

    args = p.parse_args(argv)

    def header_bytes(path):
        with open(path, "rb") as fh:
            return fh.read()

    if args.cmd == "params":
        print(json.dumps(gguf_params(header_bytes(args.header))))
        return 0
    params = gguf_params(header_bytes(args.header))
    if args.cmd == "kvpertok":
        print(kv_bytes_per_token(params, args.kv))
        return 0
    if args.cmd == "maxctx":
        cap = args.cap_ctx or params.get("n_ctx_train") or 0
        ctx = max_ctx(_parse_nodes(args.nodes), kv_bytes_per_token(params, args.kv),
                      headroom=args.headroom, reserve_mib=args.reserve_mib, cap_ctx=cap)
        print(ctx)
        return 0
    if args.cmd == "pernodekv":
        shares = [float(x) for x in args.shares.split(",") if x.strip()]
        vals = per_node_kv_mib(args.ctx, kv_bytes_per_token(params, args.kv), shares)
        print(" ".join("%.1f" % v for v in vals))
        return 0
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 -- a shape we can't read must exit 1, never crash the caller
        sys.stderr.write("ctxplan: %s\n" % exc)
        sys.exit(1)
