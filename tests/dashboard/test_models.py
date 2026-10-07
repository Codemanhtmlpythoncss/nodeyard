"""The AI model menu: switching models (unload + clean up the old one), running downloaded ones, and Stop."""
import os
import socket
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "share", "nodeyard", "dashboard"))

import aiapi  # noqa: E402
import kube  # noqa: E402

NODES = {"debian-1", "archlinux-2"}
REPO, FILE = "mradermacher/Some-Model-i1-GGUF", "Some-Model.i1-Q4_K_M.gguf"


class SwitchCommand(unittest.TestCase):
    def test_switch_deletes_the_old_model_by_default(self):
        title, argv = aiapi.build_command("switch", {"repo": REPO, "file": FILE, "ctx": 8192}, NODES, "/k")
        self.assertEqual(argv[:3], ["ai", "split", "switch"])
        self.assertIn("--model", argv)
        self.assertNotIn("--keep-old", argv)
        self.assertIn("--yes", argv)
        self.assertTrue(title.startswith("Switch to: "))

    def test_switch_can_keep_the_old_files(self):
        _, argv = aiapi.build_command("switch", {"repo": REPO, "file": FILE, "keep_old": True}, NODES, "/k")
        self.assertIn("--keep-old", argv)
        # only a real true keeps them
        _, argv = aiapi.build_command("switch", {"repo": REPO, "file": FILE, "keep_old": "yes"}, NODES, "/k")
        self.assertNotIn("--keep-old", argv)

    def test_switch_checks_its_input_like_deploy(self):
        for bad in ({"repo": "x", "file": FILE}, {"repo": REPO, "file": "../../etc/passwd.gguf"}, {"repo": REPO, "file": FILE, "nodes": ["nope"]},
                    {"repo": REPO, "file": FILE, "ctx": 10}):
            with self.assertRaises(aiapi.AIError):
                aiapi.build_command("switch", bad, NODES, "/k")


class RunFromDisk(unittest.TestCase):
    def test_a_downloaded_model_runs_from_disk_without_a_repo(self):
        title, argv = aiapi.build_command("switch", {"local": True, "file": "Ornith-1.5-9B-Q4_K_M.gguf"}, NODES, "/k")
        self.assertEqual(argv[argv.index("--model") + 1], "local:Ornith-1.5-9B-Q4_K_M.gguf")
        _, argv = aiapi.build_command("deploy", {"local": True, "file": FILE, "repo": "ignored/Repo"}, NODES, "/k")
        self.assertEqual(argv[argv.index("--model") + 1], "local:" + FILE)

    def test_only_a_plain_file_name_on_disk(self):
        for bad in ("../x.gguf", "dir/x.gguf", "x.txt", "-x.gguf", ""):
            with self.assertRaises(aiapi.AIError):
                aiapi.build_command("switch", {"local": True, "file": bad}, NODES, "/k")

    def test_local_must_really_be_true(self):
        with self.assertRaises(aiapi.AIError):  # (then it needs a repo)
            aiapi.build_command("switch", {"local": "yes", "file": FILE}, NODES, "/k")


class FakeSock:
    def __init__(self):
        self.shut = False

    def shutdown(self, how):
        self.shut = True


class FakeConn:
    def __init__(self):
        self.sock = FakeSock()

    def close(self):
        pass


class Handler:
    def __init__(self, token):
        self.token = token
        self.sent = []
        self.connection, self.peer = socket.socketpair()

    def _token(self):
        return self.token

    def _json(self, obj, code=200, extra=None):
        self.sent.append((code, obj))


