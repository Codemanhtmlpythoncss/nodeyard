import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import selfupdate  # noqa: E402


class _Music:
    def __init__(self, playback):
        self._pb = playback

    def playback(self):
        return self._pb


def _su(music=None, pending_since=None):
    """A SelfUpdate wired to just the context bits busy() reads for music."""
    su = selfupdate.SelfUpdate.__new__(selfupdate.SelfUpdate)
    su.ctx = SimpleNamespace(music=music)
    su.pending_since = pending_since
    return su


class MusicDeferTests(unittest.TestCase):
    def test_playing_now_defers_the_restart(self):
        su = _su(_Music({"playing": True, "updated": time.time()}))
        self.assertEqual(su.busy(), "music is playing")

    def test_paused_does_not_defer(self):
        su = _su(_Music({"playing": False, "updated": time.time()}))
        self.assertEqual(su.busy(), "")

    def test_stale_heartbeat_does_not_defer(self):
        # the page stopped reporting a while ago: the tab is gone, don't hold the update
        old = time.time() - (selfupdate.MUSIC_FRESH + 30)
        su = _su(_Music({"playing": True, "updated": old}))
        self.assertEqual(su.busy(), "")

    def test_defer_is_capped_so_updates_still_land(self):
        # an update that has been pending longer than the cap goes ahead even with music on
        old_pending = time.time() - (selfupdate.MUSIC_MAX_DEFER + 10)
        su = _su(_Music({"playing": True, "updated": time.time()}), pending_since=old_pending)
        self.assertEqual(su.busy(), "")

    def test_within_cap_still_defers(self):
        recent_pending = time.time() - 5
        su = _su(_Music({"playing": True, "updated": time.time()}), pending_since=recent_pending)
        self.assertEqual(su.busy(), "music is playing")

    def test_no_music_module_is_safe(self):
        su = _su(None)
        self.assertEqual(su.busy(), "")

    def test_broken_music_module_never_blocks(self):
        class Boom:
            def playback(self):
                raise RuntimeError("nope")
        su = _su(Boom())
        self.assertEqual(su.busy(), "")


if __name__ == "__main__":
    unittest.main()
