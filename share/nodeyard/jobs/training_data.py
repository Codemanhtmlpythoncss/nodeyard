"""Read plain text and common instruction/chat JSONL training records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator


ROLE_NAMES = {
    "user": "User",
    "human": "User",
    "customer": "User",
    "assistant": "Assistant",
    "gpt": "Assistant",
    "bot": "Assistant",
    "system": "System",
    "developer": "Developer",
}


def _record_text(record: object, path: Path, line_number: int) -> str:
    if isinstance(record, str):
        return record
    if not isinstance(record, dict):
        raise ValueError(f"JSONL record at {path}:{line_number} must be an object or string.")

    messages = record.get("messages", record.get("conversations"))
    if isinstance(messages, list):
        parts = []
        for message in messages:
            if isinstance(message, str):
                parts.append(message)
                continue
            if not isinstance(message, dict):
                continue
            role = next((message[key] for key in ("role", "from", "speaker")
                         if isinstance(message.get(key), str)), "")
            content = next((message[key] for key in ("content", "value", "text")
                            if isinstance(message.get(key), str)), "")
            if content.strip():
                role = ROLE_NAMES.get(role.lower(), role.title() if role else "Message")
                parts.append(f"{role}: {content.strip()}")
        if parts:
            return "\n".join(parts)

    instruction = record.get("instruction")
    output = next((record[key] for key in ("output", "response", "answer", "completion")
                   if isinstance(record.get(key), str)), None)
    if isinstance(instruction, str) and output is not None:
        prompt = instruction.strip()
        extra_input = record.get("input")
        if isinstance(extra_input, str) and extra_input.strip():
            prompt += "\n" + extra_input.strip()
        return f"User: {prompt}\nAssistant: {output.strip()}"

    prompt = record.get("prompt")
    if isinstance(prompt, str) and output is not None:
        return f"User: {prompt.strip()}\nAssistant: {output.strip()}"

    # Common chat JSONL stores one turn per line as {"speaker": ..., "text": ...}.
    role = next((record[key] for key in ("role", "from", "speaker")
                 if isinstance(record.get(key), str)), "")
    if role:
        content = next((record[key] for key in ("content", "value", "text")
                        if isinstance(record.get(key), str)), "")
        if content.strip():
            normalized_role = ROLE_NAMES.get(role.lower())
            # Shakespeare corpora often store one line as {speaker, text}. Keep
            # the character name, but mark the line as assistant output so chat
            # training never learns JSON keys such as "speaker" and "text".
            if normalized_role:
                return f"{normalized_role}: {content.strip()}"
            return f"Assistant: {role.title()}: {content.strip()}"

    for key in ("text", "content"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    raise ValueError(
        f"Unsupported JSONL record at {path}:{line_number}; expected text, messages, "
        "instruction/output, or prompt/completion fields."
    )


def iter_training_text(path: Path) -> Iterator[str]:
    """Yield text chunks, formatting JSONL records as readable role-prefixed text."""
    path = path.expanduser()
    if path.suffix.lower() != ".jsonl":
        with path.open("r", encoding="utf-8", errors="replace", newline=None) as source:
            yield from source
        return

    with path.open("r", encoding="utf-8-sig", errors="strict", newline=None) as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"Invalid JSONL at {path}:{line_number}: {error.msg}. "
                    "Each non-empty line must contain one JSON value."
                ) from error
            yield _record_text(record, path, line_number) + "\n\n"
