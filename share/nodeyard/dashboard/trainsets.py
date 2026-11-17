"""Ready-made training data for Train AI, and turning any data file into exactly what the model reads.

The catalog is fixed here (no URL comes from the page): nodeyard's own starter chat set, generated on this machine,
and three downloads with licences that allow training: TinyStories, Dolly 15k and public-domain novels from Project
Gutenberg. Downloads stream to disk in pieces, so a big file never sits in the dashboard's memory.
"""
import codecs
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
JOBS_DIR = os.path.join(os.path.dirname(HERE), "jobs")
UA = "nodeyard-dashboard (Train AI dataset download)"
GUTENBERG = [(1342, "Pride and Prejudice"), (11, "Alice's Adventures in Wonderland"), (1661, "The Adventures of Sherlock Holmes"),
             (84, "Frankenstein"), (98, "A Tale of Two Cities"), (2701, "Moby-Dick"), (76, "Adventures of Huckleberry Finn")]

CATALOG = [
    {"id": "starter-chat", "name": "nodeyard starter chat", "file": "starter-chat.txt", "size": 2_500_000, "format": "conversations in text",
     "source": "Generated on this machine by nodeyard: nothing is downloaded.", "licence": "Made for nodeyard; yours to use.",
     "what": "About 18,000 short User/Assistant conversations: greetings, sums, counting, spelling, days and months, units, opposites, "
             "what the AI words on this page mean, and nodeyard/Kubernetes commands. Every answer is computed, so every answer is right.",
     "good_for": "Seeing a chatbot learn in minutes: the reply format, polite short answers, and patterns like small sums. "
                 "Tick “learn only the replies”. It's narrow: it won't teach general English."},
    {"id": "tinystories", "name": "TinyStories (validation set)", "file": "tinystories.txt", "size": 22_502_601, "format": "plain text",
     "url": "https://huggingface.co/datasets/roneneldan/TinyStories/resolve/main/TinyStoriesV2-GPT4-valid.txt",
     "source": "huggingface.co/datasets/roneneldan/TinyStories", "licence": "CDLA-Sharing-1.0",
     "what": "About 27,000 short stories in very simple English, made for training tiny models from scratch. "
             "(The full set, 2.2 GB, is on the same page; this sample is the right size for home hardware.)",
     "good_for": "Fluent English from a small model: a few million weights learn to write short, coherent stories."},
    {"id": "dolly", "name": "Dolly 15k instructions", "file": "dolly-15k.jsonl", "size": 13_085_339, "format": "JSONL (instruction, context, response)",
     "url": "https://huggingface.co/datasets/databricks/databricks-dolly-15k/resolve/main/databricks-dolly-15k.jsonl",
     "source": "huggingface.co/datasets/databricks/databricks-dolly-15k", "licence": "CC BY-SA 3.0: credit Databricks, share alike",
     "what": "15,000 questions and instructions answered by people: facts, brainstorming, summaries, classification.",
     "good_for": "A chatbot that tries to answer anything. It needs a bigger model and hours of training before answers make sense; "
                 "mix it with TinyStories so the model also learns plain English."},
    {"id": "gutenberg", "name": "Seven classic novels", "file": "gutenberg-classics.txt", "size": 4_700_000, "format": "plain text",
     "urls": ["https://www.gutenberg.org/cache/epub/%d/pg%d.txt" % (n, n) for n, _ in GUTENBERG],
     "source": "gutenberg.org", "licence": "Public domain in the USA (Project Gutenberg's header, footer and licence are removed).",
     "what": ", ".join(t for _, t in GUTENBERG) + ".",
     "good_for": "Rich 19th-century English: the model learns to write in that style. Under 5 MB, so a small model."},
]
BY_ID = {c["id"]: c for c in CATALOG}


def strip_gutenberg(text):
    """Keep only the book: drop Project Gutenberg's header and footer (the lines around *** START/END OF … ***)."""
    text = text.replace("\r\n", "\n").lstrip("﻿")
    # The marker can wrap onto a second line ("*** START OF … SHERLOCK\nHOLMES ***").
    start = re.search(r"^\*\*\* ?START OF[^*\n]*(?:\n[^*\n]*)?\*\*\*.*$|^\*\*\* ?START OF.*$", text, re.M)
    end = re.search(r"^\*\*\* ?END OF.*$", text, re.M)
    body = text[start.end() if start else 0: end.start() if end else len(text)]
    return re.sub(r"\n{3,}", "\n\n", body).strip() + "\n"