class Stop(unittest.TestCase):
    def test_stop_cuts_the_models_connection_for_whoever_asked_only(self):
        ctx = types.SimpleNamespace(get_routes={}, post_routes={}, post_limits={}, store=None)
        aiapi.register(ctx, types.SimpleNamespace(demo=False))
        conn, seen = FakeConn(), {}

        def open_chat(target, payload, on_conn=None):
            on_conn(conn)  # registered before the model starts reading the prompt
            ctx.post_routes["/api/ai/stop"](Handler("someone-else"), {"id": "s1"})
            seen["other"] = conn.sock.shut
            ctx.post_routes["/api/ai/stop"](Handler("me"), {"id": "s1"})
            seen["owner"] = conn.sock.shut
            raise aiapi.AIError("Couldn't reach the model", 502)
        ctx.ai.open_chat = open_chat
        h = Handler("me")
        ctx.post_routes["/api/ai/chat"](h, {"stream_id": "s1", "target": "split", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(seen, {"other": False, "owner": True})
        # finished: the id is forgotten, so a late Stop does nothing
        h2 = Handler("me")
        ctx.post_routes["/api/ai/stop"](h2, {"id": "s1"})
        self.assertEqual(h2.sent[-1][1]["stopped"], False)

    def test_big_attached_files_fit_but_a_huge_conversation_does_not(self):
        ctx = types.SimpleNamespace(get_routes={}, post_routes={}, post_limits={}, store=None)
        aiapi.register(ctx, types.SimpleNamespace(demo=False))
        self.assertGreater(ctx.post_limits["/api/ai/chat"], 4 * 1024 * 1024)
        h = Handler("me")
        ctx.post_routes["/api/ai/chat"](h, {"target": "split", "messages": [{"role": "user", "content": "x" * (aiapi.MAX_CHAT_CHARS + 1)}]})
        self.assertEqual(h.sent[-1][0], 413)


class GpuShare(unittest.TestCase):
    """llama.cpp's devices: the RPC servers first, then the main node's GPU (the last -ts share)."""

    def deployment(self, ts, rpc, gpu=True):
        spec = {"nodeSelector": {"kubernetes.io/hostname": "debian-1"}, "containers": [{"args": ["-ts", ts, "--rpc", rpc]}]}
        if gpu:
            spec["runtimeClassName"] = "nvidia"
        return {"metadata": {"namespace": "ai-split", "name": "llama-main"}, "spec": {"replicas": 1, "template": {"spec": spec}}, "status": {}}

    def ai(self, main, pods=()):
        raw = {"deployments": {"items": [main]}, "services": {"items": []}, "jobs": {"items": []}, "daemonsets": {"items": []}}
        return kube.KubeSource._ai(raw, list(pods))["split"]

    def test_the_last_share_is_the_gpu(self):
        sp = self.ai(self.deployment("9809,4904,2982", "rpc-debian-1.ai-split.svc:50052,rpc-archlinux-2.ai-split.svc:50052"))
        self.assertEqual([(s["node"], s["mib"], s["gpu"]) for s in sp["shares"]],
                         [("debian-1", 9809, False), ("archlinux-2", 4904, False), ("debian-1 GPU", 2982, True)])

    def test_without_a_gpu_every_share_is_an_rpc_server(self):
        sp = self.ai(self.deployment("6000,3000", "rpc-a.x:1,rpc-b.x:1", gpu=False))
        self.assertEqual([s["node"] for s in sp["shares"]], ["a", "b"])

    def test_the_gpu_fills_with_what_the_main_server_read_past_the_rpc_shares(self):
        MiB = 1024 * 1024
        pods = [{"name": "llama-main-x", "namespace": "ai-split", "status": "Running", "mem": (1000 + 500 + 300) * MiB},
                {"name": "rpc-a-y", "namespace": "ai-split", "status": "Running", "mem": 1048 * MiB},
                {"name": "rpc-b-y", "namespace": "ai-split", "status": "Running", "mem": 548 * MiB}]
        ld = kube.load_progress(self.deployment("1000,500,600", "rpc-a.x:1,rpc-b.x:1"), pods, [1000, 500, 600], ["a", "b"], "debian-1")
        self.assertEqual([(n["node"], n["got_mib"]) for n in ld["nodes"]], [("a", 1000), ("b", 500), ("debian-1 GPU", 300)])


class GpuShareDirectIO(unittest.TestCase):
    def test_the_gpu_share_is_read_on_the_card_when_loading_with_direct_io(self):
        MiB = 1024 * 1024
        main = {"spec": {"replicas": 1, "template": {"spec": {"runtimeClassName": "nvidia", "nodeSelector": {"kubernetes.io/hostname": "debian-1"},
                                                              "containers": [{"args": ["--load-mode", "dio"]}]}}}, "status": {}}
        pods = [{"name": "llama-main-x", "namespace": "ai-split", "status": "Running", "mem": 500 * MiB},
                {"name": "rpc-a-y", "namespace": "ai-split", "status": "Running", "mem": 1048 * MiB}]
        ld = kube.load_progress(main, pods, [1000, 600], ["a"], "debian-1")
        self.assertIsNone(ld["file_pct"])  # (the main server's memory says nothing about the file)
        self.assertEqual(ld["nodes"][1], {"node": "debian-1 GPU", "got_mib": 0, "share_mib": 600})
        state = {"ai": {"split": {"load": ld}}, "nodes": [{"name": "debian-1", "hw": {"gpu_live": [{"mem_used": 364 * MiB}]}}]}
        kube.gpu_progress(state)
        self.assertEqual(ld["nodes"][1]["got_mib"], 300)
        self.assertEqual(ld["pct"], 81.2)


if __name__ == "__main__":
    unittest.main()
