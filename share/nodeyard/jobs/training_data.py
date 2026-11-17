"""Reads training data and turns it into the plain text the model learns from.

Accepted (the format is detected from the content, not the file name, because the
jobs runner copies every file as data.txt):

  * Plain text / Markdown: books, notes, articles. Used as it is.
  * Chat transcripts in text: lines that start with "User:" and "Assistant:"
    (blank line between conversations).
  * JSONL, one JSON object per line, any of:
      {"messages": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]}
      {"conversations": [{"from": "human", "value": "..."}, {"from": "gpt", "value": "..."}]}
      {"instruction": "...", "input": "...", "output": "..."}       (input optional)
      {"instruction": "...", "context": "...", "response": "..."}   (Dolly; context optional)
      {"prompt": "...", "completion": "..."}   or  {"question": "...", "answer": "..."}
      {"text": "..."}   (ChatML in it -- <|im_start|>user ... <|im_end|> -- is turned into User:/Assistant:)
  * A JSON file holding a list of the objects above.

Every chat-shaped record becomes:

    User: <what the person said>
    Assistant: <the reply>

with a blank line after it, which is the layout the trainer's --assistant-only
option and the chat window expect. This module only uses the standard library so it
can be copied to any node next to the trainer.

load_split() is what the trainer uses. It holds back the last 5% of *every* file for
validation (not just the end of the last file, which made the validation loss measure
one file only), and it leaves out conversations that only repeat replies seen many
times already: a generated set where the same few hundred answers recur hundreds of
times teaches the model to give those canned answers to everything.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

ROLE_NAMES = {
    "user": "User", "human": "User", "prompter": "User", "question": "User",
    "assistant": "Assistant", "gpt": "Assistant", "bot": "Assistant", "model": "Assistant", "answer": "Assistant",
    "system": "System",
}
MAX_REPORTED_PROBLEMS = 20
HOLD_OUT = 0.05            # fraction of each file kept back to measure validation loss
MAX_REPEATS = 3            # a reply may recur this often; a conversation adding only over-used replies is left out
MIN_COUNTED_REPLY = 24     # shorter replies ("Yes.", "Positive") are labels, not canned text: never capped
CHATML_TURN = re.compile(r"<\|im_start\|>[ \t]*([A-Za-z_]+)[ \t]*\n?(.*?)(?=<\|im_end\|>|<\|im_start\|>|\Z)", re.S)
ROLE_LINE = re.compile(r"^(User|Assistant|System):", re.M)
# A speaker's name in front of a line of a play or a chat export: "JULIET: O Romeo", "Nurse: Madam!", "First Citizen: Hear me".
SPEAKER = re.compile(r"^([A-Z][A-Za-z.'’\-]*(?: [A-Za-z.'’\-]+){0,3}):[ \t]+(?=\S)")
NAMED_SHARE = 0.5          # when this share of the replies starts with a name, the file is a script: the names come off


def _clean(value) -> str:
    return str(value if value is not None else "").replace("\r\n", "\n").strip()


def chatml_to_text(text: str) -> str | None:
    """'<|im_start|>user\nHi<|im_end|><|im_start|>assistant\nHello<|im_end|>' -> 'User: Hi\nAssistant: Hello'.
    None when the text isn't ChatML, so the caller keeps it as it is."""
    if "<|im_start|>" not in text:
        return None
    lines = []
    for role, content in CHATML_TURN.findall(text):
        name = ROLE_NAMES.get(role.lower())
        content = _clean(content)
        if name and content:
            lines.append(f"{name}: {content}")
    return "\n".join(lines) or None


