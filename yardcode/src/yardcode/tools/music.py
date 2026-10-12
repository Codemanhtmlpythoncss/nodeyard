"""Nodeyard dashboard Music tools for an AI chat."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .base import Result, Tool, ToolError


def _request(path, body=None):
    base = (os.environ.get("NODEYARD_MUSIC_URL") or "").rstrip("/")
    if not base:
        raise ToolError("Nodeyard Music is unavailable in this chat. Turn on the Music skill and try again.")
    headers = {"Accept": "application/json"}
    cookie = os.environ.get("NODEYARD_MUSIC_COOKIE") or ""
    if cookie:
        headers["Cookie"] = cookie
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers.update({"Content-Type": "application/json", "X-Nodeyard": "1", "X-Nodeyard-AI": "1", "Origin": base})
    req = urllib.request.Request(base + path, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read(2_000_000).decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read(12000).decode("utf-8")).get("error", "")
        except (ValueError, UnicodeDecodeError):
            detail = ""
        raise ToolError(detail or "Nodeyard Music returned HTTP %d." % e.code)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        raise ToolError("Couldn't reach Nodeyard Music: %s" % e)
    if not result.get("ok"):
        raise ToolError(result.get("error") or "Nodeyard Music couldn't complete that request.")
    return result


class MusicLibrary(Tool):
    name = "MusicLibrary"
    kind = "read"
    description = ("Read the user's Nodeyard Music overview, liked songs, listening history, or local music library. "
                   "When creating a playlist, read likes and recent history first. If the user asks for a playlist but has not "
                   "given a vibe or purpose, ask a short follow-up in chat before choosing tracks. This only accesses their own saved music.")
    parameters = {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["overview", "liked", "history", "library"], "description": "Which part of the user's music to read."},
        "query": {"type": "string", "description": "Optional title or folder words to filter local tracks."},
        "offset": {"type": "integer", "description": "Starting row for the next page; pages contain up to 50 songs."}}, "required": []}

    def run(self, args, ctx):
        action = str(args.get("action") or "overview")
        if action not in ("overview", "liked", "history", "library"):
            raise ToolError("action must be overview, liked, history or library.")
        query = urllib.parse.urlencode({"action": action, "q": str(args.get("query") or "")[:100],
                                        "offset": max(0, min(10000, int(args.get("offset") or 0))), "limit": 50})
        result = _request("/api/music/ai?" + query)
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        return Result(text, summary="Read music %s" % action, preview=["%s songs" % result.get("total", result.get("local_track_count", 0))])


class CreateMusicPlaylist(Tool):
    name = "CreateMusicPlaylist"
    kind = "edit"
    description = ("Create a saved playlist in Nodeyard Music from tracks returned by MusicLibrary. Ask the user for a vibe if needed, "
                   "then choose songs from their likes, listening history, or local library. The dashboard asks the user before saving.")
    parameters = {"type": "object", "properties": {
        "name": {"type": "string", "description": "Playlist name."},
        "tracks": {"type": "array", "description": "Selected track objects returned by MusicLibrary, each identified by url or path.",
                   "items": {"type": "object", "properties": {"url": {"type": "string"}, "path": {"type": "string"}}, "required": []}}},
        "required": ["name", "tracks"]}

    def run(self, args, ctx):
        tracks = args.get("tracks")
        if not isinstance(tracks, list) or not tracks:
            raise ToolError("Choose at least one track from MusicLibrary.")
        result = _request("/api/music/ai", {"action": "create_playlist", "name": str(args.get("name") or "")[:120],
                                             "tracks": tracks[:200]})
        playlist = result.get("playlist") or {}
        return Result("Saved %r with %d tracks." % (playlist.get("name", "playlist"), len(playlist.get("tracks") or [])),
                      summary="Saved playlist · %d tracks" % len(playlist.get("tracks") or []))


MUSIC_TOOLS = [MusicLibrary, CreateMusicPlaylist]
