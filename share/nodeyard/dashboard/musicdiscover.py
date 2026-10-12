"""Finding new songs a listener is likely to enjoy, from their own likes and listening history.

No account, API key or OAuth is involved: the dashboard only reads SoundCloud's public pages through yt-dlp (the same
way Music search works). The pieces here are pure functions so they can be tested without the network:

* profile()   - a taste profile: how much each known track counts (likes, full listens, repeat plays, quick skips,
                with recent listening counting most), favourite artists, recurring words (hardtekk, phonk, slowed...)
                and the seed tracks to ask SoundCloud about.
* plan()      - which seeds and searches this refresh uses (it rotates, so refreshes find different songs).
* rank()      - scores every candidate the seeds and searches turned up, explains each pick, and keeps the list
                varied (at most two songs per artist).
"""
import math
import re
import time

# Words that say nothing about taste (or only about the upload).
STOP = set("""a an and are at be by for from in into is it its of on or the to with you your my me we our this that these those i im
    feat ft featuring prod produced official audio video lyric lyrics lyrical visualizer visualiser hq hd 4k free download dl out now new
    full version ver original mix edit extended radio clip music song track records record ep lp vol pt part remastered remaster x vs
    soundcloud youtube tiktok premiere exclusive description link bio www com http https""".split())
# Words that do describe taste but are too broad to search for on their own.
GENERIC = set("remix remixed bootleg flip cover instrumental beat type live session acoustic".split())
VARIANTS = set("slowed reverb sped speed nightcore bassboosted boosted 8d".split())
SKIP_SECONDS = 25            # stopped before this (and not finished): a skip
HALF_LIFE_DAYS = 21          # listening this long ago counts half


def clean_title(title):
    """A title without the upload clutter: (prod. X), [Free Download], emoji. Used to spot re-uploads of known songs."""
    t = str(title or "").lower()
    t = re.sub(r"[\(\[\{][^\)\]\}]*(prod|free|download|official|video|audio|out now|premiere|exclusive|dl)[^\)\]\}]*[\)\]\}]", " ", t)
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


def norm_artist(artist):
    return re.sub(r"[^\w]+", "", str(artist or "").lower(), flags=re.UNICODE)


def words(text):
    return [w for w in re.findall(r"[a-z0-9]+", str(text or "").lower()) if len(w) >= 3 and not w.isdigit() and w not in STOP]


def split_local(name, folder=""):
    """'Artist - Title' file names give an artist; otherwise the folder may be one."""
    name = str(name or "")
    m = re.match(r"^\s*(?:\d+[\s.\-_]+)?(.+?)\s+[-–—]\s+(.+)$", name)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    leaf = str(folder or "").split("/")[-1]
    return (leaf if leaf and leaf.lower() not in ("music", "downloads", "soundcloud") else ""), name


def track_key(t):
    return str(t.get("url") or t.get("path") or "")


def profile(likes, history, local, dislikes, now=None):
    now = now or time.time()
    tracks = {}       # key -> {"w", "title", "artist", "url"}

    def entry(t):
        key = track_key(t)
        if not key:
            return None
        e = tracks.get(key)
        if e is None:
            if t.get("url"):
                artist, title = t.get("artist") or "", t.get("title") or ""
            else:
                artist, title = split_local(t.get("name"), t.get("folder"))
            e = tracks[key] = {"w": 0.0, "title": title, "artist": artist, "url": t.get("url") or "", "plays": 0, "skips": 0}
        return e

    n = max(1, len(likes))
    for i, t in enumerate(likes):            # newest first: recent likes say more about taste today
        e = entry(t)
        if e is not None:
            e["w"] += 4.0 * (1 - 0.5 * i / n)
    for row in history:
        e = entry(row)
        if e is None:
            continue
        age_days = max(0.0, (now - float(row.get("played_at") or now)) / 86400.0)
        decay = 0.5 ** (age_days / HALF_LIFE_DAYS)
        seconds = float(row.get("seconds") or 0)
        if row.get("completed"):
            score = 2.5
        elif seconds < SKIP_SECONDS:
            score = -1.2
            e["skips"] += 1
        else:
            score = 1.0 + min(1.0, seconds / 120.0)
        e["plays"] += 1
        e["w"] += score * decay
    for t in local:                           # owning a song is a mild signal on its own
        e = entry(t)
        if e is not None:
            e["w"] += 0.3

    artists, tokens = {}, {}
    for e in tracks.values():
        if e["w"] <= 0:
            continue
        a = norm_artist(e["artist"])
        if a:
            artists[a] = artists.get(a, 0.0) + e["w"]
        artist_words = set(words(e["artist"]))
        for w in set(words(e["title"])) - artist_words:
            tokens.setdefault(w, [0.0, 0])
            tokens[w][0] += e["w"]
            tokens[w][1] += 1
    disliked_artists = {}
    for d in dislikes:
        a = norm_artist(d.get("artist"))
        if a:
            disliked_artists[a] = disliked_artists.get(a, 0) + 1
            artists[a] = artists.get(a, 0.0) - 3.0
    # a word in only one song is that song's name, not a taste
    tok = {w: v[0] * (1 + math.log(v[1])) for w, v in tokens.items() if v[1] >= 2 or v[0] >= 6}
    names = {}
    for e in tracks.values():
        a = norm_artist(e["artist"])
        if a and a not in names and e["artist"]:
            names[a] = e["artist"]
    seeds = sorted((dict(e, key=k) for k, e in tracks.items() if e["url"] and e["w"] > 0), key=lambda e: -e["w"])
    return {
        "tracks": tracks, "artists": artists, "artist_names": names, "tokens": tok, "seeds": seeds,
        "token_songs": {w: v[1] for w, v in tokens.items()},
        "known_urls": {k for k in tracks if k.startswith("http")} | {d.get("url") for d in dislikes if d.get("url")},
        "known_titles": {clean_title(e["title"]) for e in tracks.values() if e["title"]},
        "disliked_urls": {d.get("url") for d in dislikes if d.get("url")}, "disliked_artists": disliked_artists,
    }


