"""Tests for the dashboard's AI back end: which commands the page may start, the task runner, Hugging Face parsing."""
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "share", "nodeyard", "dashboard"))

import agents  # noqa: E402
import aiapi  # noqa: E402
import demoai  # noqa: E402

NODES = {"debian-1", "archlinux-2", "k8s-control"}
KEY = "/etc/nodeyard/secrets/ai-split-api-key"


class Commands(unittest.TestCase):
    def build(self, action, **p):
        return aiapi.build_command(action, p, NODES, KEY)

    def test_deploy_builds_exactly_the_expected_arguments(self):
        title, argv = self.build("deploy", repo="owner/Repo-GGUF", file="model.Q4_K_M.gguf", ctx=4096, nodes=["debian-1", "archlinux-2"], alias="my-model")
        self.assertEqual(argv, ["ai", "split", "deploy", "--model", "owner/Repo-GGUF:model.Q4_K_M.gguf", "--ctx", "4096", "--nodes", "debian-1,archlinux-2",
                                "--alias", "my-model", "--api-key-file", KEY, "--yes"])
        self.assertIn("model.Q4_K_M.gguf", title)

    def test_plan_never_gets_the_key(self):
        _, argv = self.build("plan", repo="o/r", file="a.gguf")
        self.assertNotIn(KEY, argv)
        self.assertEqual(argv[:3], ["ai", "split", "plan"])

    def test_alias_is_made_up_when_missing(self):
        _, argv = self.build("deploy", repo="o/r", file="Some Model_Q4.gguf".replace(" ", "-"))
        self.assertEqual(argv[argv.index("--alias") + 1], "some-model_q4")

    def test_nothing_that_could_be_an_option_or_a_path_gets_through(self):
        bad = [
            dict(repo="--oops/x", file="a.gguf"), dict(repo="o/r", file="--yes.gguf"), dict(repo="o/r", file="../etc/passwd.gguf"),
            dict(repo="o/r", file="/abs/a.gguf"), dict(repo="o/r", file="a.gguf; rm -rf /"), dict(repo="o r/x", file="a.gguf"), dict(repo="o/r", file="a.bin"),
            dict(repo="o/r", file="a.gguf", ctx=1), dict(repo="o/r", file="a.gguf", ctx="lots"), dict(repo="o/r", file="a.gguf", ctx=10 ** 9),
            dict(repo="o/r", file="a.gguf", nodes=["ghost"]), dict(repo="o/r", file="a.gguf", nodes="debian-1"), dict(repo="o/r", file="a.gguf", nodes=["--all"]),
            dict(repo="o/r", file="a.gguf", alias="Bad Alias"), dict(repo="o/r", file="a.gguf", alias="-x"), dict(repo="", file=""),
        ]
        for p in bad:
            with self.assertRaises(aiapi.AIError, msg=str(p)):
                self.build("deploy", **p)

    def test_model_names_for_ollama(self):
        self.assertEqual(self.build("pull", name="llama3.2:3b")[1], ["ai", "model", "install", "llama3.2:3b"])
        self.assertEqual(self.build("pull", name="hf.co/o/r:Q4_K_M")[1][-1], "hf.co/o/r:Q4_K_M")
        for name in ("-x", "a b", "a;b", "", "$(id)"):
            with self.assertRaises(aiapi.AIError, msg=name):
                self.build("pull", name=name)

    def test_fixed_actions(self):
        self.assertEqual(self.build("undeploy")[1], ["ai", "split", "undeploy", "--yes"])
        self.assertEqual(self.build("force-stop")[1], ["ai", "split", "undeploy", "--force", "--yes"])
        self.assertEqual(self.build("split-unload")[1], ["ai", "split", "unload", "--yes"])
        self.assertEqual(self.build("split-load")[1], ["ai", "split", "load", "--yes"])
        self.assertEqual(self.build("agent-install")[1], ["dashboard", "agent", "install", "--yes"])
        self.assertEqual(self.build("test")[1], ["ai", "split", "test", "--api-key-file", KEY])
        self.assertEqual(self.build("reboot-cluster")[1], ["reboot-cluster", "--yes"])
        self.assertIn("reboot-cluster", aiapi.OUTSIDE_ACTIONS)

    def test_unknown_actions_are_refused(self):
        for action in ("", "shell", "rm", "deploy ", "ai"):
            with self.assertRaises(aiapi.AIError, msg=action):
                self.build(action)


