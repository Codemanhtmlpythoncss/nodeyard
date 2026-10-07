"""Context compression: keeping a long conversation inside the model's context window.

Two steps, cheapest first:
  1. prune: old tool results (file contents, command output) are cut down to a few lines. No model call; usually frees most of it.
  2. summarize: everything before the last few turns is replaced by a summary the model writes (done in pieces
     when the history is itself too big for the window).
"""
import json

from . import util

KEEP_HEAD = 220
KEEP_TAIL = 120

SUMMARY_SYSTEM = ("You write precise summaries of a conversation between a user and a coding assistant, so the work can continue "
                  "without the original messages. Keep exact names, paths, commands, numbers, error messages and decisions. Never invent anything.")

SUMMARY_REQUEST = """Summarize the conversation so far for the assistant to continue from. Use these sections, leaving out empty ones:

## Request
What the user asked for, and any preferences or constraints they gave.
## Done so far
What was accomplished, in order, with the results that matter.
## Files
Paths read or changed, and what was learned or changed in each.
## Commands and findings
Commands run and the facts they showed (versions, errors, outputs that matter).
## Decisions
Choices made and why.
## Open problems and next steps
What is unfinished, failing or planned next.

Be concise (under {words} words) but keep every detail needed to carry on.{extra}"""


def message_tokens(m):
    n = 6 + util.est_tokens(m.get("content") or "")
    for c in m.get("tool_calls") or []:
        n += 8 + util.est_tokens(c.get("function", {}).get("name", "")) + util.est_tokens(c.get("function", {}).get("arguments", ""))
    return n


def estimate(messages):
    return sum(message_tokens(m) for m in messages)


def cut_index(messages, keep_turns):
    """Index where the last KEEP_TURNS user turns begin (0 when there are fewer)."""
    seen = 0
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") == "user" and not m.get("_synthetic"):
            seen += 1
            if seen >= keep_turns:
                return i
    return 0


def prune(messages, keep_turns, max_chars):
    """Shorten old tool results. Returns (new messages, characters saved)."""
    cut = cut_index(messages, keep_turns)
    out, saved = [], 0
    for i, m in enumerate(messages):
        if i < cut and m.get("role") == "tool" and not m.get("_pruned"):
            text = m.get("content") or ""
            if len(text) > max_chars:
                new = text[:KEEP_HEAD] + "\n... [older output trimmed: %d characters; run the tool again if you need it] ...\n" % (len(text) - KEEP_HEAD - KEEP_TAIL) + text[-KEEP_TAIL:]
                m = dict(m, content=new, _pruned=True)
                saved += len(text) - len(new)
        elif i < cut and m.get("role") == "assistant" and m.get("tool_calls"):
            # big arguments (a whole file written with Write) are the same kind of dead weight
            calls = []
            changed = False
            for c in m["tool_calls"]:
                a = c.get("function", {}).get("arguments", "")
                if len(a) > max_chars * 2:
                    try:
                        obj = json.loads(a)
                        for k, v in list(obj.items()):
                            if isinstance(v, str) and len(v) > max_chars:
                                obj[k] = v[:KEEP_HEAD] + "...[trimmed %d characters]" % (len(v) - KEEP_HEAD)
                        c = dict(c, function=dict(c["function"], arguments=json.dumps(obj)))
                        saved += len(a) - len(c["function"]["arguments"])
                        changed = True
                    except ValueError:
                        pass
                calls.append(c)
            if changed:
                m = dict(m, tool_calls=calls)
        out.append(m)
    return out, saved


def render_transcript(messages, per_item=1200):
    """The conversation as plain text for the summarizer (long tool output shortened)."""
    lines = []
    for m in messages:
        role = m.get("role")
        text = m.get("content") or ""
        if role == "user":
            if m.get("_synthetic") == "summary":
                lines.append("[Earlier summary]\n" + text)
            else:
                lines.append("USER: " + text)
        elif role == "assistant":
            if text.strip():
                lines.append("ASSISTANT: " + util.truncate_middle(text, per_item))
            for c in m.get("tool_calls") or []:
                lines.append("ASSISTANT called %s(%s)" % (c["function"]["name"], util.truncate_middle(c["function"].get("arguments", ""), 400)))
        elif role == "tool":
            lines.append("TOOL RESULT: " + util.truncate_middle(text, per_item))
    return "\n".join(lines)


def rendered_tokens(m, per_item):
    """What a message costs in the summarizer's prompt (long tool output is shortened there)."""
    text = m.get("content") or ""
    n = 8 + util.est_tokens(text if len(text) <= per_item else text[:per_item])
    for c in m.get("tool_calls") or []:
        n += 8 + min(util.est_tokens(c.get("function", {}).get("arguments", "")), 120)
    return n


def chunk_messages(messages, budget_tokens, per_item=1500):
    """Split messages into groups that each fit the budget (a group never splits a tool call from its result)."""
    groups, cur, size = [], [], 0
    for m in messages:
        t = rendered_tokens(m, per_item)
        if cur and size + t > budget_tokens and m.get("role") != "tool":
            groups.append(cur)
            cur, size = [], 0
        cur.append(m)
        size += t
    if cur:
        groups.append(cur)
    return groups


def summarize(client, messages, context_window, extra="", on_progress=None, stop=None, temperature=0.2):
    """Ask the model for a summary of MESSAGES. Long histories are summarized piece by piece."""
    window = context_window or 16384
    budget = max(1500, int(window * 0.5))
    words = 450 if window >= 12000 else 250
    groups = chunk_messages(messages, budget, 1500)
    if len(groups) > 1:
        groups = chunk_messages(messages, budget, 900)
    summary = ""
    for gi, group in enumerate(groups, 1):
        if on_progress:
            on_progress("Summarizing earlier messages (%d/%d)" % (gi, len(groups)))
        body = render_transcript(group, per_item=900 if len(groups) > 1 else 1500)
        prompt = ""
        if summary:
            prompt += "Summary so far:\n" + summary + "\n\nMore of the conversation follows. Update the summary to include it.\n\n"
        prompt += "<conversation>\n" + body + "\n</conversation>\n\n"
        prompt += SUMMARY_REQUEST.format(words=words, extra=("\n\nPay special attention to: " + extra) if extra else "")
        comp = client.chat([{"role": "system", "content": SUMMARY_SYSTEM}, {"role": "user", "content": prompt}], tools=None,
                           max_tokens=min(1500, max(300, window // 4)), temperature=temperature, stop=stop)
        summary = (comp.content or "").strip() or summary
    return summary


def summary_message(summary):
    return {"role": "user", "_synthetic": "summary",
            "content": "[This is a summary of the earlier part of our conversation, which was compressed to save space.]\n\n" + summary}


def ack_message():
    return {"role": "assistant", "_synthetic": "ack", "content": "Understood. I'll continue from that summary."}


def compact(client, messages, context_window, keep_turns=3, extra="", on_progress=None, stop=None):
    """(new message list, summary text, tokens before, tokens after). Raises ValueError when there is nothing to compress."""
    before = estimate(messages)
    cut = cut_index(messages, keep_turns)
    if cut <= 0:  # too few turns to keep several: compress everything but the last turn
        cut = cut_index(messages, 1)
    head, tail = messages[:cut], messages[cut:]
    if len(head) < 2:
        raise ValueError("There isn't enough earlier conversation to compress yet.")
    summary = summarize(client, head, context_window, extra, on_progress, stop)
    if not summary:
        raise ValueError("The model returned an empty summary.")
    new = [summary_message(summary), ack_message()] + tail
    return new, summary, before, estimate(new)
