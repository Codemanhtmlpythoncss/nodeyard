import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "share" / "nodeyard" / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import music  # noqa: E402

ART = "https://i1.sndcdn.com/artworks-abc123-xyz-"


class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class _Opener:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, []

    def open(self, request, timeout=None):
        self.calls.append(request.full_url)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return _Answer(json.dumps(self.outcome).encode())


class CoverLinkTests(unittest.TestCase):
    def test_sizes_become_500px(self):
        for size in ("original.png", "original.jpg", "large.jpg", "t300x300.jpg", "crop.jpg", "t500x500.jpg"):
            self.assertEqual(music.Music._sc_cover(ART + size), ART + "t500x500.jpg", size)
        self.assertEqual(music.Music._sc_cover("https://i1.sndcdn.com/avatars-000-a1-large.jpg"),
                         "https://i1.sndcdn.com/avatars-000-a1-t500x500.jpg")

    def test_unknown_names_kept_and_bad_hosts_refused(self):
        self.assertEqual(music.Music._sc_cover("https://a1.sndcdn.com/images/default_avatar_large.png"),
                         "https://a1.sndcdn.com/images/default_avatar_large.png")
        for bad in ("http://i1.sndcdn.com/artworks-a-large.jpg", "https://evil.example/artworks-a-large.jpg",
                    "https://i1.sndcdn.com.evil.example/a-large.jpg", "https://u:p@i1.sndcdn.com/artworks-a-large.jpg", "", None):
            self.assertEqual(music.Music._sc_cover(bad), "", bad)

    def test_flat_entry_cover_comes_from_thumbnails(self):
        # what `yt-dlp --flat-playlist -J scsearch…` gives per entry: a "thumbnails" list and no "thumbnail"
        flat = {"url": "https://soundcloud.com/a/b", "thumbnails": [
            {"id": "mini", "url": ART + "mini.jpg"}, {"id": "t500x500", "url": ART + "t500x500.jpg"},
            {"id": "original", "url": ART + "original.png", "preference": 10}]}
        self.assertEqual(music.Music._sc_entry_cover(flat), ART + "t500x500.jpg")
        # a full look-up's "thumbnail" is the "original" one
        self.assertEqual(music.Music._sc_entry_cover({"thumbnail": ART + "original.png"}), ART + "t500x500.jpg")
        self.assertEqual(music.Music._sc_entry_cover({"thumbnails": [{"url": "https://evil.example/x.jpg"}]}), "")
        self.assertEqual(music.Music._sc_entry_cover({}), "")


class CoverRowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = music.Music(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_entry_row_and_saved_tracks(self):
        row = self.m._entry_row({"webpage_url": "https://soundcloud.com/a/b", "title": "T",
                                 "thumbnails": [{"id": "original", "url": ART + "original.png"}]})
        self.assertEqual(row["artwork_url"], ART + "t500x500.jpg")
        t = self.m._norm_track({"url": "https://soundcloud.com/a/b", "artwork_url": ART + "original.png"})
        self.assertEqual(t["artwork_url"], ART + "t500x500.jpg")

    def test_track_lookup_kept_once_found(self):
        opener = _Opener({"thumbnail_url": ART + "t500x500.jpg"})
        with mock.patch.object(music.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(self.m._sc_track_cover("https://soundcloud.com/a/b"), ART + "t500x500.jpg")
            self.assertEqual(self.m._sc_track_cover("https://soundcloud.com/a/b"), ART + "t500x500.jpg")
        self.assertEqual(len(opener.calls), 1)
        self.assertTrue(opener.calls[0].startswith("https://soundcloud.com/oembed?"))

    def test_track_lookup_gone_is_kept_network_fault_is_not(self):
        gone = _Opener(urllib.error.HTTPError("u", 404, "Not Found", {}, io.BytesIO()))
        with mock.patch.object(music.urllib.request, "build_opener", return_value=gone):
            self.assertEqual(self.m._sc_track_cover("https://soundcloud.com/a/gone"), "")
            self.assertEqual(self.m._sc_track_cover("https://soundcloud.com/a/gone"), "")
        self.assertEqual(len(gone.calls), 1)
        down = _Opener(urllib.error.URLError("offline"))
        with mock.patch.object(music.urllib.request, "build_opener", return_value=down):
            self.m._sc_track_cover("https://soundcloud.com/a/c")
            self.m._sc_track_cover("https://soundcloud.com/a/c")
        self.assertEqual(len(down.calls), 2)

    def test_track_lookup_refuses_bad_links_and_covers(self):
        opener = _Opener({"thumbnail_url": "https://evil.example/x.jpg"})
        with mock.patch.object(music.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(self.m._sc_track_cover("https://soundcloud.com/a/b"), "")
            self.assertEqual(self.m._sc_track_cover("https://evil.example/a/b"), "")
            self.assertEqual(self.m._sc_track_cover("file:///etc/passwd"), "")
        self.assertEqual(len(opener.calls), 1)

    def test_artwork_by_track_serves_the_cached_cover(self):
        cover = ART + "t500x500.jpg"
        folder = os.path.join(self.m.cache_dir(), "covers")
        os.makedirs(folder)
        key = music.hashlib.sha256(cover.encode()).hexdigest()
        Path(folder, key + ".img").write_bytes(b"\xff\xd8jpeg")
        Path(folder, key + ".img.type").write_text("image/jpeg")
        with mock.patch.object(self.m, "_sc_track_cover", return_value=cover):
            self.assertEqual(self.m.artwork(track_url="https://soundcloud.com/a/b"), (b"\xff\xd8jpeg", "image/jpeg"))
        # an "original" link already saved is served at 500 px too
        self.assertEqual(self.m.artwork(image_url=ART + "original.png"), (b"\xff\xd8jpeg", "image/jpeg"))


if __name__ == "__main__":
    unittest.main()
