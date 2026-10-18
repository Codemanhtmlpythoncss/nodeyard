"""The quotes on the Desk page's quote widget: a built-in set, plus your own, editable from the page and kept on the
server (quotes.json in the dashboard's state folder) so they survive restarts and appear on every device.

The built-in quotes are verbatim and checked against their original sources (listed with each). A quote you add can
be marked as a paraphrase, and the widget then says so rather than presenting it as someone's exact words.

  GET  /api/quotes            {quotes: [{id, text, by, source, kind, builtin, edited}], rotate, interval}
  POST /api/quotes/save       {id?, text, by, source, kind: quote|paraphrase}   add, or edit (a built-in one is kept as your edit)
  POST /api/quotes/delete     {id}
  POST /api/quotes/restore    {own: false}   bring back the built-in quotes you deleted or edited (own: also remove yours)
  POST /api/quotes/settings   {rotate, interval}   interval in seconds; 0 = a different quote each day
"""
import json
import os
import re
import secrets
import threading

BUILTIN = [
    {"id": "torvalds-talk", "text": "Talk is cheap. Show me the code.", "by": "Linus Torvalds",
     "source": "linux-kernel mailing list, 25 August 2000 (lkml.org/lkml/2000/8/25/132)"},
    {"id": "torvalds-data", "text": "Bad programmers worry about the code. Good programmers worry about data structures and their relationships.",
     "by": "Linus Torvalds", "source": "git mailing list, \"Re: Licensing and the library version of git\", 27 July 2006"},
    {"id": "torvalds-hobby", "text": "I'm doing a (free) operating system (just a hobby, won't be big and professional like gnu) for 386(486) AT clones.",
     "by": "Linus Torvalds", "source": "comp.os.minix, \"What would you like to see most in minix?\", 25 August 1991"},
    {"id": "torvalds-backup", "text": "Only wimps use tape backup: _real_ men just upload their important stuff on ftp, and let the rest of the world mirror it ;)",
     "by": "Linus Torvalds", "source": "linux-kernel mailing list, 20 July 1996 (the Linux 2.0.8 announcement)"},
    {"id": "torvalds-indent", "text": "If you need more than 3 levels of indentation, you're screwed anyway, and should fix your program.",
     "by": "Linus Torvalds", "source": "Linux kernel coding style (Documentation/process/coding-style.rst)"},
    {"id": "davis-simple", "text": "Any genius can make it complicated. Like in physics, it takes a supra-genius to make it simple.",
     "by": "Terry A. Davis", "source": "TempleOS Charter (Doc/Charter.DD)"},
    {"id": "davis-c64", "text": "The vision is the same usage model and niche as the Commodore 64 -- a non-networked, simple machine where programming was the goal, not just a means to an end.",
     "by": "Terry A. Davis", "source": "TempleOS Charter (Doc/Charter.DD)"},
    {"id": "davis-kayak", "text": "Also, it's a kayak, not a Titanic.", "by": "Terry A. Davis", "source": "TempleOS Charter (Doc/Charter.DD), on keeping TempleOS small"},
    {"id": "davis-deps", "text": "Dependency on components and libraries creates a hell that is no longer blissful.",
     "by": "Terry A. Davis", "source": "TempleOS Charter (Doc/Charter.DD)"},
    {"id": "frost-through", "text": "He says the best way out is always through.", "by": "Robert Frost",
     "source": "\"A Servant to Servants\", North of Boston (1914)"},
    {"id": "franklin-done", "text": "Well done is better than well said.", "by": "Benjamin Franklin", "source": "Poor Richard's Almanack, 1737"},
    {"id": "vangogh-small", "text": "For the great doesn't happen through impulse alone, and is a succession of little things that are brought together.",
     "by": "Vincent van Gogh", "source": "letter to Theo van Gogh, The Hague, 22 October 1882 (letter 274; Van Gogh Letters translation)"},
]
for _q in BUILTIN:
    _q["kind"] = "quote"
INTERVALS = (0, 30, 60, 300, 900, 3600)   # 0: a different quote each day


class QuoteError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