class TinyStoriesWriter:
    """Streams TinyStories: stories are separated by <|endoftext|> lines; they become blank lines."""

    def __init__(self, out):
        self.out, self.rest, self.blank = out, "", True

    def feed(self, chunk):
        text = self.rest + chunk
        lines = text.split("\n")
        self.rest = lines.pop()
        for line in lines:
            self.line(line)

    def line(self, line):
        line = line.rstrip()
        if line.strip() == "<|endoftext|>" or not line.strip():
            if not self.blank:
                self.out.write("\n")
                self.blank = True
            return
        self.out.write(line + "\n")
        self.blank = False

    def close(self):
        if self.rest:
            self.line(self.rest)


class CurlStream:
    """Enough of urlopen's response for _run: read(), headers, and use in a with block."""

    def __init__(self, url):
        self.proc = subprocess.Popen(["curl", "-fsSL", "--max-time", "3600", "-A", UA, url], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.headers = {}

    def read(self, n=-1):
        return self.proc.stdout.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.proc.stdout.close()
        code = self.proc.wait()
        if code != 0 and exc[0] is None:
            raise OSError("curl failed (exit %d): %s" % (code, self.proc.stderr.read().decode("utf-8", "replace").strip()[:200]))


class Fetcher:
    def __init__(self, data_dir, room):
        self.data_dir = data_dir
        self.room = room            # callable: bytes still allowed in the data folder
        self.state = {}
        self.lock = threading.Lock()

    def catalog(self):
        out = []
        for c in CATALOG:
            have = os.path.isfile(os.path.join(self.data_dir, c["file"]))
            out.append(dict({k: v for k, v in c.items() if k not in ("url", "urls")}, have=have, job=self.state.get(c["id"])))
        return out

    def start(self, cid):
        c = BY_ID.get(cid)
        if not c:
            raise ValueError("There's no dataset called %s." % cid)
        with self.lock:
            job = self.state.get(cid)
            if job and job["state"] == "downloading":
                return job
            if c["size"] > self.room():
                raise OverflowError("Not enough room in your training data (20 GB): delete some files first.")
            job = self.state[cid] = {"state": "downloading", "got": 0, "total": c["size"], "error": None, "started": time.time(), "file": c["file"]}
        threading.Thread(target=self._run, args=(c, job), daemon=True).start()
        return job

    def _open(self, url):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60)
        except urllib.error.URLError as e:
            # Some Pythons (python.org's on a Mac) ship without root certificates; curl uses the system's.
            if "CERTIFICATE_VERIFY_FAILED" not in str(e) or not shutil.which("curl"):
                raise
            return CurlStream(url)

    def _run(self, c, job):
        tmp = os.path.join(self.data_dir, ".dl-" + c["id"])
        try:
            with open(tmp, "w", encoding="utf-8") as out:
                if c["id"] == "starter-chat":
                    sys.path.insert(0, JOBS_DIR)
                    try:
                        import starter_dataset
                    finally:
                        sys.path.remove(JOBS_DIR)
                    text = starter_dataset.generate()
                    out.write(text)
                    job["got"] = len(text)
                elif "urls" in c:
                    for i, url in enumerate(c["urls"]):
                        with self._open(url) as r:
                            book = r.read(8 * 1024 * 1024).decode("utf-8", "replace")
                        job["got"] += len(book)
                        out.write(("\n\n" if i else "") + strip_gutenberg(book))
                        time.sleep(1)   # be gentle with Project Gutenberg
                else:
                    writer = TinyStoriesWriter(out) if c["id"] == "tinystories" else None
                    decode = codecs.getincrementaldecoder("utf-8")("replace").decode   # a character can span two blocks
                    with self._open(c["url"]) as r:
                        total = int(r.headers.get("Content-Length") or 0)
                        if total:
                            job["total"] = total
                        while True:
                            block = r.read(1024 * 1024)
                            text = decode(block, final=not block)
                            if writer:
                                writer.feed(text)
                            else:
                                out.write(text)
                            if not block:
                                break
                            job["got"] += len(block)
                    if writer:
                        writer.close()
            os.replace(tmp, os.path.join(self.data_dir, c["file"]))
            job.update(state="done", got=job["total"], finished=time.time())
        except Exception as e:   # noqa: BLE001 - shown on the page
            job.update(state="failed", error="%s: %s" % (type(e).__name__, e))
            try:
                os.remove(tmp)
            except OSError:
                pass


