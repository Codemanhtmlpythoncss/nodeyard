"""Ready-made training data for Train AI, and turning any data file into exactly what the model reads.

The catalog is fixed here (no URL comes from the page): nodeyard's own starter chat set, generated on this machine,
and three downloads with licences that allow training: TinyStories, Dolly 15k and public-domain novels from Project
Gutenberg. Downloads stream to disk in pieces, so a big file never sits in the dashboard's memory.
"""
import codecs
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

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
    start = re.search(r"^\*\*\* ?START OF.*$", text, re.M)
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


def convert_file(src, dst):
    """Write exactly what the trainer will read from `src` into `dst`, streaming JSONL so size doesn't matter."""
    sys.path.insert(0, JOBS_DIR)
    try:
        import training_data
    finally:
        sys.path.remove(JOBS_DIR)
    with open(src, "r", encoding="utf-8", errors="replace") as f:
        head = f.read(4000)
    kind = training_data.detect_format(head.replace("\r\n", "\n").lstrip("﻿"))
    records = skipped = 0
    tmp = dst + ".tmp"
    with open(src, "r", encoding="utf-8", errors="replace") as f, open(tmp, "w", encoding="utf-8") as out:
        if kind == "jsonl":
            first = True
            for line in f:
                if not line.strip():
                    continue
                try:
                    text = training_data.record_to_text(json.loads(line))
                except ValueError:
                    text = None
                if text is None:
                    skipped += 1
                    continue
                out.write(("" if first else "\n\n") + text)
                first = False
                records += 1
            out.write("\n")
        elif kind == "json":
            if os.path.getsize(src) > 200 * 1024 * 1024:
                raise ValueError("JSON lists over 200 MB can't be converted here: save it as JSONL (one record per line) instead.")
            text, rep = training_data.normalize(f.read())
            out.write(text)
            records, skipped = rep["records"], rep["skipped"]
        else:
            for line in f:
                out.write(line.replace("\r\n", "\n").lstrip("﻿"))
    os.replace(tmp, dst)
    return {"format": kind, "records": records, "skipped": skipped}


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


def multiparty_to_pairs(src, dst, speaker, context=3, names=True):
    """Every turn by `speaker` becomes one example: the `context` turns before it as the User side (each with its
    speaker's name, so the model knows who said what), and the speaker's own words as the Assistant side."""
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