def record_to_text(record) -> str | None:
    """One JSON record -> training text, or None when the shape isn't one we know."""
    if isinstance(record, str):
        return _clean(record) or None
    if not isinstance(record, dict):
        return None
    turns = record.get("messages") or record.get("conversations") or record.get("conversation")
    if isinstance(turns, list):
        lines = []
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            role = str(turn.get("role") or turn.get("from") or turn.get("speaker") or "").lower()
            content = _clean(turn.get("content") if "content" in turn else turn.get("value", turn.get("text")))
            name = ROLE_NAMES.get(role)
            if name and content:
                lines.append(f"{name}: {content}")
        return "\n".join(lines) or None
    for ask, reply in (("instruction", "output"), ("instruction", "response"), ("prompt", "completion"),
                       ("prompt", "response"), ("question", "answer"), ("input", "output")):
        if ask in record and reply in record:
            question = _clean(record[ask])
            extra = _clean(record.get("input") or record.get("context")) if ask == "instruction" else ""
            if extra:
                question = f"{question}\n{extra}"
            answer = _clean(record[reply])
            if question and answer:
                return f"User: {question}\nAssistant: {answer}"
            return None
    for key in ("text", "content", "body"):
        if isinstance(record.get(key), str):
            return chatml_to_text(record[key]) or _clean(record[key]) or None
    return None


def detect_format(text: str) -> str:
    head = text.lstrip()[:4000]
    # A JSON list starts [ then {, ", [ or ]; a book can start with "[Illustration]" and is plain text.
    if re.match(r"\[\s*[\[{\"\]]", head):
        return "json"
    first = next((line.strip() for line in head.splitlines() if line.strip()), "")
    if first.startswith("{") and first.endswith("}"):
        return "jsonl"
    lines = [line.lstrip() for line in head.splitlines()]
    if any(line.startswith("User:") for line in lines) and any(line.startswith("Assistant:") for line in lines):
        return "chat"
    return "text"


def count_named_replies(piece: str) -> tuple[int, int]:
    """(replies, replies that start with a speaker's name) in one conversation."""
    replies_seen = named = 0
    for mark in ROLE_LINE.finditer(piece):
        if mark.group(1) == "Assistant":
            replies_seen += 1
            end = piece.find("\n", mark.end())
            named += bool(SPEAKER.match(piece[mark.end():end if end >= 0 else len(piece)].lstrip()))
    return replies_seen, named


def is_script(replies_seen: int, named: int) -> bool:
    return bool(replies_seen) and named >= NAMED_SHARE * replies_seen


def drop_speaker_names(piece: str, names: Counter) -> str:
    """Take "NAME: " off the start of every line of one conversation, counting the names in `names`."""
    lines = []
    for line in piece.split("\n"):
        role = ROLE_LINE.match(line)
        head, body = (role.group(0) + " ", line[role.end():].lstrip()) if role else ("", line)
        m = SPEAKER.match(body)
        if m:
            names[m.group(1)] += 1
            body = body[m.end():]
        lines.append(head + body)
    return "\n".join(lines)


def strip_speaker_names(pieces: list[str]) -> tuple[list[str], int, list[str]]:
    """A chat set made from a play or a group chat often keeps the speaker in front of every line
    ("Assistant: JULIET: O Romeo"). Trained on that, the model starts every reply with a name. When most replies
    start with one, every "NAME: " at the start of a line comes off, on both sides; a set where only a few replies
    start with a label ("Note: …") is left alone. Returns (pieces, names taken off, the most common names)."""
    counts = [count_named_replies(p) for p in pieces]
    if not is_script(sum(c[0] for c in counts), sum(c[1] for c in counts)):
        return pieces, 0, []
    names: Counter = Counter()
    out = [drop_speaker_names(p, names) for p in pieces]
    return out, sum(names.values()), [n for n, _ in names.most_common(5)]


def add_trigger(piece: str, trigger: str) -> str:
    """Put `trigger` in front of the first thing the person says ("User: Speak like Shakespeare. Hello"), so a
    style learned from this conversation comes out only when a message starts with the same words."""
    m = re.search(r"^User:[ \t]*", piece, re.M)
    return piece[:m.end()] + trigger + " " + piece[m.end():] if m and trigger else piece