class TaskRunner(unittest.TestCase):
    def fake_nodeyard(self, body):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        path = os.path.join(d, "nodeyard")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def wait(self, jobs, jid, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            v = jobs.view(jid, 0)
            if v["status"] != "running":
                return v
            time.sleep(0.05)
        self.fail("the task never finished")

    def test_output_and_exit_status(self):
        jobs = aiapi.Jobs(self.fake_nodeyard('echo "args: $*"; echo second line; exit 0'))
        done = []
        jid = jobs.start("Demo", ["ai", "split", "status"], on_done=lambda job: done.append((job["status"], job["rc"])))
        v = self.wait(jobs, jid)
        self.assertEqual((v["status"], v["rc"]), ("ok", 0))
        self.assertEqual(done, [("ok", 0)])
        self.assertEqual(v["lines"], ["args: --no-color ai split status", "second line"])
        self.assertEqual(jobs.view(jid, 1)["lines"], ["second line"])

    def test_a_failing_command_is_reported(self):
        jobs = aiapi.Jobs(self.fake_nodeyard('echo oops >&2; exit 3'))
        v = self.wait(jobs, jobs.start("Fails", []))
        self.assertEqual((v["status"], v["rc"]), ("failed", 3))
        self.assertEqual(v["lines"], ["oops"])

    def test_only_one_task_at_a_time(self):
        jobs = aiapi.Jobs(self.fake_nodeyard("sleep 1"))
        first = jobs.start("Slow", [])
        with self.assertRaises(aiapi.AIError) as cm:
            jobs.start("Second", [])
        self.assertEqual(cm.exception.code, 409)
        self.wait(jobs, first)
        self.wait(jobs, jobs.start("Now fine", []))

    def test_running_task_can_be_cancelled_and_its_process_group_is_stopped(self):
        jobs = aiapi.Jobs(self.fake_nodeyard("sleep 30 &\nwait"))
        jid = jobs.start("Long task", [])

        self.assertTrue(jobs.view(jid, 0)["cancelable"])
        self.assertTrue(jobs.cancel(jid))
        v = self.wait(jobs, jid, timeout=3)

        self.assertEqual(v["status"], "cancelled")
        self.assertTrue(v["cancel_requested"])
        self.assertFalse(v["cancelable"])
        self.assertIn("Cancellation requested", "\n".join(v["lines"]))

    def test_finished_task_cannot_be_cancelled(self):
        jobs = aiapi.Jobs(self.fake_nodeyard("exit 0"))
        jid = jobs.start("Quick task", [])
        self.wait(jobs, jid)

        with self.assertRaises(aiapi.AIError) as cm:
            jobs.cancel(jid)
        self.assertEqual(cm.exception.code, 409)

    def test_no_nodeyard_means_no_tasks(self):
        for path in ("", "/nonexistent/nodeyard"):
            with self.assertRaises(aiapi.AIError) as cm:
                aiapi.Jobs(path).start("x", [])
            self.assertEqual(cm.exception.code, 501)

    def test_the_task_does_not_inherit_our_environment(self):
        os.environ["SECRET_FOR_TEST"] = "leak"
        self.addCleanup(lambda: os.environ.pop("SECRET_FOR_TEST", None))
        jobs = aiapi.Jobs(self.fake_nodeyard('echo "[$SECRET_FOR_TEST] $NODEYARD_COLOR"'))
        self.assertEqual(self.wait(jobs, jobs.start("env", []))["lines"], ["[] never"])


class HuggingFaceParsing(unittest.TestCase):
    def hf(self, payload):
        h = aiapi.HuggingFace()
        h._get = lambda path, ttl=300: payload  # noqa: E731
        return h

    def test_search_keeps_valid_repos_and_tidies_tags(self):
        h = self.hf([{"id": "o/Good-GGUF", "downloads": 5, "likes": 2, "tags": ["gguf", "text-generation", "license:mit", "region:us", "qwen"], "lastModified": "2026-01-02T00:00:00Z"},
                     {"id": "../bad", "downloads": 1}, {"id": "no-slash"}])
        r = h.search("good", "downloads", 10)
        self.assertEqual([m["id"] for m in r], ["o/Good-GGUF"])
        self.assertEqual(r[0]["tags"], ["text-generation", "qwen"])

    def test_files_group_parts_and_name_the_quality(self):
        tree = [
            {"type": "file", "path": "Model-Q4_K_M.gguf", "lfs": {"size": 4_000_000_000}},
            {"type": "file", "path": "Model-IQ3_XS.gguf", "size": 2_000_000_000},
            {"type": "file", "path": "Big-Q8_0-00001-of-00002.gguf", "lfs": {"size": 20_000_000_000}},
            {"type": "file", "path": "Big-Q8_0-00002-of-00002.gguf", "lfs": {"size": 15_000_000_000}},
            {"type": "file", "path": "README.md", "size": 10}, {"type": "directory", "path": "sub"},
            {"type": "file", "path": "../evil.gguf", "size": 1},
        ]
        files = {f["file"]: f for f in self.hf(tree).files("o/r")}
        self.assertEqual(set(files), {"Model-Q4_K_M.gguf", "Model-IQ3_XS.gguf", "Big-Q8_0-00001-of-00002.gguf"})
        self.assertEqual(files["Model-Q4_K_M.gguf"]["quant"], "Q4_K_M")
        self.assertEqual(files["Model-Q4_K_M.gguf"]["ollama"], "hf.co/o/r:Q4_K_M")
        self.assertEqual(files["Model-IQ3_XS.gguf"]["quant"], "IQ3_XS")
        big = files["Big-Q8_0-00001-of-00002.gguf"]
        self.assertEqual((big["size"], big["parts"], big["split_ok"]), (35_000_000_000, 2, False))
        self.assertEqual(list(files)[0], "Model-IQ3_XS.gguf")  # smallest first

    def test_bad_repo_names_are_refused(self):
        for repo in ("", "x", "a/b/c", "../x", "a b/c"):
            with self.assertRaises(aiapi.AIError, msg=repo):
                self.hf([]).files(repo)


class ChatInputValidation(unittest.TestCase):
    def test_text_and_multiple_inline_images_are_kept_for_the_model(self):
        image = "data:image/png;base64,aGVsbG8="
        messages = [{"role": "user", "content": [
            {"type": "text", "text": "Read these labels"},
            {"type": "image_url", "image_url": {"url": image, "detail": "high"}},
            {"type": "image_url", "image_url": {"url": image}},
        ]}]

        clean = aiapi.clean_chat_messages(messages)

        self.assertEqual(clean, [{"role": "user", "content": [
            {"type": "text", "text": "Read these labels"},
            {"type": "image_url", "image_url": {"url": image}},
            {"type": "image_url", "image_url": {"url": image}},
        ]}])

    def test_remote_or_malformed_images_are_refused(self):
        for url in ("https://example.com/image.png", "data:image/svg+xml;base64,PHN2Zz4=", "data:image/png;base64,%%%", "data:image/png;base64,aaaaa"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                aiapi.clean_chat_messages([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}])

    def test_images_are_only_allowed_in_user_messages_and_have_a_count_limit(self):
        image = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,YQ=="}}
        with self.assertRaises(ValueError):
            aiapi.clean_chat_messages([{"role": "assistant", "content": [image]}])
        with self.assertRaises(ValueError):
            aiapi.clean_chat_messages([{"role": "user", "content": [image] * (aiapi.MAX_CHAT_IMAGES + 1)}])

    def test_ollama_gets_its_string_image_url_format(self):
        messages = aiapi.clean_chat_messages([{"role": "user", "content": [
            {"type": "text", "text": "Read this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ==", "detail": "high"}},
        ]}])

        ollama = aiapi.chat_payload_for_target("ollama:pod-1:model", {"messages": messages})
        split = aiapi.chat_payload_for_target("split", {"messages": messages})

        self.assertEqual(ollama["messages"][0]["content"][1]["image_url"], "data:image/png;base64,YQ==")
        self.assertEqual(split["messages"], messages)


