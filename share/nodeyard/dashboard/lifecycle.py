"""Model lifecycle: give back idle memory and refresh the split model while it is unused.

Automatic unloading is off by default; hourly split-model refresh is on by default. Settings live in the dashboard's
prefs.json (one place for the page, the control API and the Mac app). When each feature is on:

  - The split model is unloaded (every server it runs on stops; the files stay on disk) once it has been idle for
    the chosen time. "Idle" comes from llama.cpp's own /slots endpoint, so requests that reach the model directly
    (yardcode, the dashboard's skills, other programs) count as use, not only chats through this dashboard. If the
    dashboard can't read /slots it doesn't know whether the model is busy, so it never unloads it.
  - Ollama models get the chosen time as their keep-alive after every request that goes through Nodeyard and when
    they are loaded from the page. Ollama itself counts down and unloads them; any request resets its timer.
  - The split model is fully unloaded and loaded again once an hour, but only when its own /slots endpoint confirms
    that no request is active. If busy, the refresh waits until the next safe moment.

Whether on or off: a model that is answering, or has queued work, is never unloaded; a model unloaded by the idle
policy is never loaded again by that policy; and an Ollama model loaded from the page stays loaded (keep-alive -1)
when automatic unloading is off, even after chats through Ollama's OpenAI endpoint (which on its own resets the
timer to Ollama's 5-minute default and made models reload mid-task).
"""
import collections
import threading
import time

PRESETS = (300, 600, 900, 1800, 2700, 3600, 7200)
MIN_IDLE, MAX_IDLE = 60, 7 * 24 * 3600
RELOAD_SECONDS = 3600
DEFAULTS = {"enabled": False, "idle_seconds": 1800, "auto_reload": True}
PREF_KEY = "model_lifecycle"
POLL_SECONDS = 15
SLOTS_STALE = 3 * POLL_SECONDS       # a /slots answer older than this doesn't prove the model is idle
UNLOADED_SETTLE = 60                 # the cluster view may still say "loaded" this long after an unload
RETRY_BASE, RETRY_MAX = 300, 3600    # a failed unload is retried after 5, 10, 20... minutes, at most hourly
REPAIR_COOLDOWN = 600                # a model found broken is reloaded by itself at most every 10 minutes (a flapping node can't loop it)


class LifecycleError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.code = code


def clean_settings(value, current=None):
    """A validated copy of the settings; raises LifecycleError with a readable message."""
    out = dict(DEFAULTS if current is None else current)
    if not isinstance(value, dict):
        raise LifecycleError("Send the settings as an object.")
    if "enabled" in value:
        if not isinstance(value["enabled"], bool):
            raise LifecycleError("Automatic unloading must be on or off.")
        out["enabled"] = value["enabled"]
    if "auto_reload" in value:
        if not isinstance(value["auto_reload"], bool):
            raise LifecycleError("Automatic reloading must be on or off.")
        out["auto_reload"] = value["auto_reload"]
    if "idle_seconds" in value:
        raw = value["idle_seconds"]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw != int(raw):
            raise LifecycleError("The idle time must be a whole number of seconds.")
        raw = int(raw)
        if not MIN_IDLE <= raw <= MAX_IDLE:
            raise LifecycleError("The idle time must be between 1 minute and 7 days.")
        out["idle_seconds"] = raw
    return out


def stored_settings(raw):
    """Settings read from disk: anything missing or broken falls back to the defaults (off)."""
    try:
        return clean_settings(raw if isinstance(raw, dict) else {})
    except LifecycleError:
        return dict(DEFAULTS)


def keep_alive_value(seconds):
    return "%ds" % int(seconds)