def _training_data():
    sys.path.insert(0, JOBS_DIR)
    try:
        import training_data
    finally:
        sys.path.remove(JOBS_DIR)
    return training_data


def _kind(src, training_data):
    with open(src, "r", encoding="utf-8", errors="replace") as f:
        head = f.read(4000)
    return training_data.detect_format(head.replace("\r\n", "\n").lstrip("\ufeff"))


def _jsonl_pieces(src, training_data, info):
    """The pieces of a JSONL file, streamed twice: once to see whether its replies start with speaker names (a
    script), once to yield them, with the names taken off if so."""
    def pieces():
        with open(src, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    text = training_data.record_to_text(json.loads(line))
                except ValueError:
                    text = None
                if text is None:
                    info["skipped"] += 1
                    continue
                yield text
    replies = named = 0
    speakers = set()
    for text in pieces():
        r, n = training_data.count_named_replies(text, speakers)
        replies, named = replies + r, named + n
    script = training_data.is_script(replies, named, speakers)
    names = Counter()
    info["skipped"] = 0
    for text in pieces():
        yield training_data.drop_speaker_names(text, names) if script else text
    info["names_removed"], info["names"] = sum(names.values()), [n for n, _ in names.most_common(5)]


def convert_file(src, dst):
    """Write exactly what the trainer will read from `src` into `dst`, streaming JSONL so size doesn't matter."""
    training_data = _training_data()
    kind = _kind(src, training_data)
    info = {"skipped": 0, "names_removed": 0, "names": []}
    records = 0
    tmp = dst + ".tmp"
    with open(src, "r", encoding="utf-8", errors="replace") as f, open(tmp, "w", encoding="utf-8") as out:
        if kind == "jsonl":
            for text in _jsonl_pieces(src, training_data, info):
                out.write(("\n\n" if records else "") + text)
                records += 1
            out.write("\n")
        elif kind == "json":
            if os.path.getsize(src) > 200 * 1024 * 1024:
                raise ValueError("JSON lists over 200 MB can't be converted here: save it as JSONL (one record per line) instead.")
            text, rep = training_data.normalize(f.read())
            out.write(text)
            records, info = rep["records"], rep
        else:
            for line in f:
                out.write(line.replace("\r\n", "\n").lstrip("\ufeff"))
    os.replace(tmp, dst)
    return {"format": kind, "records": records, "skipped": info["skipped"], "names_removed": info.get("names_removed", 0),
            "names": info.get("names", [])}


# -- plays and poems in plain text ------------------------------------------------------------------------------
# A printed play, e.g. Project Gutenberg's Complete Works of Shakespeare: a speech starts with the speaker's name
# and a full stop, either on the same line ("  Ham. Who's there?", "COUNTESS. In delivering my son from me") with
# the rest of the speech indented under it, or alone on its line ("HAMLET.") with the speech on the lines below,
# up to a blank line. Stage directions ("Enter \u2026", "Exit", "[Aside]") sit between and beside the speeches, and
# ACT/SCENE headings start a new scene. Sonnets are numbered poems of about 14 lines under a centred number.
_NAME_ANY = r"(?:[A-Z][A-Za-z'\u2019\-]{0,2}\. |[A-Z][A-Za-z'\u2019\-]* ){0,3}[A-Z][A-Za-z'\u2019\-]*"   # Ham, Lady M, K. Hen, MRS. PAGE
_NAME_CAPS = r"(?:[A-Z][A-Z'\u2019\-]+ ){0,3}[A-Z][A-Z'\u2019\-]+"             # COUNTESS, ANTIPHOLUS OF SYRACUSE
SPEECH_LINE = re.compile(r"^(?: {1,3}(%s)|(%s))\. +(\S.*)$" % (_NAME_ANY, _NAME_CAPS))
SPEECH_CAPS = re.compile(r"^ {0,4}(%s)\. +(\S.*)$" % _NAME_CAPS)       # NAME in capitals: a new speech even inside one
SPEECH_NAME = re.compile(r"^ {0,3}(%s)\.$" % _NAME_CAPS)
PLAY_HEADING = re.compile(r"^\s*(?:ACT|Act|SCENE|Scene)(?![a-z])")
PLAY_END = re.compile(r"^\s*(?:THE END|Dramatis Person|DRAMATIS PERSON|PERSONS REPRESENTED|\d{4}\s*$)")
STAGE_WORDS = re.compile(r"\b(?:Enter|Re-enter|Reenter|Exit|Exeunt|Manet|Manent|Flourish|Sennet|Alarums?|Trumpets|Hautboys)\b")
SIDE_NOTE = re.compile(r"(?<=\S) {4,}\S.*$|(?<=[.!?,;:\-]) +(?:Exit|Exeunt)\b.*$")   # a stage direction beside a line
STAGE_LINE = re.compile(r"^(?:Exit|Exeunt|Re-enter|Reenter|Manet|Manent)\b")
SONNET_NUMBER = re.compile(r"^ {8,}(?:\d{1,3}|[IVXLC]+)\.?\s*$")
SONNET_ASKS = ["Write me a sonnet.", "Write a sonnet.", "Write me a poem.", "Give me a sonnet."]
PLAY_SHARE = 0.1                                       # speeches must be this share of the lines for a file to be a play
PLAY_MAX = 50 * 1024 * 1024                  # bigger plain text is never read whole to look for a play


def _speech_text(lines):
    """A speech's lines -> its words: stage directions beside or inside it taken off, verse lines kept."""
    text = re.sub(r" *\[[^\]]{0,300}\] *", " ", "\n".join(lines))
    text = re.sub(r"\[.*|^[^\[\n]*\]\.?", "", text, flags=re.M)     # what's left of a [note] that ran on past the speech
    text = "\n".join(SIDE_NOTE.sub("", line).strip() for line in text.split("\n"))
    lines = (re.sub(r"[ \t]{2,}", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def play_scenes(text):
    """[[(speaker, speech), \u2026] for each scene] of a play in plain text, or of all the plays in a complete works."""
    text = re.sub(r"<<[^>]*>>", "\n", text.replace("\r\n", "\n").expandtabs())   # World Library's notices between the plays
    scenes, scene = [], []
    cur = None                                         # [speaker, lines, indent of its first line, name on its own line]
    in_play = False                                    # off in front matter and cast lists, on from the first ACT or SCENE
    in_note = False                                    # inside a [stage direction] that runs over several lines

    def end_speech():
        nonlocal cur
        if cur:
            words = _speech_text(cur[1])
            if words:
                scene.append((cur[0].upper(), words))
        cur = None

    def end_scene():
        end_speech()
        if scene:
            scenes.append(scene[:])
            scene.clear()

    for raw in text.split("\n"):
        line = raw.rstrip()
        words = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if in_note:
            in_note = "]" not in words
        elif not words:
            if cur and cur[3] and cur[1]:
                end_speech()
        elif PLAY_HEADING.match(line):
            end_scene()
            in_play = True
        elif PLAY_END.match(line):
            end_scene()
            in_play = False
        elif not in_play:
            continue
        elif words.startswith("["):
            in_note = "]" not in words                 # a stage direction between speeches or lines
        elif STAGE_LINE.match(words) or (indent >= 5 and STAGE_WORDS.search(words)):
            continue
        elif SPEECH_CAPS.match(line):
            m = SPEECH_CAPS.match(line)
            end_speech()
            cur = [m.group(1), [m.group(2)], indent, False]
        elif cur and not cur[3] and cur[2] == 0 and indent > 0:
            cur[1].append(line)                        # a speech that starts at the margin goes on indented
        elif SPEECH_LINE.match(line):
            m = SPEECH_LINE.match(line)
            end_speech()
            cur = [m.group(1) or m.group(2), [m.group(3)], indent, False]
        elif SPEECH_NAME.match(line):
            end_speech()
            cur = [SPEECH_NAME.match(line).group(1), [], indent, True]
        elif cur and (cur[3] or indent > cur[2]):
            cur[1].append(line)
        else:
            end_speech()                               # a place, a stage direction at the margin, a title
    end_scene()
    return scenes


def sonnets(text):
    """Every poem of 12 to 16 lines under a centred number (1, 2 \u2026 or I, II \u2026), as its lines."""
    found, cur = [], None
    for line in text.replace("\r\n", "\n").split("\n"):
        if SONNET_NUMBER.match(line):
            cur = []
            found.append(cur)
        elif cur is not None:
            if line.strip():
                cur.append(line.strip())
            elif cur:
                cur = None
    return ["\n".join(s) for s in found if 12 <= len(s) <= 16]


def play_conversations(text):
    """A play (or complete works) -> conversations with no names: every speech answers the one before it in the
    same scene, and every sonnet answers "Write me a sonnet." [] if the text isn't a play."""
    scenes = play_scenes(text)
    lines = sum(1 for line in text.split("\n") if line.strip())
    if sum(len(s) for s in scenes) < max(20, PLAY_SHARE * lines):
        return []
    out = ["User: %s\nAssistant: %s" % (SONNET_ASKS[i % len(SONNET_ASKS)], poem) for i, poem in enumerate(sonnets(text))]
    for scene in scenes:
        out.extend("User: %s\nAssistant: %s" % (a, b) for (who_a, a), (who_b, b) in zip(scene, scene[1:]) if who_a != who_b)
    return out


MERGE_MAX = 1024 ** 3        # the shuffle keeps an index of every record in the dashboard's memory
MERGE_CHUNK = 2000           # plain text is cut at paragraph breaks into pieces of about this many characters
REPLY_MIN = 200              # with a trigger, each block of plain text is one reply, joined to the next if shorter than this


def _text_blocks(src):
    """The blank-line-separated blocks of a plain text file, streamed."""
    para = []
    with open(src, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.replace("\r\n", "\n").lstrip("\ufeff").rstrip("\n")
            if line.strip():
                para.append(line)
            elif para:
                yield "\n".join(para)
                para = []
    if para:
        yield "\n".join(para)


def _file_pieces(src, training_data, info, chunk=MERGE_CHUNK, trigger=""):
    """Every piece of one file as the trainer reads it (speaker names already off), streaming where it can. With a
    trigger, every conversation starts with it, and plain text becomes conversations too: a play becomes its
    speeches answering each other (names off), anything else one reply per blank-line block (a story, a
    paragraph or two) to the trigger itself."""
    kind = info["format"] = _kind(src, training_data)
    if kind == "jsonl":
        pieces = _jsonl_pieces(src, training_data, info)
    elif kind in ("json", "chat"):
        if os.path.getsize(src) > 200 * 1024 * 1024:
            raise ValueError("%s: JSON lists and chat text over 200 MB can't be merged here: save it as JSONL instead." % os.path.basename(src))
        with open(src, "r", encoding="utf-8", errors="replace") as f:
            pieces, rep = training_data.normalize_pieces(f.read())
        info.update(skipped=rep["skipped"], names_removed=rep["names_removed"], names=rep["names"])
    elif trigger:
        plays = []
        if os.path.getsize(src) <= PLAY_MAX:
            with open(src, "r", encoding="utf-8", errors="replace") as f:
                plays = play_conversations(f.read().lstrip("\ufeff"))
        if plays:
            info["format"] = "play"
            pieces = plays
        else:
            yield from _wrap_blocks(_text_blocks(src), trigger)
            return
    else:
        buf, size = [], 0
        for block in _text_blocks(src):
            buf.append(block)
            size += len(block)
            if size >= chunk:
                yield "\n\n".join(buf)
                buf, size = [], 0
        if buf:
            yield "\n\n".join(buf)
        return
    for piece in pieces:
        yield training_data.add_trigger(piece, trigger) if trigger else piece


def _wrap_blocks(blocks, trigger):
    buf = []
    for block in blocks:
        buf.append(block)
        if sum(map(len, buf)) >= REPLY_MIN:
            yield "User: %s\nAssistant: %s" % (trigger, "\n\n".join(buf))
            buf = []
    if buf:
        yield "User: %s\nAssistant: %s" % (trigger, "\n\n".join(buf))


def merge_files(srcs, dst, triggers=None, seed=0):
    """Several training files -> one JSONL file of all their records in a random order, so the 5% the trainer holds
    back for validation (the end of the file) has some of every file. Each record reads back exactly as the trainer
    read it from its own file: speaker names taken off a script, plain text cut at paragraph breaks into pieces of
    about MERGE_CHUNK characters. triggers: {file name: words}; each conversation from that file starts its first
    message with those words, so the model learns that file's style only for messages that start with them. Plain
    text with a trigger becomes conversations (see _file_pieces), so its style also comes out only when asked."""
    training_data = _training_data()
    triggers = triggers or {}
    parts = dst + ".parts"
    os.makedirs(parts, exist_ok=True)
    index, files = [], []
    try:
        for n, src in enumerate(srcs):
            name = os.path.basename(src)
            trigger = str(triggers.get(name) or "").strip()
            info = {"file": name, "records": 0, "conversations": 0, "triggered": 0, "skipped": 0, "names_removed": 0, "names": []}
            with open(os.path.join(parts, "%d.jsonl" % n), "wb") as out:
                for piece in _file_pieces(src, training_data, info, trigger=trigger):
                    rec = training_data.piece_to_record(piece)
                    if "messages" in rec:
                        info["conversations"] += 1
                        info["triggered"] += bool(trigger and any(t["role"] == "user" for t in rec["messages"]))
                    line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
                    index.append((n, out.tell(), len(line)))
                    out.write(line)
                    info["records"] += 1
            if trigger:
                info["trigger"] = trigger
            files.append(info)
        if not index:
            raise ValueError("Those files have nothing to train on.")
        random.Random(seed).shuffle(index)
        handles = [open(os.path.join(parts, "%d.jsonl" % n), "rb") for n in range(len(srcs))]
        try:
            with open(dst + ".tmp", "wb") as out:
                for n, offset, length in index:
                    handles[n].seek(offset)
                    out.write(handles[n].read(length))
        finally:
            for h in handles:
                h.close()
        os.replace(dst + ".tmp", dst)
    finally:
        shutil.rmtree(parts, ignore_errors=True)
        if os.path.exists(dst + ".tmp"):
            os.remove(dst + ".tmp")
    return {"records": len(index), "files": files}


# -- conversations with more than two people ------------------------------------------------------------------
# A line of a script, a play or a chat export: "NAME: text", optionally after a WhatsApp-style timestamp
# ("12/03/2024, 21:15 - Name: text" or "[12/03/2024, 21:15:03] Name: text"). Lines without a name continue the
# previous speaker's turn.
TURN_RE = re.compile(r"^\s*(?:\[?[\d/.\-]+,? [\d:]+(?: ?[APap][Mm])?\]?\s*(?:-\s*)?)?([A-Za-z][\w .'’\-]{0,39}?)\s*:\s+(.*\S)\s*$")


def read_turns(path, limit_bytes=512 * 1024 * 1024):
    """[(speaker, text)], with one speaker's consecutive lines merged into one turn."""
    turns = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        read = 0
        for line in f:
            read += len(line)
            if read > limit_bytes:
                break
            m = TURN_RE.match(line)
            if m and len(m.group(1).split()) <= 4:
                who, text = m.group(1).strip(), m.group(2).strip()
                if turns and turns[-1][0] == who:
                    turns[-1] = (who, turns[-1][1] + " " + text)
                else:
                    turns.append((who, text))
            elif line.strip() and turns:
                turns[-1] = (turns[-1][0], turns[-1][1] + " " + line.strip())
    return turns


def speakers(path):
    counts = {}
    for who, _ in read_turns(path):
        counts[who] = counts.get(who, 0) + 1
    return sorted(({"name": k, "turns": v} for k, v in counts.items()), key=lambda x: -x["turns"])[:40]


def multiparty_to_pairs(src, dst, speaker, context=3, names=False):
    """Every turn by `speaker` becomes one example: the `context` turns before it as the User side, and the
    speaker's own words as the Assistant side. By default the other speakers' names are dropped from the User
    side, so the model never sees the "NAME: text" pattern and never learns to write a "NAME:" prefix in front
    of its own replies. Pass names=True to keep the "NAME: …" labels (only useful for a group chat where who
    said what matters); the Assistant side is always just the words, with no name."""
    turns = read_turns(src)
    examples = 0
    tmp = dst + ".tmp"
    with open(tmp, "w", encoding="utf-8") as out:
        for i, (who, text) in enumerate(turns):
            if who != speaker or i == 0:
                continue
            before = turns[max(0, i - context):i]
            if not before:
                continue
            ask = "\n".join(("%s: %s" % (w, t)) if names else t for w, t in before)
            out.write(json.dumps({"messages": [{"role": "user", "content": ask}, {"role": "assistant", "content": text}]}, ensure_ascii=False) + "\n")
            examples += 1
    os.replace(tmp, dst)
    return {"examples": examples, "turns": len(turns)}
