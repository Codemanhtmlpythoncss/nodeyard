"""Shared setup for the yardcode tests: import path, throw-away config and data folders, helpers."""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "yardcode", "src"))
sys.path.insert(0, HERE)

from fakeserver import FakeModelServer  # noqa: E402
from yardcode import client, config, frontend, tools  # noqa: E402
from yardcode.agent import Agent, TaskTool  # noqa: E402
from yardcode.session import Session  # noqa: E402
from yardcode.tools.base import Context  # noqa: E402


class Base(unittest.TestCase):
    """A fresh working folder and fresh config/data folders for every test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="yc-test-")
        self.cwd = os.path.join(self.tmp, "work")
        os.makedirs(self.cwd)
        self.home = os.path.join(self.tmp, "home")
        self.data = os.path.join(self.tmp, "data")
        self._old = {k: os.environ.get(k) for k in ("YARDCODE_HOME", "YARDCODE_DATA")}
        os.environ["YARDCODE_HOME"] = self.home
        os.environ["YARDCODE_DATA"] = self.data
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.stop()
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers
    def settings(self, **over):
        return config.Settings(self.cwd, overrides=over, environ={})

    def ctx(self, **over):
        s = self.settings(**over)
        return Context(s, self.cwd)

    def write(self, name, text):
        p = os.path.join(self.cwd, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        return p

    def read(self, name):
        with open(os.path.join(self.cwd, name)) as f:
            return f.read()

    def server(self, script=None, **kw):
        s = FakeModelServer(script, **kw).start()
        self.servers.append(s)
        return s

    def agent(self, srv, fe=None, mode="default", allow_tools=None, **over):
        s = self.settings(permission_mode=mode, **over)
        c = client.Client(srv.url, model="fake-model")
        fe = fe or frontend.Recorder()
        ctx = Context(s, self.cwd, fe)
        tl = tools.builtin(s) + [TaskTool()]
        ag = Agent(s, c, fe, Session(self.cwd, persist=False), ctx, tl, allow_names=allow_tools)
        ag.context_window = srv.n_ctx
        return ag, fe