class Lifecycle:
    """Tracks model use, unloads idle models, and refreshes the split model hourly."""

    def __init__(self, backend, load_prefs, save_prefs, clock=time.time, log=None):
        self.backend = backend
        self.load_prefs = load_prefs          # () -> dict (the whole prefs.json)
        self.save_prefs = save_prefs          # (dict) -> None, atomic
        self.clock = clock
        self.log = log or (lambda msg: print("nodeyard lifecycle: " + msg, flush=True))
        self.lock = threading.RLock()
        self.inflight = collections.Counter()  # target -> requests through the dashboard right now
        self.next_token = 0
        self.tokens = {}
        self.events = collections.deque(maxlen=60)
        self.split = self._fresh_split("")
        self.stop_event = threading.Event()
        self.thread = None

    # -- settings ---------------------------------------------------------------------------------------------

    def settings(self):
        prefs = self.load_prefs() or {}
        return stored_settings(prefs.get(PREF_KEY))

    def pinned(self):
        prefs = self.load_prefs() or {}
        raw = prefs.get("ollama_pinned")
        return {tuple(x) for x in raw if isinstance(x, list) and len(x) == 2 and all(isinstance(v, str) for v in x)} if isinstance(raw, list) else set()

    def update(self, body):
        with self.lock:
            prefs = dict(self.load_prefs() or {})
            before = stored_settings(prefs.get(PREF_KEY))
            after = clean_settings(body, before)
            prefs[PREF_KEY] = after
            self.save_prefs(prefs)
        if after != before:
            changes = []
            if after["enabled"] != before["enabled"]:
                changes.append("automatic unloading %s%s" % ("on" if after["enabled"] else "off",
                               (" after %s idle" % describe_seconds(after["idle_seconds"])) if after["enabled"] else ""))
            if after["auto_reload"] != before["auto_reload"]:
                changes.append("hourly model reload %s" % ("on" if after["auto_reload"] else "off"))
            if after["idle_seconds"] != before["idle_seconds"] and after["enabled"] == before["enabled"] and after["enabled"]:
                changes.append("unload time set to %s" % describe_seconds(after["idle_seconds"]))
            if changes:
                self._event("settings", "; ".join(changes).capitalize() + ".", ok=True)
            # The idle time starts over from the change; nothing already idle is unloaded at once.
            with self.lock:
                if after["enabled"] and not before["enabled"] and self.split["last_used"] is not None:
                    self.split["last_used"] = max(self.split["last_used"], self.clock())
                if after["auto_reload"] != before["auto_reload"] and self.split["reload_phase"] is None:
                    self.split["reload_due_at"] = self.clock() + RELOAD_SECONDS if after["auto_reload"] and self.split["observed_at"] is not None else None
            self._apply_ollama_policy(after)
        return after

    def set_pinned(self, pod, model, on):
        """Remember Ollama models loaded from the page (they stay loaded while automatic unloading is off)."""
        with self.lock:
            prefs = dict(self.load_prefs() or {})
            pins = self.pinned()
            (pins.add if on else pins.discard)((pod, model))
            prefs["ollama_pinned"] = sorted([list(p) for p in pins])[:200]
            self.save_prefs(prefs)

    # -- use of a model through the dashboard -------------------------------------------------------------------

    def begin(self, target):
        target = str(target or "")
        with self.lock:
            self.next_token += 1
            token = self.next_token
            self.tokens[token] = target
            self.inflight[target] += 1
            if target == "split":
                self.split["last_used"] = self.clock()
        return token

    def end(self, token):
        with self.lock:
            target = self.tokens.pop(token, None)
            if target is None:
                return
            self.inflight[target] -= 1
            if self.inflight[target] <= 0:
                del self.inflight[target]
            if target == "split":
                self.split["last_used"] = self.clock()
        if target.startswith("ollama:"):
            self._after_ollama_use(target)

    def busy(self, target):
        with self.lock:
            return self.inflight.get(target, 0) > 0

    # -- Ollama: keep-alive is Ollama's own idle timer -------------------------------------------------------------

    def _after_ollama_use(self, target):
        parts = target.split(":", 2)
        if len(parts) != 3:
            return
        pod, model = parts[1], parts[2]
        settings = self.settings()
        if settings["enabled"]:
            value = keep_alive_value(settings["idle_seconds"])
        elif (pod, model) in self.pinned():
            value = -1
        else:
            return       # not this feature's business: Ollama's own default applies
        try:
            self.backend.ollama_keep_alive(pod, model, value)
        except Exception as e:  # noqa: BLE001 -- a chat that worked must not fail because of this
            self._event("ollama", "Couldn't set the idle time for %s on %s: %s" % (model, pod, e), ok=False, model=model, node=pod)

    def load_keep_alive(self):
        """The keep-alive for an Ollama model loaded from the page."""
        settings = self.settings()
        return keep_alive_value(settings["idle_seconds"]) if settings["enabled"] else -1

    def _apply_ollama_policy(self, settings):
        try:
            pods = self.backend.ollama_overview()
        except Exception:  # noqa: BLE001 -- no Ollama: nothing to do
            return
        pins = self.pinned()
        for entry in pods:
            for m in entry.get("models", []):
                if not m.get("loaded"):
                    continue
                key = (entry.get("pod", ""), m.get("name", ""))
                if settings["enabled"]:
                    value = keep_alive_value(settings["idle_seconds"])
                elif key in pins:
                    value = -1
                else:
                    continue
                if self.busy("ollama:%s:%s" % key):
                    continue        # it gets the new value when its request ends
                try:
                    self.backend.ollama_keep_alive(key[0], key[1], value)
                except Exception as e:  # noqa: BLE001
                    self._event("ollama", "Couldn't apply the idle time to %s on %s: %s" % (key[1], key[0], e), ok=False, model=key[1], node=key[0])

    # -- the split model -----------------------------------------------------------------------------------------

    @staticmethod
    def _fresh_split(model):
        return {"model": model, "last_used": None, "task": None, "processing": False, "observed_at": None, "observe_error": "",
                "unload_job": None, "unloading_since": None, "failures": 0, "retry_at": 0.0, "error": "", "auto_unloaded_at": None,
                "loaded_seen": False, "reload_due_at": None, "reload_last_at": None, "reload_phase": None, "reload_job": None,
                "reload_started_at": None, "reload_retry_at": 0.0, "reload_error": ""}

    def _split_state(self):
        st = (self.backend.store.snapshot().get("state") or {})
        return (st.get("ai") or {}).get("split")

    def observe(self, now):
        """Read the split model's slots: did it work since last time, is it working now?"""
        sp = self._split_state()
        with self.lock:
            s = self.split
            model = (sp or {}).get("model", "") if sp else ""
            if not sp or model != s["model"]:
                self.split = s = self._fresh_split(model)
            loaded = bool(sp and sp.get("loaded"))
            if not loaded:
                s["loaded_seen"] = False
                s["processing"] = False
                s["task"] = None
                if s["reload_phase"] is None:
                    s["reload_due_at"] = None
                return
            settling = s["auto_unloaded_at"] is not None and now - s["auto_unloaded_at"] < UNLOADED_SETTLE
            if not s["loaded_seen"] and not settling and s["unload_job"] is None:
                # Loaded (again): its idle time starts now, whoever loaded it.
                s["loaded_seen"] = True
                s["last_used"] = now
                s["auto_unloaded_at"] = None
            if not sp.get("ready"):
                return      # still loading: nothing to read yet, and loading is not idle time
        try:
            slots = self.backend.split_slots()
        except Exception as e:  # noqa: BLE001 -- the model may be busy loading, or /slots may be off
            with self.lock:
                self.split["observe_error"] = str(e)[:300]
            return
        with self.lock:
            s = self.split
            s["observe_error"] = ""
            s["observed_at"] = now
            task, processing = slots.get("task"), bool(slots.get("processing"))
            if sp.get("ready") and s["reload_phase"] is None and s["reload_due_at"] is None:
                s["reload_due_at"] = now + RELOAD_SECONDS
            if processing or (task is not None and s["task"] is not None and task != s["task"]):
                s["last_used"] = now
            s["task"], s["processing"] = task, processing

    def tick(self, now=None):
        """One round: observe, then unload the split model if it has been idle long enough."""
        now = self.clock() if now is None else now
        self._check_job(now)
        self.observe(now)
        if self._check_reload(now):
            return None
        settings = self.settings()
        sp = self._split_state()
        if sp and sp.get("loaded") and sp.get("stale"):
            self._auto_repair(now, sp)     # (broken models are mended whatever the hourly setting says)
            return None
        with self.lock:
            last = self.split["last_used"]
        unload_due = settings["enabled"] and last is not None and now - last >= settings["idle_seconds"]
        # (when the idle unload is due too, it wins: refreshing would load again a model the user asked to give back)
        if settings["auto_reload"] and not unload_due and self._start_reload(now, sp):
            return None
        with self.lock:
            s = self.split
            if not settings["enabled"] or not sp or not sp.get("loaded") or not sp.get("ready") or s["unload_job"] or s["reload_phase"]:
                return None
            if s["last_used"] is None or now < s["retry_at"] or not s["loaded_seen"]:
                return None      # (not seen loaded since the last unload: the cluster view may lag behind it)
            idle = now - s["last_used"]
            if idle < settings["idle_seconds"]:
                return None
            if self.inflight.get("split") or s["processing"]:
                return None
            if s["observed_at"] is None or now - s["observed_at"] > SLOTS_STALE or s["observe_error"]:
                return None      # we can't see whether something else (yardcode) is using it
        try:
            running = self.backend.jobs_running()
        except Exception:  # noqa: BLE001
            running = True
        if running:
            return None          # never race a load, switch, removal or download job
        # Last look right before acting: a request may have started since observe().
        try:
            slots = self.backend.split_slots()
        except Exception:  # noqa: BLE001
            return None
        with self.lock:
            s = self.split
            if slots.get("processing") or (slots.get("task") is not None and slots.get("task") != s["task"]) or self.inflight.get("split"):
                s["last_used"], s["task"], s["processing"] = now, slots.get("task"), bool(slots.get("processing"))
                return None
            if not self._settings_still(settings):
                return None
        try:
            job = self.backend.run("split-auto-unload", {"idle": int(idle)})
        except Exception as e:  # noqa: BLE001 -- e.g. another change started a moment ago (409)
            if getattr(e, "code", 0) == 409:
                return None
            self._failed(now, str(e))
            return None
        with self.lock:
            self.split["unload_job"] = job
            self.split["unloading_since"] = now
        self._event("unload", "Unloading %s after %s idle." % (sp.get("alias") or sp.get("model") or "the split model", describe_seconds(idle)),
                    ok=None, model=sp.get("model", ""), node=", ".join(x.get("node", "") for x in sp.get("shares", [])), idle=int(idle))
        return job

    def _start_reload(self, now, sp):
        """Begin a full unload/load cycle only when the split model is safe to interrupt."""
        if not sp or not sp.get("loaded") or not sp.get("ready"):
            return None
        with self.lock:
            s = self.split
            if (s["reload_phase"] or s["unload_job"] or not s["loaded_seen"] or s["reload_due_at"] is None
                    or now < s["reload_due_at"] or self.inflight.get("split") or s["processing"]
                    or s["observed_at"] is None or now - s["observed_at"] > SLOTS_STALE or s["observe_error"]):
                return None
            model = s["model"]
            last_task = s["task"]
        try:
            if self.backend.jobs_running():
                return None
            slots = self.backend.split_slots()
        except Exception:  # noqa: BLE001 -- if activity or job state is unknown, defer the refresh
            return None
        if slots.get("processing") or (slots.get("task") is not None and slots.get("task") != last_task):
            with self.lock:
                self.split["last_used"] = now
                self.split["task"], self.split["processing"] = slots.get("task"), bool(slots.get("processing"))
            return None
        try:
            with self.lock:
                s = self.split
                if (s["model"] != model or s["reload_phase"] or s["unload_job"] or self.inflight.get("split")
                        or not self.settings()["auto_reload"] or not s["reload_due_at"] or now < s["reload_due_at"]):
                    return None
                # Keep begin() from admitting a new dashboard request between the final busy check and job start.
                job = self.backend.run("split-unload", {})
                s["reload_phase"], s["reload_job"] = "unloading", job
                s["reload_started_at"], s["reload_error"] = now, ""
        except Exception as e:  # noqa: BLE001 -- a model lifecycle race or unavailable control command
            with self.lock:
                self.split["reload_due_at"] = now + RETRY_BASE
                self.split["reload_error"] = str(e)[:300]
            self._event("reload", "Hourly reload could not start: %s. Trying again in %s." % (e, describe_seconds(RETRY_BASE)), ok=False, model=model)
            return None
        self._event("reload", "Refreshing %s after one hour; waiting for it to finish any active work first." % (model or "the split model"),
                    ok=None, model=model)
        return job

    def force_reload(self, reason):
        """Unload and load the split model now, because it's broken (a node holding part of it restarted, say): it isn't
        answering anyway, so this doesn't wait for idle time. Returns the job id, or None if one is already under way."""
        sp = self._split_state()
        if not sp or not sp.get("loaded"):
            raise LifecycleError("There's no loaded split model to reload.", 409)
        with self.lock:
            s = self.split
            if s["reload_phase"] or s["unload_job"]:
                return None
        if self.backend.jobs_running():
            raise LifecycleError("Another model task is running. Try again when it has finished.", 409)
        now = self.clock()
        with self.lock:
            s = self.split
            job = self.backend.run("split-unload", {})
            s["reload_phase"], s["reload_job"] = "unloading", job
            s["reload_started_at"], s["reload_error"] = now, ""
            self.repaired_at = now
            model = s["model"]
        self._event("reload", "Reloading %s because %s." % (model or "the split model", reason), ok=None, model=model)
        return job

    def _auto_repair(self, now, sp):
        if now - getattr(self, "repaired_at", 0.0) < REPAIR_COOLDOWN:
            return
        try:
            self.force_reload("%s, so the layers kept there are gone" % sp["stale"]["why"])
        except Exception as e:  # noqa: BLE001 -- try again after the cooldown
            self.repaired_at = now
            self._event("reload", "The model needs reloading (%s) but that couldn't start: %s." % (sp["stale"]["why"], e), ok=False)

    def _check_reload(self, now):
        """Advance an hourly refresh. Returns true while one is active, preventing an idle-unload race."""
        with self.lock:
            s = self.split
            phase, jid, model = s["reload_phase"], s["reload_job"], s["model"]
            retry_at = s["reload_retry_at"]
        if not phase:
            return False
        sp = self._split_state()
        if not sp or (model and sp.get("model") != model):
            with self.lock:
                self.split["reload_phase"], self.split["reload_job"] = None, None
                self.split["reload_due_at"], self.split["reload_started_at"] = None, None
            self._event("reload", "Stopped the hourly refresh because the split model changed or was removed.", ok=False, model=model)
            return False

        if phase in ("unloading", "loading"):
            view = self.backend.job(jid, 0) if jid else None
            status = (view or {}).get("status", "failed")
            # The cluster can report ready before the CLI job record is refreshed.
            if phase == "loading" and sp.get("ready"):
                with self.lock:
                    s = self.split
                    s["reload_phase"], s["reload_job"] = None, None
                    s["reload_started_at"], s["reload_retry_at"], s["reload_error"] = None, 0.0, ""
                    s["reload_last_at"], s["reload_due_at"] = now, now + RELOAD_SECONDS
                    s["loaded_seen"], s["last_used"], s["auto_unloaded_at"] = True, now, None
                self._event("reload", "Reloaded %s; its next refresh is in one hour." % (model or "the split model"), ok=True, model=model)
                return False
            if status == "running":
                return True
            if status != "ok":
                lines = (view or {}).get("lines") or []
                error = (lines[-1] if lines else "the refresh task %s" % status)[:300]
                with self.lock:
                    s = self.split
                    s["reload_error"] = error
                    s["reload_retry_at"] = now + RETRY_BASE
                    if phase == "unloading":
                        s["reload_phase"], s["reload_job"] = None, None
                        s["reload_due_at"] = now + RETRY_BASE
                    else:
                        # The model is normally still unloaded if starting it failed; keep retrying the load.
                        s["reload_phase"], s["reload_job"] = "waiting_unload", None
                self._event("reload", "Hourly model refresh failed: %s. Retrying in %s." % (error, describe_seconds(RETRY_BASE)), ok=False, model=model)
                return phase == "loading"
            if phase == "unloading":
                with self.lock:
                    self.split["reload_phase"], self.split["reload_job"] = "waiting_unload", None
                    self.split["reload_retry_at"] = 0.0
                self._event("reload", "The model stopped; waiting for its servers to exit before loading it again.", ok=None, model=model)
                return True
            # A successful load command can finish before the model becomes ready.
            return True

        if phase == "waiting_unload":
            if sp.get("loaded") or now < retry_at:
                return True
            try:
                if self.backend.jobs_running():
                    return True
                job = self.backend.run("split-load", {})
            except Exception as e:  # noqa: BLE001
                with self.lock:
                    self.split["reload_error"] = str(e)[:300]
                    self.split["reload_retry_at"] = now + RETRY_BASE
                self._event("reload", "Couldn't start the model after its refresh: %s. Retrying in %s." % (e, describe_seconds(RETRY_BASE)), ok=False, model=model)
                return True
            with self.lock:
                self.split["reload_phase"], self.split["reload_job"] = "loading", job
                self.split["reload_retry_at"], self.split["reload_error"] = 0.0, ""
            self._event("reload", "Loading %s again after its hourly refresh." % (model or "the split model"), ok=None, model=model)
            return True
        return True

    def _settings_still(self, settings):
        current = self.settings()
        return current["enabled"] and current["idle_seconds"] == settings["idle_seconds"]

    def _check_job(self, now):
        with self.lock:
            jid = self.split["unload_job"]
        if not jid:
            return
        view = self.backend.job(jid, 0)
        status = (view or {}).get("status", "failed")
        if status == "running":
            return
        with self.lock:
            s = self.split
            s["unload_job"] = None
            s["unloading_since"] = None
            model = s["model"]
        if status == "ok":
            with self.lock:
                s["failures"], s["error"], s["retry_at"] = 0, "", 0.0
                s["auto_unloaded_at"] = now
                s["loaded_seen"] = False
            self._event("unload", "Unloaded %s: its memory is free. Load it again from Models or the chat when you need it." % (model or "the split model"),
                        ok=True, model=model)
        else:
            lines = (view or {}).get("lines") or []
            self._failed(now, (lines[-1] if lines else "the unload task %s" % status)[:300])

    def _failed(self, now, error):
        with self.lock:
            s = self.split
            s["failures"] += 1
            s["error"] = error
            s["retry_at"] = now + min(RETRY_MAX, RETRY_BASE * (2 ** (s["failures"] - 1)))
            model, retry = s["model"], s["retry_at"]
        self._event("unload", "Automatic unload of %s failed: %s. Trying again in %s." % (model or "the split model", error, describe_seconds(retry - now)),
                    ok=False, model=model)

    # -- what the pages show ---------------------------------------------------------------------------------------

    def view(self, now=None):
        now = self.clock() if now is None else now
        settings = self.settings()
        sp = self._split_state()
        with self.lock:
            s = dict(self.split)
            busy = bool(self.inflight.get("split"))
            events = list(self.events)[::-1]
        split = None
        if sp:
            loaded, ready = bool(sp.get("loaded")), bool(sp.get("ready"))
            idle = (now - s["last_used"]) if (loaded and ready and s["last_used"] is not None) else None
            if s["unload_job"]:
                state = "unloading"
            elif not loaded:
                state = "auto_unloaded" if s["auto_unloaded_at"] else "unloaded"
            elif not ready:
                state = "loading"
            elif busy or s["processing"]:
                state = "processing"
            elif s["observe_error"]:
                state = "activity_unknown"
            elif s["observed_at"] is None:
                state = "checking"
            elif s["error"] and now < s["retry_at"]:
                state = "unload_failed"
            elif settings["enabled"] and idle is not None and idle >= settings["idle_seconds"]:
                state = "unload_pending"
            else:
                state = "idle"
            eligible = bool(settings["enabled"] and state in ("idle", "unload_pending"))
            if not settings["auto_reload"]:
                reload_state = "off"
            elif s["reload_phase"] == "unloading":
                reload_state = "unloading"
            elif s["reload_phase"] == "waiting_unload":
                reload_state = "waiting_unload"
            elif s["reload_phase"] == "loading":
                reload_state = "loading"
            elif not loaded or not ready:
                reload_state = "waiting_for_model"
            elif s["reload_due_at"] is not None and now >= s["reload_due_at"]:
                reload_state = "waiting_for_idle"
            else:
                reload_state = "scheduled"
            split = {"model": sp.get("model", ""), "alias": sp.get("alias", ""), "state": state, "loaded": loaded, "ready": ready,
                     "idle_for": int(idle) if idle is not None else None, "last_used": s["last_used"],
                     "unload_in": max(0, int(settings["idle_seconds"] - idle)) if eligible and idle is not None else None,
                     "eligible": eligible, "error": s["error"], "retry_at": s["retry_at"] or None, "activity_error": s["observe_error"],
                     "activity_source": "llama.cpp /slots" if s["observed_at"] else "", "unload_job": s["unload_job"],
                     "reload_state": reload_state, "reload_in": max(0, int(s["reload_due_at"] - now)) if settings["auto_reload"] and s["reload_due_at"] is not None else None,
                     "reload_last_at": s["reload_last_at"], "reload_error": s["reload_error"],
                     "machines": [x.get("node", "") for x in sp.get("shares", [])]}
        ollama = []
        try:
            pins = self.pinned()
            for entry in self.backend.ollama_overview():
                for m in entry.get("models", []):
                    if m.get("loaded"):
                        ollama.append({"pod": entry.get("pod", ""), "node": entry.get("node", ""), "model": m.get("name", ""),
                                       "expires": m.get("expires", ""), "pinned": (entry.get("pod", ""), m.get("name", "")) in pins,
                                       "busy": bool(self.inflight.get("ollama:%s:%s" % (entry.get("pod", ""), m.get("name", ""))))})
        except Exception:  # noqa: BLE001
            ollama = []
        return {"settings": settings, "presets": list(PRESETS), "min_idle": MIN_IDLE, "max_idle": MAX_IDLE,
                "split": split, "ollama": ollama, "events": events[:30], "poll_seconds": POLL_SECONDS}

    def _event(self, kind, message, ok=None, **extra):
        item = dict({"time": self.clock(), "kind": kind, "message": message, "ok": ok}, **{k: v for k, v in extra.items() if v not in (None, "")})
        with self.lock:
            self.events.append(item)
        self.log(message)

    # -- background loop ------------------------------------------------------------------------------------------

    def start(self):
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._loop, name="nodeyard-model-lifecycle", daemon=True)
        self.thread.start()

    def _loop(self):
        delay = 3      # first look soon after start, then every POLL_SECONDS
        while not self.stop_event.wait(delay):
            delay = POLL_SECONDS
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 -- one bad round must not stop idle checks for good
                self.log("idle check failed: %s" % e)