def piece_to_record(piece: str) -> dict:
    """One training piece -> one JSONL record that reads back the same: {"messages": [...]} for a conversation,
    {"text": ...} for anything else."""
    marks = list(ROLE_LINE.finditer(piece))
    if marks and not piece[:marks[0].start()].strip():
        turns = []
        for mark, nxt in zip(marks, marks[1:] + [None]):
            content = piece[mark.end():nxt.start() if nxt else len(piece)].strip()
            if content:
                turns.append({"role": mark.group(1).lower(), "content": content})
        if turns:
            return {"messages": turns}
    return {"text": piece}


def normalize_pieces(text: str) -> tuple[list[str], dict]:
    """Raw file contents -> (training pieces, report). A piece is one record or conversation; plain text is one
    piece. The report says what was found and what was skipped."""
    text = text.replace("\r\n", "\n").lstrip("\ufeff")
    kind = detect_format(text)
    problems: list[str] = []
    skipped = 0
    if kind in ("json", "jsonl"):
        pieces = []
        if kind == "json":
            try:
                items = json.loads(text)
            except ValueError as error:
                return [], {"format": kind, "records": 0, "skipped": 0, "chat": False,
                            "problems": [f"The file starts with [ but isn't valid JSON: {error}"]}
            numbered = list(enumerate(items if isinstance(items, list) else [items], 1))
        else:
            numbered = []
            for number, line in enumerate(text.split("\n"), 1):
                if not line.strip():
                    continue
                try:
                    numbered.append((number, json.loads(line)))
                except ValueError as error:
                    skipped += 1
                    if len(problems) < MAX_REPORTED_PROBLEMS:
                        problems.append(f"Line {number}: not valid JSON ({error.msg}).")
        for number, item in numbered:
            converted = record_to_text(item)
            if converted is None:
                skipped += 1
                if len(problems) < MAX_REPORTED_PROBLEMS:
                    keys = ", ".join(sorted(item)[:6]) if isinstance(item, dict) else type(item).__name__
                    problems.append(f"{'Line' if kind == 'jsonl' else 'Item'} {number}: no field I can train on (found: {keys}).")
                continue
            pieces.append(converted)
        records = len(pieces)
    elif kind == "chat":
        pieces = [block.strip() for block in text.split("\n\n") if block.strip()]
        records = len(pieces)
    else:
        pieces = [text.strip()] if text.strip() else []
        records = sum(1 for block in text.split("\n\n") if block.strip())
    chat = any("\nAssistant:" in "\n" + p for p in pieces) and any("\nUser:" in "\n" + p for p in pieces)
    names_removed, names = 0, []
    if chat:
        pieces, names_removed, names = strip_speaker_names(pieces)
    return pieces, {"format": kind, "records": records, "skipped": skipped, "chat": chat, "problems": problems,
                    "names_removed": names_removed, "names": names}


def normalize(text: str) -> tuple[str, dict]:
    """Raw file contents -> (training text, report). The report says what was found and what was skipped."""
    pieces, report = normalize_pieces(text)
    return ("\n\n".join(pieces) + "\n") if pieces else ("" if report["format"] in ("json", "jsonl") else "\n"), report


def replies(piece: str) -> list[str]:
    """The Assistant turns of one conversation, each with its continuation lines."""
    out = []
    marks = list(ROLE_LINE.finditer(piece))
    for mark, nxt in zip(marks, marks[1:] + [None]):
        if mark.group(1) == "Assistant":
            out.append(piece[mark.end():nxt.start() if nxt else len(piece)].strip())
    return out


def exchanges(piece: str) -> list[str]:
    """One conversation -> its exchanges, each running up to and including an Assistant turn (a leading System
    turn stays with the first). Text after the last reply is a final piece of its own."""
    out, start = [], 0
    marks = list(ROLE_LINE.finditer(piece))
    for mark, nxt in zip(marks, marks[1:] + [None]):
        if mark.group(1) == "Assistant":
            end = nxt.start() if nxt else len(piece)
            out.append(piece[start:end].strip())
            start = end
    if piece[start:].strip():
        out.append(piece[start:].strip())
    return out