class Quotes:
    def __init__(self, state_dir):
        self.path = os.path.join(state_dir or "/var/lib/nodeyard/dashboard", "quotes.json")
        self.lock = threading.Lock()
        self.data = {"own": [], "edits": {}, "hidden": [], "rotate": True, "interval": 0}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self.data.update({k: raw[k] for k in self.data if k in raw})
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            import sys
            sys.stderr.write("quotes: couldn't read %s (%s); using the built-in quotes\n" % (self.path, e))

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            with open(self.path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(self.data, f)
            os.replace(self.path + ".tmp", self.path)
        except OSError as e:
            raise QuoteError("Couldn't save the quotes: %s" % (e.strerror or e), 500)

    def listing(self):
        d = self.data
        out = []
        for q in BUILTIN:
            if q["id"] in d["hidden"]:
                continue
            e = d["edits"].get(q["id"])
            out.append(dict(q, **e, builtin=True, edited=True) if e else dict(q, builtin=True, edited=False))
        out += [dict(q, builtin=False, edited=False) for q in d["own"]]
        return {"quotes": out, "rotate": bool(d["rotate"]), "interval": int(d["interval"]), "intervals": INTERVALS}

    @staticmethod
    def _clean(body):
        text = re.sub(r"\s+", " ", str(body.get("text") or "")).strip()
        by = re.sub(r"\s+", " ", str(body.get("by") or "")).strip()
        source = re.sub(r"\s+", " ", str(body.get("source") or "")).strip()
        kind = body.get("kind") if body.get("kind") in ("quote", "paraphrase") else "quote"
        if not 2 <= len(text) <= 500:
            raise QuoteError("A quote is 2 to 500 characters.")
        if not by or len(by) > 100:
            raise QuoteError("Say who said it (up to 100 characters).")
        if len(source) > 200:
            raise QuoteError("Keep the source under 200 characters.")
        return {"text": text, "by": by, "source": source, "kind": kind}

    def save(self, body):
        q = self._clean(body)
        qid = str(body.get("id") or "")
        with self.lock:
            others = [x for x in self.listing()["quotes"] if x["id"] != qid]
            if any(norm(x["text"]) == norm(q["text"]) for x in others):
                raise QuoteError("That quote is already in the list.", 409)
            d = self.data
            if qid and any(b["id"] == qid for b in BUILTIN):
                d["edits"][qid] = q
            elif qid:
                own = next((x for x in d["own"] if x["id"] == qid), None)
                if own is None:
                    raise QuoteError("No such quote.", 404)
                own.update(q)
            else:
                if len(d["own"]) >= 500:
                    raise QuoteError("That's 500 of your own quotes already.")
                d["own"].append(dict(q, id="own-" + secrets.token_hex(4)))
            self._save()
        return self.listing()

    def delete(self, body):
        qid = str(body.get("id") or "")
        with self.lock:
            d = self.data
            if any(b["id"] == qid for b in BUILTIN):
                if qid not in d["hidden"]:
                    d["hidden"].append(qid)
                d["edits"].pop(qid, None)
            else:
                n = len(d["own"])
                d["own"] = [x for x in d["own"] if x["id"] != qid]
                if len(d["own"]) == n:
                    raise QuoteError("No such quote.", 404)
            if not self.listing()["quotes"]:
                raise QuoteError("Keep at least one quote (or restore the defaults).", 409)
            self._save()
        return self.listing()

    def restore(self, body):
        with self.lock:
            self.data["hidden"], self.data["edits"] = [], {}
            if body.get("own") is True:
                self.data["own"] = []
            else:   # (one of yours that now repeats a built-in quote goes)
                builtin = {norm(b["text"]) for b in BUILTIN}
                self.data["own"] = [x for x in self.data["own"] if norm(x["text"]) not in builtin]
            self._save()
        return self.listing()

    def settings(self, body):
        try:
            interval = int(body.get("interval", self.data["interval"]))
        except (TypeError, ValueError):
            raise QuoteError("Pick how often it changes.")
        if interval not in INTERVALS:
            raise QuoteError("Pick how often it changes.")
        with self.lock:
            self.data["rotate"] = body.get("rotate", self.data["rotate"]) is not False
            self.data["interval"] = interval
            self._save()
        return self.listing()


def register(ctx, args):
    q = Quotes(getattr(args, "state_dir", ""))
    ctx.quotes = q

    def wrap(fn):
        def route(h, arg):
            try:
                r = fn(arg)
            except QuoteError as e:
                return h._json({"ok": False, "error": str(e)}, e.code)
            return h._json(dict(r, ok=True))
        return route

    ctx.get_routes["/api/quotes"] = wrap(lambda q_: q.listing())
    ctx.post_routes.update({"/api/quotes/save": wrap(q.save), "/api/quotes/delete": wrap(q.delete),
                            "/api/quotes/restore": wrap(q.restore), "/api/quotes/settings": wrap(q.settings)})