class FakeStore:
    def __init__(self, state):
        self._state = state

    def snapshot(self):
        return {"state": self._state}


class ModelInventory(unittest.TestCase):
    def live(self, state_dir):
        return aiapi.Live(FakeStore({"nodes": [], "pods": [], "services": []}), "", "", state_dir=state_dir)

    def test_invalidation_keeps_the_saved_inventory_visible_until_refresh(self):
        with tempfile.TemporaryDirectory() as d:
            live = self.live(d)
            data = {"ok": True, "nodes": [{"node": "debian-1", "items": [{"kind": "model", "name": "one.gguf", "bytes": 12}]}]}
            live.models_cache = (time.time(), data)
            live._save_models_cache(live.models_cache)

            live._forget_models_cache()

            self.assertTrue(live.models_dirty)
            self.assertEqual(live.models_cache[1]["nodes"][0]["items"][0]["name"], "one.gguf")
            self.assertEqual(live._load_models_cache()[1]["nodes"][0]["items"][0]["name"], "one.gguf")

    def test_inventory_save_failure_is_reported_instead_of_silently_losing_locations(self):
        with tempfile.TemporaryDirectory() as d:
            live = self.live(d)
            live.jobs.bin = self.fake_nodeyard('echo \'{"ok":true,"nodes":[{"node":"debian-1","items":[{"kind":"model","name":"one.gguf","bytes":12}]}]}\'')
            with patch.object(live, "_save_models_cache", return_value=False):
                live._refresh_models(0)

            self.assertIn("couldn't save their locations", live.models_error)
            self.assertEqual(live.models_cache[1]["nodes"][0]["items"][0]["name"], "one.gguf")
            self.assertIn("couldn't save their locations", live.disk_models()["scan_error"])

    def test_failed_refresh_preserves_the_last_known_models(self):
        with tempfile.TemporaryDirectory() as d:
            live = self.live(d)
            previous = {"ok": True, "nodes": [{"node": "debian-1", "items": [{"kind": "model", "name": "one.gguf", "bytes": 12}]}]}
            live.models_cache = (time.time(), previous)

            live._refresh_models(0)

            self.assertIs(live.models_cache[1], previous)
            self.assertTrue(live.models_error)

    def test_timed_out_refresh_preserves_the_last_known_models(self):
        with tempfile.TemporaryDirectory() as d:
            live = self.live(d)
            previous = {"ok": True, "nodes": [{"node": "debian-1", "items": [{"kind": "model", "name": "one.gguf", "bytes": 12}]}]}
            live.models_cache = (time.time(), previous)
            live.jobs.bin = self.fake_nodeyard("exec sleep 2")
            live.MODEL_SCAN_TIMEOUT = 0.03

            live._refresh_models(0)

            self.assertIs(live.models_cache[1], previous)
            self.assertTrue(live.models_error)

    def test_delete_removes_the_model_from_the_persisted_inventory(self):
        with tempfile.TemporaryDirectory() as d:
            live = self.live(d)
            data = {"ok": True, "nodes": [{"node": "debian-1", "items": [
                {"kind": "model", "name": "one.gguf", "bytes": 12},
                {"kind": "cache", "name": "one", "bytes": 8},
            ]}]}
            live.models_cache = (time.time(), data)
            live._save_models_cache(live.models_cache)

            live._forget_models_cache("one.gguf")

            items = live._load_models_cache()[1]["nodes"][0]["items"]
            self.assertEqual(items, [])

    def test_failed_delete_keeps_the_saved_model_location(self):
        with tempfile.TemporaryDirectory() as d:
            live = self.live(d)
            data = {"ok": True, "nodes": [{"node": "debian-1", "items": [{"kind": "model", "name": "one.gguf", "bytes": 12}]}]}
            live.models_cache = (time.time(), data)

            live._model_job_done("split-rm", {"file": "one.gguf"}, {"status": "failed"})

            self.assertEqual(live.models_cache[1]["nodes"][0]["items"][0]["name"], "one.gguf")
            self.assertTrue(live.models_dirty)

    def test_partial_node_scan_is_kept_and_reported(self):
        with tempfile.TemporaryDirectory() as d:
            live = self.live(d)
            script = self.fake_nodeyard('echo \'{"ok":true,"nodes":[{"node":"offline-1","items":[],"scan_error":"Disk scan did not finish on this node"}]}\'')
            live.jobs.bin = script

            live._refresh_models(0)

            self.assertIn("offline-1", live.models_cache[1]["scan_error"])

    def test_partial_scan_retains_last_known_items_for_an_unreachable_node(self):
        with tempfile.TemporaryDirectory() as d:
            live = self.live(d)
            previous = {"ok": True, "nodes": [{"node": "offline-1", "items": [
                {"kind": "model", "name": "saved.gguf", "bytes": 128},
            ]}]}
            live.models_cache = (time.time(), previous)
            live.jobs.bin = self.fake_nodeyard('echo \'{"ok":true,"nodes":[{"node":"offline-1","items":[],"scan_error":"Disk scan did not finish on this node"}]}\'')

            live._refresh_models(0)

            node = live.models_cache[1]["nodes"][0]
            self.assertEqual(node["items"][0]["name"], "saved.gguf")
            self.assertTrue(node["inventory_stale"])
            self.assertIn("offline-1", live.models_cache[1]["scan_error"])

    def test_restart_keeps_models_from_nodes_missing_during_kubernetes_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            live = self.live(d)
            previous = {"ok": True, "nodes": [
                {"node": "debian-1", "items": [{"kind": "model", "name": "saved.gguf", "bytes": 128}]},
                {"node": "archlinux-2", "items": [{"kind": "model", "name": "other.gguf", "bytes": 256}]},
            ]}
            live.models_cache = (time.time(), previous)
            live._save_models_cache(live.models_cache)

            # A new dashboard process starts while Kubernetes has only
            # returned its control node; both workers rejoin after restart.
            restarted = self.live(d)
            restarted.jobs.bin = self.fake_nodeyard(
                'echo \'{"ok":true,"nodes":[{"node":"k8s-control","items":[]}]}\''
            )
            restarted._refresh_models(0)

            nodes = {node["node"]: node for node in restarted.models_cache[1]["nodes"]}
            self.assertEqual(nodes["debian-1"]["items"][0]["name"], "saved.gguf")
            self.assertEqual(nodes["archlinux-2"]["items"][0]["name"], "other.gguf")
            self.assertTrue(nodes["debian-1"]["inventory_stale"])
            self.assertTrue(nodes["archlinux-2"]["inventory_stale"])
            self.assertIn("debian-1", restarted.models_cache[1]["scan_error"])
            self.assertEqual(restarted._load_models_cache()[1]["nodes"], restarted.models_cache[1]["nodes"])

    def fake_nodeyard(self, body):
        path = os.path.join(tempfile.mkdtemp(), "nodeyard")
        self.addCleanup(lambda: __import__("shutil").rmtree(os.path.dirname(path), ignore_errors=True))
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path


