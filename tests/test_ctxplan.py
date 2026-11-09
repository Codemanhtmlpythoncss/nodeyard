import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import ctxplan  # noqa: E402


def _kv_string(key, text):
    raw = text.encode("utf-8")
    return struct.pack("<Q", len(key)) + key.encode() + struct.pack("<I", 8) + struct.pack("<Q", len(raw)) + raw


def _kv_u32(key, value):
    return struct.pack("<Q", len(key)) + key.encode() + struct.pack("<I", 4) + struct.pack("<I", value)


def _kv_u32_array(key, values):
    body = struct.pack("<I", 4) + struct.pack("<Q", len(values))
    for v in values:
        body += struct.pack("<I", v)
    return struct.pack("<Q", len(key)) + key.encode() + struct.pack("<I", 9) + body


def _kv_str_array(key, values):
    body = struct.pack("<I", 8) + struct.pack("<Q", len(values))
    for v in values:
        raw = v.encode()
        body += struct.pack("<Q", len(raw)) + raw
    return struct.pack("<Q", len(key)) + key.encode() + struct.pack("<I", 9) + body


def make_gguf(pairs):
    """Build a GGUF v3 header holding the given (encoder, ...) metadata bytes."""
    header = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + struct.pack("<Q", len(pairs))
    return header + b"".join(pairs)


# A small llama-shaped model: 32 layers, 8 KV heads, head dim 128, 32k trained ctx.
LLAMA = make_gguf([
    _kv_string("general.architecture", "llama"),
    _kv_u32("llama.block_count", 32),
    _kv_u32("llama.attention.head_count", 32),
    _kv_u32("llama.attention.head_count_kv", 8),
    _kv_u32("llama.embedding_length", 4096),
    _kv_u32("llama.attention.key_length", 128),
    _kv_u32("llama.attention.value_length", 128),
    _kv_u32("llama.context_length", 32768),
])


class ParseTests(unittest.TestCase):
    def test_reads_llama_shape(self):
        p = ctxplan.gguf_params(LLAMA)
        self.assertEqual(p["arch"], "llama")
        self.assertEqual(p["n_layer"], 32)
        self.assertEqual(p["n_head_kv"], 8)
        self.assertEqual(p["key_length"], 128)
        self.assertEqual(p["value_length"], 128)
        self.assertEqual(p["n_ctx_train"], 32768)

    def test_head_dim_falls_back_to_embedding_over_heads(self):
        g = make_gguf([
            _kv_string("general.architecture", "qwen3"),
            _kv_u32("qwen3.block_count", 24),
            _kv_u32("qwen3.attention.head_count", 16),
            _kv_u32("qwen3.embedding_length", 2048),  # no key_length -> 2048/16 = 128
        ])
        p = ctxplan.gguf_params(g)
        self.assertEqual(p["key_length"], 128)
        self.assertEqual(p["value_length"], 128)
        self.assertEqual(p["n_head_kv"], 16)  # head_count_kv absent -> head_count

    def test_per_layer_kv_head_array_takes_the_largest(self):
        g = make_gguf([
            _kv_string("general.architecture", "gpt"),
            _kv_u32("gpt.block_count", 4),
            _kv_u32("gpt.attention.head_count", 8),
            _kv_u32_array("gpt.attention.head_count_kv", [2, 2, 4, 2]),
            _kv_u32("gpt.attention.key_length", 64),
        ])
        p = ctxplan.gguf_params(g)
        self.assertEqual(p["n_head_kv"], 4)

    def test_bad_magic_raises(self):
        with self.assertRaises(ctxplan.GGUFError):
            ctxplan.gguf_params(b"NOPE" + b"\x00" * 64)

    def test_skips_a_huge_array_before_the_arch_keys(self):
        # A big tokenizer-like array must not stop us reaching the shape keys after it.
        g = make_gguf([
            _kv_str_array("tokenizer.ggml.tokens", ["tok%d" % i for i in range(5000)]),
            _kv_u32_array("tokenizer.ggml.token_type", list(range(5000))),
            _kv_string("general.architecture", "gpt-oss"),
            _kv_u32("gpt-oss.block_count", 24),
            _kv_u32("gpt-oss.attention.head_count", 64),
            _kv_u32("gpt-oss.attention.head_count_kv", 8),
            _kv_u32("gpt-oss.attention.key_length", 64),
            _kv_u32("gpt-oss.attention.value_length", 64),
            _kv_u32("gpt-oss.context_length", 131072),
        ])
        p = ctxplan.gguf_params(g)
        self.assertEqual((p["n_layer"], p["n_head_kv"], p["key_length"], p["n_ctx_train"]), (24, 8, 64, 131072))

    def test_tolerates_truncation_after_the_shape_keys(self):
        # Header cut partway through a trailing array: the shape keys came first, so it still parses.
        full = make_gguf([
            _kv_string("general.architecture", "llama"),
            _kv_u32("llama.block_count", 32),
            _kv_u32("llama.attention.head_count", 32),
            _kv_u32("llama.attention.head_count_kv", 8),
            _kv_u32("llama.attention.key_length", 128),
            _kv_u32("llama.attention.value_length", 128),
            _kv_u32("llama.context_length", 32768),
            _kv_str_array("tokenizer.ggml.tokens", ["tok%d" % i for i in range(5000)]),
        ])
        p = ctxplan.gguf_params(full[: len(full) - 40000])   # chop deep into the token array
        self.assertEqual(p["n_layer"], 32)
        self.assertEqual(ctxplan.kv_bytes_per_token(p, "f16"), 131072)

    def test_truncation_before_arch_keys_raises(self):
        g = make_gguf([
            _kv_str_array("tokenizer.ggml.tokens", ["tok%d" % i for i in range(5000)]),
            _kv_string("general.architecture", "llama"),
            _kv_u32("llama.block_count", 32),
        ])
        with self.assertRaises(ctxplan.GGUFError):
            ctxplan.gguf_params(g[:200])   # cut inside the token array, before the arch keys

    def test_garbage_raises_not_crashes(self):
        with self.assertRaises(ctxplan.GGUFError):
            ctxplan.gguf_params(b"GGUF" + struct.pack("<I", 3) + b"\xff" * 200)


