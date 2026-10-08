"""Settings back end: picture checks, password swap, sign-out, service validation, job allowlist."""
import base64
import os
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "share", "nodeyard", "dashboard"))

import aiapi  # noqa: E402
import auth  # noqa: E402
import settings  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\0" * 64
WEBP = b"RIFF\0\0\0\0WEBPVP8 " + b"\0" * 64


class FakeStore:
    interval = 2

    def snapshot(self):
        return {"state": {"nodes": [{"name": "debian-1"}], "ai": {}, "agents": {}}}


def make(demo=False, with_auth=True):
    ctx = types.SimpleNamespace(store=FakeStore(), auth=auth.Auth("old-password") if with_auth else None, ai=None,
                                get_routes={}, post_routes={}, post_limits={})
    args = types.SimpleNamespace(demo=demo, nodeyard_bin="", password_file="", state_dir=tempfile.mkdtemp(), listen="local", port=9092, cluster_name="")
    return settings.Settings(ctx, args), ctx


class Pictures(unittest.TestCase):
    def test_kind_comes_from_the_bytes(self):
        self.assertEqual(settings.image_kind(PNG), "png")
        self.assertEqual(settings.image_kind(JPG), "jpg")
        self.assertEqual(settings.image_kind(WEBP), "webp")
        self.assertIsNone(settings.image_kind(b"<svg onload=alert(1)>"))
        self.assertIsNone(settings.image_kind(b"GIF89a"))

    def test_upload_store_style_remove(self):
        s, _ = make()
        bg = s.set_background({"image": "data:image/png;base64," + base64.b64encode(PNG).decode()})
        self.assertEqual(bg["kind"], "png")
        data, ctype = s.background_file()
        self.assertEqual((data, ctype), (PNG, "image/png"))
        self.assertEqual(oct(os.stat(os.path.join(s.dir, "background.png")).st_mode & 0o777), "0o600")
        self.assertEqual(s.background_style({"blur": 99, "dim": -5}), dict(bg, blur=40, dim=0))
        s.set_background({"image": base64.b64encode(JPG).decode()})
        self.assertFalse(os.path.exists(os.path.join(s.dir, "background.png")))
        s.remove_background()
        self.assertEqual(s.background_file(), (None, None))

    def test_rejects_disguised_and_huge_files(self):
        s, _ = make()
        with self.assertRaises(settings.SettingsError):
            s.set_background({"image": base64.b64encode(b"<svg/onload=alert(1)>").decode()})
        with self.assertRaises(settings.SettingsError) as e:
            s.set_background({"image": base64.b64encode(PNG + b"\0" * settings.IMAGE_MAX).decode()})
        self.assertEqual(e.exception.code, 413)
        with self.assertRaises(settings.SettingsError):
            s.set_background({"image": "not base64!!"})

    def test_register_gives_the_upload_its_own_body_limit(self):
        _, ctx = make()
        settings.register(ctx, types.SimpleNamespace(demo=True, state_dir=tempfile.mkdtemp()))
        self.assertGreater(ctx.post_limits["/api/settings/background"], settings.IMAGE_MAX)
        self.assertIn("/api/settings/password", ctx.post_routes)


class ChatDefaults(unittest.TestCase):
    def test_defaults_persist_in_server_preferences(self):
        s, _ = make()
        values = {"system": "Be concise", "temperature": 0.4, "max_tokens": 2048, "compress": "off", "web": True,
                  "skills_auto": False, "autofix": True, "files": "always", "context_length": 16384}
        self.assertEqual(s.set_chat_defaults({"defaults": values})["chat_defaults"], values)
        reopened = settings.Settings(s.ctx, s.args)
        self.assertEqual(reopened.chat_defaults(), values)

    def test_context_and_other_values_are_validated(self):
        s, _ = make()
        for bad in ({"context_length": 511}, {"context_length": 131073}, {"temperature": 2.1},
                    {"max_tokens": 15}, {"max_tokens": 65537}, {"files": "delete-everything"}, {"skills_auto": "yes"}):
            with self.subTest(bad=bad), self.assertRaises(settings.SettingsError):
                s.set_chat_defaults(bad)

    def test_defaults_are_exposed_and_a_save_route_is_registered(self):
        s, ctx = make(demo=True)
        self.assertEqual(s.chat_defaults()["context_length"], 8192)
        self.assertEqual(s.view(None)["chat_defaults"]["context_length"], 8192)
        settings.register(ctx, types.SimpleNamespace(demo=True, state_dir=tempfile.mkdtemp()))
        self.assertIn("/api/settings/chat-defaults", ctx.post_routes)


