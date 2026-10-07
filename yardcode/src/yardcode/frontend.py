"""What the agent talks to. The terminal screen, the dashboard's JSON stream and the tests each implement this.

Every method has a harmless default, so a frontend only overrides what it cares about.
"""
from . import ui


class Frontend:
    interactive = False          # can it ask the user things (permissions, questions, plan approval)?
    style = ui.Style(False)

    # ---- the model's turn ----
    def begin_turn(self, user_text):
        pass

    def on_text(self, delta):
        pass

    def on_thinking(self, delta):
        pass

    def end_text(self):
        """The assistant stopped writing text (a tool call or the end of the answer follows)."""

    def waiting(self, label, tokens=0):
        """Waiting for the model (first token not here yet)."""

    def end_turn(self, final_text):
        pass

    # ---- tools ----
    def tool_use(self, call_id, name, summary, args, depth=0):
        pass

    def tool_progress(self, call_id, line):
        pass

    def tool_result(self, call_id, name, result, depth=0):
        pass

    def request_permission(self, tool, args, decision, summary):
        """Return ("allow" | "deny", scope, feedback). scope: once | session | project | user."""
        return "deny", "once", "no way to ask"

    def todos(self, items):
        pass

    def ask_user(self, questions):
        return None

    def approve_plan(self, plan):
        return None              # "auto" | "ask" | "no"

    # ---- housekeeping ----
    def usage(self, comp, session, context):
        pass

    def info(self, text):
        pass

    def warn(self, text):
        pass

    def error(self, text):
        pass

    def compacting(self, label):
        pass

    def compacted(self, before, after, summary):
        pass

    def mode_changed(self, mode):
        pass


class Recorder(Frontend):
    """Collects everything (used by tests and by sub-agents)."""

    def __init__(self, interactive=False, answers=None):
        self.events = []
        self.interactive = interactive
        self.answers = list(answers or [])
        self.text = ""

    def on_text(self, delta):
        self.text += delta
        self.events.append(("text", delta))

    def tool_use(self, call_id, name, summary, args, depth=0):
        self.events.append(("tool_use", name, summary))

    def tool_result(self, call_id, name, result, depth=0):
        self.events.append(("tool_result", name, result.text, result.error))

    def request_permission(self, tool, args, decision, summary):
        self.events.append(("permission", tool.name, decision.reason))
        return self.answers.pop(0) if self.answers else ("deny", "once", "")

    def info(self, text):
        self.events.append(("info", text))

    def warn(self, text):
        self.events.append(("warn", text))

    def error(self, text):
        self.events.append(("error", text))

    def compacted(self, before, after, summary):
        self.events.append(("compacted", before, after))