class KvMathTests(unittest.TestCase):
    def test_bytes_per_token_f16(self):
        p = ctxplan.gguf_params(LLAMA)
        # 32 layers * 8 kv heads * (128+128) * 2 bytes = 131072 B/token
        self.assertEqual(ctxplan.kv_bytes_per_token(p, "f16"), 131072)

    def test_quantized_cache_is_smaller(self):
        p = ctxplan.gguf_params(LLAMA)
        f16 = ctxplan.kv_bytes_per_token(p, "f16")
        self.assertAlmostEqual(ctxplan.kv_bytes_per_token(p, "q8_0"), f16 * (34 / 32) / 2, delta=2)
        self.assertAlmostEqual(ctxplan.kv_bytes_per_token(p, "q4_0"), f16 * (18 / 32) / 2, delta=2)

    def test_unknown_cache_rejected(self):
        p = ctxplan.gguf_params(LLAMA)
        with self.assertRaises(ValueError):
            ctxplan.kv_bytes_per_token(p, "q2_k")


class MaxCtxTests(unittest.TestCase):
    def setUp(self):
        self.kv = 131072  # bytes/token for the LLAMA fixture, f16

    def test_single_node_capped_at_trained_context(self):
        # 4096 MiB all on one node, headroom 1.0 -> 32768 ceiling, capped to 32768.
        self.assertEqual(ctxplan.max_ctx([(4096, 1.0)], self.kv, headroom=1.0, cap_ctx=32768), 32768)

    def test_headroom_leaves_slack(self):
        got = ctxplan.max_ctx([(4096, 1.0)], self.kv, headroom=0.9, cap_ctx=131072)
        self.assertEqual(got, 29440)  # floor(0.9*32768 / 256) * 256

    def test_binding_node_is_the_tightest_per_share(self):
        # Two nodes each hold half the layers; the one with less free RAM binds.
        nodes = [(1024, 0.5), (3072, 0.5)]
        self.assertEqual(ctxplan.max_ctx(nodes, self.kv, headroom=1.0, cap_ctx=131072), 16384)

    def test_adding_a_node_raises_the_ceiling(self):
        # Spreading the same layers over more RAM lets the context grow.
        one = ctxplan.max_ctx([(1024, 1.0)], self.kv, headroom=1.0, cap_ctx=131072)
        two = ctxplan.max_ctx([(1024, 0.5), (1024, 0.5)], self.kv, headroom=1.0, cap_ctx=131072)
        self.assertEqual(one, 8192)
        self.assertEqual(two, 16384)
        self.assertGreater(two, one)

    def test_node_with_no_layers_is_ignored(self):
        nodes = [(256, 0.0), (4096, 1.0)]  # coordinator-only node holds no KV
        self.assertEqual(ctxplan.max_ctx(nodes, self.kv, headroom=1.0, cap_ctx=32768), 32768)

    def test_no_room_returns_zero(self):
        self.assertEqual(ctxplan.max_ctx([(100, 1.0)], self.kv, headroom=1.0, reserve_mib=200), 0)

    def test_rounds_down_to_pages(self):
        got = ctxplan.max_ctx([(1000, 1.0)], self.kv, headroom=1.0, cap_ctx=131072)
        self.assertEqual(got % ctxplan.CTX_STEP, 0)


class PerNodeKvTests(unittest.TestCase):
    def test_spread_matches_shares(self):
        # 16384 tokens * 131072 B/tok = 2048 MiB total, split 50/50 -> 1024 each.
        vals = ctxplan.per_node_kv_mib(16384, 131072, [0.5, 0.5])
        self.assertEqual(vals, [1024.0, 1024.0])

    def test_whole_cache_on_one_node(self):
        vals = ctxplan.per_node_kv_mib(8192, 131072, [1.0])
        self.assertEqual(vals, [1024.0])