class SignIn(unittest.TestCase):
    def test_set_password_keeps_this_browser_signs_out_others(self):
        a = auth.Auth("old-password")
        me, other = a.login("1.1.1.1", "old-password"), a.login("2.2.2.2", "old-password")
        a.set_password("new-password", keep=me)
        self.assertTrue(a.valid(me))
        self.assertFalse(a.valid(other))
        self.assertIsNone(a.login("3.3.3.3", "old-password"))
        self.assertIsNotNone(a.login("3.3.3.3", "new-password"))
        with self.assertRaises(ValueError):
            a.set_password("123")

    def test_signout_all(self):
        a = auth.Auth("old-password")
        t = a.login("1.1.1.1", "old-password")
        self.assertEqual(a.signout_all(), 1)
        self.assertFalse(a.valid(t))

    def test_password_rules(self):
        s, _ = make(demo=True)
        with self.assertRaises(settings.SettingsError):
            s.set_password({"password": "abc", "again": "abc"}, None)
        with self.assertRaises(settings.SettingsError):
            s.set_password({"password": "abcdefgh", "again": "abcdefgX"}, None)


class Service(unittest.TestCase):
    def test_validation(self):
        s, _ = make(demo=True)
        for bad in ({"port": 80, "interval": 2, "listen": "local"}, {"port": 9092, "interval": 0, "listen": "local"},
                    {"port": 9092, "interval": 2, "listen": "local; rm -rf /"}, {"port": 9092, "interval": 2, "listen": "999.1.1.1"}):
            with self.assertRaises(settings.SettingsError):
                s.apply_service(bad)
        self.assertEqual(s.apply_service({"port": 9093, "interval": 5, "listen": "local,100.64.0.10"}), {"restarting": False})


class Allowlist(unittest.TestCase):
    def cmd(self, action, **p):
        return aiapi.build_command(action, p, {"debian-1"}, "/k")[1]

    def test_new_actions(self):
        self.assertEqual(self.cmd("split-rm", file="a.gguf"), ["ai", "split", "rm", "a.gguf", "--yes"])
        self.assertEqual(self.cmd("clean", models=True), ["ai", "split", "clean", "--yes", "--models"])
        self.assertEqual(self.cmd("download", repo="o/r", file="m.gguf"), ["ai", "split", "download", "--model", "o/r:m.gguf"])
        self.assertEqual(self.cmd("doctor-fix", only="ip-forward"), ["doctor", "--fix", "--yes", "--only", "ip-forward"])
        self.assertEqual(self.cmd("gate-install", trusted=["192.168.1.7/24"]), ["ai", "gate", "install", "--trusted", "192.168.1.0/24", "--yes"])
        self.assertEqual(self.cmd("disk-limit", node="debian-1", gib="8"), ["ai", "disk", "limit", "debian-1", "8"])

    def test_rejects_bad_input(self):
        for action, p in (("split-rm", {"file": "../../etc/passwd.gguf"}), ("split-rm", {"file": "a/b.gguf"}),
                          ("doctor-fix", {"only": "x;reboot"}), ("gate-install", {"trusted": ["evil"]}),
                          ("disk-limit", {"node": "nope", "gib": "8"}), ("disk-limit", {"node": "debian-1", "gib": "-1"}),
                          ("cluster-name", {"name": "Bad Name"}), ("nope", {})):
            with self.assertRaises(aiapi.AIError):
                aiapi.build_command(action, p, {"debian-1"}, "/k")

    def test_downloads_dont_wait_for_model_changes(self):
        self.assertIn("download", aiapi.SHARED_ACTIONS)
        self.assertNotIn("deploy", aiapi.SHARED_ACTIONS)
        self.assertIn("doctor-fix", aiapi.OUTSIDE_ACTIONS)


class JobsLock(unittest.TestCase):
    def test_exclusive_vs_shared(self):
        j = aiapi.Jobs("/bin/sleep")
        j._run = lambda job, argv: None  # never actually run anything
        j.start("deploy", ["60"], exclusive=True)
        with self.assertRaises(aiapi.AIError):
            j.start("undeploy", ["61"], exclusive=True)
        j.start("download a", ["62"], exclusive=False)
        j.start("download b", ["63"], exclusive=False)
        with self.assertRaises(aiapi.AIError):
            j.start("download a again", ["62"], exclusive=False)


if __name__ == "__main__":
    unittest.main()


class WeakPassword(unittest.TestCase):
    def test_any_stored_password_starts_and_the_minimum_only_applies_when_setting(self):
        a = auth.Auth("ab")  # a short password already stored never stops the dashboard
        self.assertIsNotNone(a.login("1.1.1.1", "ab"))
        with self.assertRaises(ValueError):
            a.set_password("abc")
        a.set_password("x", min_length=1)
        self.assertIsNotNone(a.login("1.1.1.1", "x"))

    def test_the_setting_turns_off_the_minimum(self):
        s, ctx = make(demo=True)
        ctx.auth = auth.Auth("old-password")
        with self.assertRaises(settings.SettingsError):
            s.set_password({"password": "abc", "again": "abc"}, None)
        s.set_weak({"on": True})
        self.assertTrue(s.view(None)["weak_password"])
        s.set_password({"password": "abc", "again": "abc"}, None)
        self.assertIsNotNone(ctx.auth.login("1.1.1.1", "abc"))