def plan(p, rotation=0, n_seeds=5, n_artist=2, n_word=2):
    """(seed tracks, [(query, kind)]): the strongest seeds every time, plus a rotating few, so each refresh finds more."""
    seeds = p["seeds"]
    keep = seeds[:3]
    rest = seeds[3:15]
    if rest:
        start = (rotation * 2) % len(rest)
        keep += (rest[start:] + rest[:start])[:max(0, n_seeds - len(keep))]
    queries = []
    good_artists = [a for a, w in sorted(p["artists"].items(), key=lambda x: -x[1]) if w > 0 and p["disliked_artists"].get(a, 0) < 2]
    if good_artists:
        start = rotation % max(1, min(4, len(good_artists)))
        for a in (good_artists[start:] + good_artists[:start])[:n_artist]:
            queries.append((p["artist_names"].get(a, a), "artist"))
    # only words that run through several songs make a search (one song's title words find covers of that song)
    good_words = [w for w, _ in sorted(p["tokens"].items(), key=lambda x: -x[1])
                  if w not in GENERIC and w not in VARIANTS and p.get("token_songs", {}).get(w, 0) >= 2]
    if good_words:
        start = rotation % max(1, min(4, len(good_words)))
        for w in (good_words[start:] + good_words[:start])[:n_word]:
            queries.append((w, "word"))
    return keep, queries


def rank(candidates, p, limit=40, per_artist=2):
    """candidates: url -> {title, artist, genres, tags, likes, duration_ms, via: [(kind, label, weight)]}. Best first."""
    max_artist = max([w for w in p["artists"].values() if w > 0] or [1.0])
    max_tok = max(list(p["tokens"].values()) or [1.0])
    known_titles = p["known_titles"]
    out = []
    for url, c in candidates.items():
        if url in p["known_urls"] or url in p["disliked_urls"]:
            continue
        title_key = clean_title(c.get("title"))
        if title_key and title_key in known_titles:
            continue                           # another upload of a song they already have
        a = norm_artist(c.get("artist"))
        if p["disliked_artists"].get(a, 0) >= 2:
            continue
        found = sum(w for _, _, w in c.get("via", []))
        affinity = max(0.0, p["artists"].get(a, 0.0)) / max_artist if a else 0.0
        text = " ".join([c.get("title") or ""] + list(c.get("genres") or []) + list(c.get("tags") or []))
        cw = set(words(text))
        match = sum(p["tokens"].get(w, 0.0) for w in cw) / max_tok
        variant_dislike = any(w in VARIANTS and w not in p["tokens"] for w in cw)
        dur = (c.get("duration_ms") or 0) / 1000.0
        score = (1.6 * found + 1.2 * affinity + 0.8 * min(1.5, match) + 0.12 * math.log10(1 + (c.get("likes") or 0))
                 - (1.0 if 0 < dur < 45 else 0) - (0.8 if dur > 12 * 60 else 0) - (0.25 if variant_dislike else 0)
                 - 0.8 * p["disliked_artists"].get(a, 0))
        # why: the biggest reason
        why = ""
        seeds = [label for kind, label, w in sorted(c.get("via", []), key=lambda v: -v[2]) if kind == "seed"]
        top_word = max(((w, p["tokens"][w]) for w in cw if w in p["tokens"] and w not in GENERIC), key=lambda x: x[1], default=None)
        if affinity >= 0.5 and c.get("artist"):
            why = "More from %s, an artist you play a lot" % c["artist"]
        elif seeds:
            why = "Because you like %s" % seeds[0]
        elif top_word:
            why = "Matches your taste for %s" % top_word[0]
        elif c.get("via"):
            why = "Picked from a search for %s" % c["via"][0][1]
        out.append(dict({k: v for k, v in c.items() if k != "via"}, url=url, score=round(score, 3), why=why))
    out.sort(key=lambda x: -x["score"])
    picked, per = [], {}
    for c in out:
        a = norm_artist(c.get("artist")) or c["url"]
        if per.get(a, 0) >= per_artist:
            continue
        per[a] = per.get(a, 0) + 1
        picked.append(c)
        if len(picked) >= limit:
            break
    return picked