class MoeDetectionTests(unittest.TestCase):
    def test_dense_model_has_no_experts(self):
        self.assertEqual(ctxplan.gguf_params(LLAMA)["n_expert"], 0)

    def test_mixture_of_experts_count_is_read(self):
        g = make_gguf([
            _kv_string("general.architecture", "gpt-oss"),
            _kv_u32("gpt-oss.block_count", 24),
            _kv_u32("gpt-oss.attention.head_count", 64),
            _kv_u32("gpt-oss.attention.head_count_kv", 8),
            _kv_u32("gpt-oss.attention.key_length", 64),
            _kv_u32("gpt-oss.attention.value_length", 64),
            _kv_u32("gpt-oss.context_length", 131072),
            _kv_u32("gpt-oss.expert_count", 128),
        ])
        self.assertEqual(ctxplan.gguf_params(g)["n_expert"], 128)


class ComputeReserveTests(unittest.TestCase):
    def test_params_expose_embedding_width(self):
        self.assertEqual(ctxplan.gguf_params(LLAMA)["n_embd"], 4096)

    def test_embedding_width_falls_back_to_heads_times_head_dim(self):
        g = make_gguf([
            _kv_string("general.architecture", "llama"),
            _kv_u32("llama.block_count", 32),
            _kv_u32("llama.attention.head_count", 32),
            _kv_u32("llama.attention.head_count_kv", 8),
            _kv_u32("llama.attention.key_length", 128),
            _kv_u32("llama.attention.value_length", 128),
            _kv_u32("llama.context_length", 32768),
        ])  # no embedding_length -> 32 heads * 128 = 4096
        self.assertEqual(ctxplan.gguf_params(g)["n_embd"], 4096)

    def test_reserve_never_below_the_floor(self):
        params = ctxplan.gguf_params(LLAMA)
        self.assertEqual(ctxplan.compute_reserve_mib(params, 512), ctxplan.COMPUTE_FLOOR_MIB)

    def test_reserve_grows_with_a_bigger_microbatch_and_width(self):
        wide = {"n_embd": 8192}
        small_ub = ctxplan.compute_reserve_mib(wide, 512)
        big_ub = ctxplan.compute_reserve_mib(wide, 2048)
        self.assertGreaterEqual(big_ub, small_ub)
        self.assertGreater(big_ub, ctxplan.COMPUTE_FLOOR_MIB)  # wide + large batch clears the floor


class CtxPlanTests(unittest.TestCase):
    def setUp(self):
        self.params = ctxplan.gguf_params(LLAMA)
        self.kv = ctxplan.kv_bytes_per_token(self.params, "f16")  # 131072

    def test_recommended_never_exceeds_max(self):
        nodes = [(1536, 0.4), (8192, 0.6)]
        plan = ctxplan.ctx_plan(nodes, self.kv, params=self.params, n_ubatch=512, headroom=0.9)
        self.assertLessEqual(plan["recommended"], plan["max"])
        self.assertGreaterEqual(plan["recommended"], 512)

    def test_recommended_leaves_room_on_a_tight_node(self):
        # A node whose KV fills nearly all its RAM must get a lower recommended ctx,
        # so the compute buffer still fits and the model doesn't OOM-reload-loop.
        nodes = [(1536, 0.4), (8192, 0.6)]
        plan = ctxplan.ctx_plan(nodes, self.kv, params=self.params, n_ubatch=512, headroom=0.9)
        self.assertLess(plan["recommended"], plan["max"])
        self.assertEqual(plan["compute_mib"], ctxplan.COMPUTE_FLOOR_MIB)

    def test_roomy_nodes_reach_the_trained_cap_for_both(self):
        nodes = [(99999, 0.5), (99999, 0.5)]
        plan = ctxplan.ctx_plan(nodes, self.kv, params=self.params, n_ubatch=512, headroom=0.9)
        self.assertEqual(plan["max"], 32768)
        self.assertEqual(plan["recommended"], 32768)

    def test_never_advertises_more_than_the_trained_context(self):
        nodes = [(99999, 1.0)]
        plan = ctxplan.ctx_plan(nodes, self.kv, params=self.params, n_ubatch=512, headroom=1.0)
        self.assertLessEqual(plan["max"], self.params["n_ctx_train"])

    def test_recommended_ctx_matches_max_ctx_with_the_buffer_reserved(self):
        nodes = [(1536, 0.4), (8192, 0.6)]
        rec = ctxplan.recommended_ctx(nodes, self.kv, headroom=0.9, compute_mib=512)
        same = ctxplan.max_ctx(nodes, self.kv, headroom=0.9, reserve_mib=512)
        self.assertEqual(rec, same)


if __name__ == "__main__":
    unittest.main()