def thin_repeats(pieces, seen: set, counts: Counter, max_repeats: int = MAX_REPEATS) -> tuple[list[str], int]:
    """Leave out what only repeats: a conversation identical to an earlier one, and inside conversations each
    exchange (question + reply) that has already appeared max_repeats times, or whose reply (of at least
    MIN_COUNTED_REPLY characters) has, whatever the question. Short replies to different questions ("Positive",
    "4.") are labels and stay. A conversation left with no reply is dropped. `seen` and `counts` carry over
    between files; plain text passes through untouched. Returns (kept pieces, exchanges left out)."""
    kept, dropped = [], 0
    for piece in pieces:
        if "Assistant:" not in piece or not ROLE_LINE.search(piece):
            kept.append(piece)
            continue
        if piece in seen:
            dropped += max(1, len(replies(piece)))
            continue
        seen.add(piece)
        if not max_repeats:
            kept.append(piece)
            continue
        keep = []
        for part in exchanges(piece):
            said = replies(part)
            if not said:
                keep.append(part)
                continue
            reply = said[-1]
            if counts[("x", part)] >= max_repeats or (len(reply) >= MIN_COUNTED_REPLY and counts[("r", reply)] >= max_repeats):
                dropped += 1
                continue
            counts[("x", part)] += 1
            counts[("r", reply)] += 1
            keep.append(part)
        if any(replies(part) for part in keep):
            kept.append("\n".join(keep))
    return kept, dropped


def hold_out(pieces, fraction: float = HOLD_OUT) -> tuple[str, str]:
    """One file's pieces -> (training text, validation text): its last `fraction`, by characters. Many records:
    whole records from the end. One long text (a book): cut near the end at a paragraph, else a line, else a word
    break."""
    text = "\n\n".join(pieces)
    if fraction <= 0 or not text:
        return text, ""
    want = len(text) * fraction
    if len(pieces) >= 20:
        i, taken = len(pieces), 0
        while i > 1 and taken < want:
            i -= 1
            taken += len(pieces[i]) + 2
        return "\n\n".join(pieces[:i]), "\n\n".join(pieces[i:])
    cut = int(len(text) - want)
    floor = int(len(text) * (1 - 3 * fraction))
    for sep in ("\n\n", "\n", " "):
        at = text.rfind(sep, floor, cut + 1)
        if at > 0:
            return text[:at].strip(), text[at:].strip()
    return text[:cut], text[cut:]


def load_split(paths, fraction: float = HOLD_OUT, max_repeats: int = MAX_REPEATS):
    """Several files -> (training text, [(file name, validation text)], reports). The last `fraction` of every
    file is held out, so the validation loss measures all of them; repeated conversations are thinned across
    all files (see thin_repeats). Each report gains "repeats_dropped"."""
    train_parts, valid_parts, reports = [], [], []
    seen: set = set()
    counts: Counter = Counter()
    for path in paths:
        path = Path(path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Training data does not exist: {path}")
        pieces, report = normalize_pieces(path.read_text(encoding="utf-8", errors="replace"))
        pieces, report["repeats_dropped"] = thin_repeats(pieces, seen, counts, max_repeats)
        report["file"] = path.name
        reports.append(report)
        train, valid = hold_out(pieces, fraction)
        if train.strip():
            train_parts.append(train.strip())
        valid_parts.append((path.name, valid.strip()))
    return "\n\n".join(train_parts) + "\n\n", valid_parts, reports


def load_files(paths) -> tuple[str, list[dict]]:
    """Several files -> one training text (blank line between files) and a report per file."""
    texts, reports = [], []
    for path in paths:
        path = Path(path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Training data does not exist: {path}")
        converted, report = normalize(path.read_text(encoding="utf-8", errors="replace"))
        report["file"] = path.name
        reports.append(report)
        if converted.strip():
            texts.append(converted.strip())
    return "\n\n".join(texts) + "\n", reports