class Resolving(unittest.TestCase):
    def state(self, ready=True, auth=True):
        return {"ai": {"split": {"ready": ready, "auth": auth, "alias": "m"}}, "nodes": [], "pods": [
            {"namespace": "ai-inference", "name": "ollama-abc", "status": "Running", "ip": "10.42.0.9", "node": "n1"}],
            "services": [{"namespace": "ai-split", "name": "llama", "cluster_ip": "10.43.0.5"}]}

    def live(self, state, key_file=""):
        return aiapi.Live(FakeStore(state), key_file, "")

    def test_split_uses_the_service_address_and_the_key(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("sekret\n")
        self.addCleanup(os.unlink, f.name)
        host, port, headers, model = self.live(self.state(), f.name)._resolve("split")
        self.assertEqual((host, port, model), ("10.43.0.5", 8080, "m"))
        self.assertEqual(headers, {"Authorization": "Bearer sekret"})

    def test_a_model_that_needs_a_key_but_has_none_says_so(self):
        with self.assertRaises(aiapi.AIError) as cm:
            self.live(self.state(), "/nonexistent")._resolve("split")
        self.assertIn("API key", str(cm.exception))

    def test_an_unloaded_model_is_not_chatted_with(self):
        with self.assertRaises(aiapi.AIError) as cm:
            self.live(self.state(ready=False))._resolve("split")
        self.assertEqual(cm.exception.code, 503)

    def test_ollama_explicit_load_stays_resident_until_unloaded(self):
        live = self.live(self.state())
        live._ollama_pods = lambda: [{"name": "ollama-abc", "ip": "10.42.0.9", "node": "n1"}]
        requests = []
        live._ollama_json = lambda pod, method, path, body=None, timeout=5: (requests.append(body or {}) or (200, {}))

        live.ollama_load("ollama-abc", "llama3.2:3b", True)
        live.ollama_load("ollama-abc", "llama3.2:3b", False)

        self.assertEqual([request["keep_alive"] for request in requests], [-1, 0])

    def test_ollama_overview_reports_loaded_models_on_each_pod(self):
        live = self.live(self.state())
        live._ollama_pods = lambda: [{"name": "ollama-abc", "ip": "10.42.0.9", "node": "n1"}]
        def reply(pod, method, path, body=None, timeout=5):
            if path == "/api/tags":
                return 200, {"models": [{"name": "llama3.2:3b", "size": 100, "details": {}}]}
            return 200, {"models": [{"name": "llama3.2:3b", "size_vram": 80, "expires_at": "never"}]}
        live._ollama_json = reply

        result = live.ollama_overview()

        self.assertTrue(result[0]["models"][0]["loaded"])
        self.assertEqual(result[0]["models"][0]["memory"], 80)

    def test_ollama_targets_must_be_real_pods(self):
        host, port, headers, model = self.live(self.state())._resolve("ollama:ollama-abc:llama3.2:3b")
        self.assertEqual((host, port, headers, model), ("10.42.0.9", 11434, {}, "llama3.2:3b"))
        for bad in ("ollama:ghost:llama3.2", "ollama:ollama-abc:$(id)", "http://evil/", "", "ollama:"):
            with self.assertRaises(aiapi.AIError, msg=bad):
                self.live(self.state())._resolve(bad)

    def test_chat_goes_nowhere_but_the_clusters_own_addresses(self):
        # the target is always looked up in the cluster state, never taken from the request
        with self.assertRaises(aiapi.AIError):
            self.live(self.state())._resolve("ollama:ollama-abc:x@evil.example")


class DemoBackend(unittest.TestCase):
    def test_demo_running_tasks_can_be_cancelled(self):
        backend = demoai.DemoAI(FakeStore({"nodes": []}))
        jid = backend.run("switch", {"local": True, "file": "one.gguf"})
        self.assertTrue(backend.job(jid, 0)["cancelable"])
        self.assertTrue(backend.cancel_job(jid))

        deadline = time.time() + 2
        while time.time() < deadline and backend.job(jid, 0)["status"] == "running":
            time.sleep(0.02)

        self.assertEqual(backend.job(jid, 0)["status"], "cancelled")

    def test_the_demo_chat_streams_events_and_ends(self):
        stream = demoai.FakeStream("Hello there friend.", delay=0)
        lines = []
        while True:
            line = stream.readline()
            if not line:
                break
            lines.append(line.decode())
        data = [ln for ln in lines if ln.startswith("data:")]
        self.assertTrue(data[0].startswith('data: {"choices"'))
        self.assertEqual(data[-1].strip(), "data: [DONE]")
        self.assertIn("timings", data[-2])

    def test_agent_payloads_get_pod_names(self):
        import demo
        src = demo.DemoSource()
        st = src.collect()
        src.agents.annotate(st)
        node = src.agents.payload(st, "yard-3")["yard-3"]
        pods = [p for p in node["processes"] if p["group"]["kind"] == "pod"]
        self.assertTrue(pods)
        self.assertTrue(all("/" in p["group"]["name"] for p in pods))
        self.assertTrue(all("/" in g["name"] for g in node["groups"] if g["kind"] == "pod"))


if __name__ == "__main__":
    unittest.main()
