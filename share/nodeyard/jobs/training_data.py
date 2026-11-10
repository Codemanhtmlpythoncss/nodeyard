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
      {"text": "..."}
  * A JSON file holding a list of the objects above.

Every chat-shaped record becomes:

    User: <what the person said>
    Assistant: <the reply>

with a blank line after it, which is the layout the trainer's --assistant-only
option and the chat window expect. This module only uses the standard library so it
can be copied to any node next to the trainer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROLE_NAMES = {
    "user": "User", "human": "User", "prompter": "User", "question": "User",
    "assistant": "Assistant", "gpt": "Assistant", "bot": "Assistant", "model": "Assistant", "answer": "Assistant",
    "system": "System",
}
MAX_REPORTED_PROBLEMS = 20


def _clean(value) -> str:
    return str(value if value is not None else "").replace("\r\n", "\n").strip()


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
            return _clean(record[key]) or None
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


def normalize(text: str) -> tuple[str, dict]:
    """Raw file contents -> (training text, report). The report says what was found and what was skipped."""
    text = text.replace("\r\n", "\n").lstrip("﻿")
    kind = detect_format(text)
    problems: list[str] = []
    records = 0
    skipped = 0
    if kind in ("json", "jsonl"):
        pieces = []
        if kind == "json":
            try:
                items = json.loads(text)
            except ValueError as error:
                return "", {"format": kind, "records": 0, "skipped": 0, "chat": False,
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
            records += 1
            pieces.append(converted)
        out = "\n\n".join(pieces) + ("\n" if pieces else "")
    else:
        out = text.strip() + "\n"
        records = sum(1 for block in out.split("\n\n") if block.strip())
    chat = "\nAssistant:" in "\n" + out and "\nUser:" in "\n" + out
    return out, {"format": kind, "records": records, "skipped": skipped, "chat": chat, "problems": problems}


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