def describe_seconds(seconds):
    seconds = int(max(0, seconds))
    if seconds < 90:
        return "%d s" % seconds
    if seconds < 5400:
        return "%d min" % round(seconds / 60)
    hours, minutes = divmod(round(seconds / 60), 60)
    return "%d h%s" % (hours, (" %d min" % minutes) if minutes else "")


def register(ctx, args):
    backend = getattr(ctx, "ai", None)
    settings = getattr(ctx, "settings", None)
    if backend is None or settings is None:
        return None

    def save(prefs):
        with settings.lock:
            settings._save_prefs(prefs)

    lc = Lifecycle(backend, settings.prefs, save)
    ctx.lifecycle = lc
    backend.lifecycle = lc

    def view(h, q):
        h._json(dict(lc.view(), ok=True))

    def update(h, body):
        try:
            h._json({"ok": True, "settings": lc.update(body), "view": lc.view()})
        except LifecycleError as e:
            h._json({"ok": False, "error": str(e)}, e.code)
        except OSError as e:
            h._json({"ok": False, "error": "Couldn't save the setting: %s" % (e.strerror or e)}, 500)

    def reload_now(h, body):
        try:
            job = lc.force_reload("you asked for it")
            h._json({"ok": True, "job": job, "view": lc.view()})
        except LifecycleError as e:
            h._json({"ok": False, "error": str(e)}, e.code)

    ctx.get_routes.update({"/api/ai/lifecycle": view, "/api/v1/lifecycle": view})
    ctx.post_routes["/api/ai/reload-model"] = reload_now
    ctx.post_routes.update({"/api/ai/lifecycle": update, "/api/v1/lifecycle": update})
    lc.start()
    return lc
