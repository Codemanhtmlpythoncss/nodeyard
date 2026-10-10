"""Automatic model unloading (lifecycle.py) with a fake cluster and a clock the tests move."""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "share", "nodeyard", "dashboard"))

import lifecycle  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeStore:
    def __init__(self, split):
        self.split = split

    def snapshot(self):
        return {"state": {"ai": {"split": self.split}}}


class FakeBackend:
    """Just enough of aiapi.Live: the split model's state, its /slots, jobs and Ollama keep-alive calls."""

    def __init__(self):
        self.store = FakeStore({"model": "big.gguf", "alias": "big", "loaded": True, "ready": True, "shares": [{"node": "gpu-node"}, {"node": "cpu-node"}]})
        self.slots = {"processing": False, "task": 1}
        self.slots_error = None
        self.running = False
        self.started = []
        self.job_status = {}
        self.run_error = None
        self.keep_alive = []
        self.ollama = [{"pod": "ollama-a", "node": "node-a", "models": [{"name": "small:1b", "loaded": True}]},
                       {"pod": "ollama-b", "node": "node-b", "models": [{"name": "small:1b", "loaded": False}]}]

    def split_slots(self):
        if self.slots_error:
            raise RuntimeError(self.slots_error)
        return dict(self.slots)

    def jobs_running(self):
        return self.running

    def run(self, action, params):
        if self.run_error:
            raise self.run_error
        jid = "job%d" % (len(self.started) + 1)
        self.started.append((action, params, jid))
        self.job_status[jid] = "running"
        return jid

    def job(self, jid, since):
        return {"status": self.job_status.get(jid, "failed"), "lines": ["Couldn't stop the model's servers."]}

    def ollama_overview(self):
        return self.ollama

    def ollama_keep_alive(self, pod, model, value):
        self.keep_alive.append((pod, model, value))


class Prefs:
    def __init__(self, data=None):
        self.data = data or {}

    def load(self):
        return json.loads(json.dumps(self.data))

    def save(self, value):
        self.data = json.loads(json.dumps(value))


def make(settings=None):
    backend, clock, prefs = FakeBackend(), Clock(), Prefs({"model_lifecycle": settings} if settings else {})
    lc = lifecycle.Lifecycle(backend, prefs.load, prefs.save, clock=clock, log=lambda m: None)
    return lc, backend, clock, prefs


def advance(lc, clock, seconds, step=15):
    """Run idle checks every `step` seconds for `seconds`; returns the first job started, if any."""
    started = None
    end = clock.now + seconds
    while clock.now < end:
        clock.now = min(end, clock.now + step)
        job = lc.tick()
        started = started or job
    return started


class Settings(unittest.TestCase):
    def test_off_by_default_for_installs_without_a_setting(self):
        lc, *_ = make()
        self.assertEqual(lc.settings(), {"enabled": False, "idle_seconds": 1800})
        self.assertEqual(lifecycle.stored_settings({"enabled": "yes", "idle_seconds": -5}), {"enabled": False, "idle_seconds": 1800})

    def test_invalid_values_are_rejected(self):
        lc, *_ = make()
        for bad in ({"idle_seconds": 0}, {"idle_seconds": -60}, {"idle_seconds": 30}, {"idle_seconds": 8 * 24 * 3600},
                    {"idle_seconds": 90.5}, {"idle_seconds": "600"}, {"idle_seconds": True}, {"enabled": "on"}, []):
            with self.subTest(bad=bad), self.assertRaises(lifecycle.LifecycleError):
                lc.update(bad)
        self.assertEqual(lc.settings()["enabled"], False)

    def test_settings_persist_across_a_restart(self):
        lc, backend, clock, prefs = make()
        lc.update({"enabled": True, "idle_seconds": 600})
        again = lifecycle.Lifecycle(backend, prefs.load, prefs.save, clock=clock, log=lambda m: None)
        self.assertEqual(again.settings(), {"enabled": True, "idle_seconds": 600})
        self.assertEqual(prefs.data["model_lifecycle"], {"enabled": True, "idle_seconds": 600})

    def test_presets_are_offered(self):
        lc, *_ = make()
        self.assertEqual(lc.view()["presets"], [300, 600, 900, 1800, 2700, 3600, 7200])


class SplitModel(unittest.TestCase):
    def test_disabled_never_unloads(self):
        lc, backend, clock, _ = make({"enabled": False, "idle_seconds": 300})
        self.assertIsNone(advance(lc, clock, 3600))
        self.assertEqual(backend.started, [])

    def test_enabled_unloads_after_the_idle_time(self):
        lc, backend, clock, _ = make({"enabled": True, "idle_seconds": 300})
        lc.tick()                                    # first look: idle time starts now
        self.assertIsNone(advance(lc, clock, 280))
        job = advance(lc, clock, 60)
        self.assertEqual(job, "job1")
        self.assertEqual(backend.started[0][0], "split-auto-unload")
        self.assertEqual(lc.view()["split"]["state"], "unloading")

    def test_use_before_the_timeout_gives_a_fresh_idle_period(self):
        lc, backend, clock, _ = make({"enabled": True, "idle_seconds": 900})
        lc.tick()
        advance(lc, clock, 14 * 60)
        backend.slots["task"] += 1                   # a request reached the model (e.g. yardcode, directly)
        self.assertIsNone(advance(lc, clock, 2 * 60))  # 16 minutes after loading, 1-2 after the request
        self.assertIsNone(advance(lc, clock, 12 * 60))
        self.assertEqual(advance(lc, clock, 2 * 60), "job1")

    def test_active_request_through_the_dashboard_is_never_unloaded(self):
        lc, backend, clock, _ = make({"enabled": True, "idle_seconds": 300})
        lc.tick()
        token = lc.begin("split")
        clock.now += 3600                            # one very long answer
        self.assertIsNone(advance(lc, clock, 600))
        lc.end(token)
        self.assertIsNone(advance(lc, clock, 200))
        self.assertEqual(advance(lc, clock, 200), "job1")

    def test_processing_or_queued_work_on_the_model_blocks_unloading(self):
        lc, backend, clock, _ = make({"enabled": True, "idle_seconds": 300})
        lc.tick()
        backend.slots["processing"] = True
        self.assertIsNone(advance(lc, clock, 1800))
        backend.slots["processing"] = False
        backend.slots["task"] += 1                   # the queued request started and finished between two looks
        self.assertIsNone(advance(lc, clock, 200))
        self.assertEqual(lc.view()["split"]["state"], "idle")

    def test_concurrent_requests_keep_it_loaded_until_the_last_one_ends(self):
        lc, backend, clock, _ = make({"enabled": True, "idle_seconds": 300})
        lc.tick()
        a, b = lc.begin("split"), lc.begin("split")
        lc.end(a)
        self.assertIsNone(advance(lc, clock, 900))
        lc.end(b)
        lc.end(b)                                    # ending twice is harmless
        self.assertIsNone(advance(lc, clock, 250))
        self.assertEqual(advance(lc, clock, 100), "job1")

    def test_a_request_starting_right_before_the_unload_wins(self):
        lc, backend, clock, _ = make({"enabled": True, "idle_seconds": 300})
        lc.tick()
        advance(lc, clock, 290)
        real = backend.split_slots
        calls = []

        def slots():
            calls.append(1)
            out = real()
            if len(calls) == 2:                      # the last check before acting sees new work
                out["processing"] = True
            return out
        backend.split_slots = slots
        clock.now += 30
        self.assertIsNone(lc.tick())
        self.assertEqual(backend.started, [])

    def test_unknown_activity_is_never_treated_as_idle(self):
        lc, backend, clock, _ = make({"enabled": True, "idle_seconds": 300})
        backend.slots_error = "The model's activity endpoint (/slots) answered HTTP 501."
        self.assertIsNone(advance(lc, clock, 3600))
        view = lc.view()["split"]
        self.assertEqual(view["state"], "activity_unknown")
        lc2, *_ = make({"enabled": True, "idle_seconds": 300})
        self.assertEqual(lc2.view()["split"]["state"], "checking")
        self.assertIn("/slots", view["activity_error"])

    def test_manually_unloaded_model_is_left_alone(self):
        lc, backend, clock, _ = make({"enabled": True, "idle_seconds": 300})
        lc.tick()
        backend.store.split = dict(backend.store.split, loaded=False, ready=False)
        self.assertIsNone(advance(lc, clock, 3600))
        self.assertEqual(lc.view()["split"]["state"], "unloaded")

    def test_never_races_a_load_switch_or_removal_job(self):
        lc, backend, clock, _ = make({"enabled": True, "idle_seconds": 300})
        lc.tick()
        backend.running = True
        self.assertIsNone(advance(lc, clock, 3600))
        backend.running = False
        self.assertEqual(advance(lc, clock, 15), "job1")

    def test_a_job_that_starts_in_between_is_not_a_failure(self):
        lc, backend, clock, _ = make({"enabled": True, "idle_seconds": 300})
        lc.tick()
        err = Exception("Another change to the model is still running.")
        err.code = 409
        backend.run_error = err
        self.assertIsNone(advance(lc, clock, 600))
        self.assertEqual(lc.view()["split"]["error"], "")

    def test_disabling_stops_future_unloads(self):
        lc, backend, clock, _ = make({"enabled": True, "idle_seconds": 300})
        lc.tick()
        advance(lc, clock, 200)
        lc.update({"enabled": False})
        self.assertIsNone(advance(lc, clock, 3600))
        self.assertFalse(lc.view()["split"]["eligible"])

    def test_turning_it_on_starts_the_idle_time_over(self):
        lc, backend, clock, _ = make({"enabled": False, "idle_seconds": 300})
        lc.tick()
        advance(lc, clock, 3600)
        lc.update({"enabled": True})
        self.assertIsNone(advance(lc, clock, 250))
        self.assertEqual(advance(lc, clock, 100), "job1")

    def test_changing_the_timeout_applies_to_the_next_checks(self):
        lc, backend, clock, _ = make({"enabled": True, "idle_seconds": 3600})
        lc.tick()
        advance(lc, clock, 900)
        self.assertIsNone(lc.tick())
        lc.update({"idle_seconds": 600})             # already idle for 15 minutes: over the new limit
        clock.now += 15
        self.assertEqual(lc.tick(), "job1")
        lc2, backend2, clock2, _ = make({"enabled": True, "idle_seconds": 300})
        lc2.tick()
        advance(lc2, clock2, 200)
        lc2.update({"idle_seconds": 3600})           # made longer: no unload at the old limit
        self.assertIsNone(advance(lc2, clock2, 600))

    def test_failed_unload_keeps_state_and_retries_with_backoff(self):
        lc, backend, clock, _ = make({"enabled": True, "idle_seconds": 300})
        lc.tick()
        self.assertEqual(advance(lc, clock, 330), "job1")
        backend.job_status["job1"] = "failed"
        clock.now += 15
        lc.tick()
        view = lc.view()["split"]
        self.assertEqual(view["state"], "unload_failed")
        self.assertIn("Couldn't stop", view["error"])
        self.assertTrue(view["loaded"])               # still loaded: the page doesn't pretend otherwise
        self.assertIsNone(advance(lc, clock, 250))     # 5 minutes before the first retry
        self.assertEqual(advance(lc, clock, 60), "job2")  # monitoring carries on after a failure
        backend.job_status["job2"] = "failed"
        clock.now += 15
        lc.tick()
        self.assertGreaterEqual(lc.split["retry_at"] - clock.now, 590)  # then 10 minutes

    def test_successful_unload_is_recorded_and_never_reloaded(self):
        lc, backend, clock, _ = make({"enabled": True, "idle_seconds": 300})
        lc.tick()
        advance(lc, clock, 330)
        backend.job_status["job1"] = "ok"
        backend.store.split = dict(backend.store.split, loaded=False, ready=False)
        clock.now += 15
        lc.tick()
        self.assertEqual(lc.view()["split"]["state"], "auto_unloaded")
        self.assertIsNone(advance(lc, clock, 7200))
        self.assertEqual(len(backend.started), 1)
        self.assertTrue(any(e["ok"] is True and "Unloaded big.gguf" in e["message"] for e in lc.view()["events"]))

    def test_a_lagging_cluster_view_after_an_unload_does_not_start_another(self):
        lc, backend, clock, _ = make({"enabled": True, "idle_seconds": 60})
        lc.tick()
        self.assertEqual(advance(lc, clock, 90), "job1")
        backend.job_status["job1"] = "ok"              # done, but the cluster view still says loaded for a while
        self.assertIsNone(advance(lc, clock, 55))
        self.assertEqual(len(backend.started), 1)
        self.assertIsNone(advance(lc, clock, 30))      # past the settle time: treated as loaded again, idle restarts
        self.assertEqual(len(backend.started), 1)

    def test_loading_again_starts_a_new_idle_period(self):
        lc, backend, clock, _ = make({"enabled": True, "idle_seconds": 300})
        lc.tick()
        advance(lc, clock, 330)
        backend.job_status["job1"] = "ok"
        backend.store.split = dict(backend.store.split, loaded=False, ready=False)
        advance(lc, clock, 600)
        backend.store.split = dict(backend.store.split, loaded=True, ready=True)
        self.assertIsNone(advance(lc, clock, 250))
        self.assertEqual(advance(lc, clock, 100), "job2")

    def test_a_different_model_gets_its_own_state(self):
        lc, backend, clock, _ = make({"enabled": True, "idle_seconds": 300})
        lc.tick()
        advance(lc, clock, 200)
        backend.store.split = dict(backend.store.split, model="other.gguf")   # switched (or removed and redeployed)
        self.assertIsNone(advance(lc, clock, 200))
        self.assertEqual(lc.view()["split"]["model"], "other.gguf")

    def test_removed_model_has_no_state(self):
        lc, backend, clock, _ = make({"enabled": True, "idle_seconds": 300})
        lc.tick()
        backend.store.split = None
        self.assertIsNone(advance(lc, clock, 3600))
        self.assertIsNone(lc.view()["split"])

    def test_unload_covers_every_machine_of_the_split_model(self):
        lc, backend, clock, _ = make({"enabled": True, "idle_seconds": 300})
        lc.tick()
        advance(lc, clock, 330)
        event = next(e for e in lc.view()["events"] if e["kind"] == "unload")
        self.assertEqual(event["node"], "gpu-node, cpu-node")
        self.assertEqual(lc.view()["split"]["machines"], ["gpu-node", "cpu-node"])


class Ollama(unittest.TestCase):
    def test_pinned_model_stays_loaded_after_a_chat_while_off(self):
        lc, backend, clock, _ = make({"enabled": False, "idle_seconds": 300})
        lc.set_pinned("ollama-a", "small:1b", True)
        lc.end(lc.begin("ollama:ollama-a:small:1b"))
        self.assertEqual(backend.keep_alive, [("ollama-a", "small:1b", -1)])

    def test_unpinned_model_keeps_ollamas_own_default_while_off(self):
        lc, backend, clock, _ = make({"enabled": False, "idle_seconds": 300})
        lc.end(lc.begin("ollama:ollama-a:small:1b"))
        self.assertEqual(backend.keep_alive, [])

    def test_enabled_gives_the_idle_time_to_the_right_pod_after_each_use(self):
        lc, backend, clock, _ = make({"enabled": True, "idle_seconds": 600})
        lc.end(lc.begin("ollama:ollama-b:small:1b"))
        self.assertEqual(backend.keep_alive, [("ollama-b", "small:1b", "600s")])
        self.assertEqual(lc.load_keep_alive(), "600s")

    def test_turning_it_on_applies_to_loaded_models_only(self):
        lc, backend, clock, _ = make()
        lc.update({"enabled": True, "idle_seconds": 900})
        self.assertEqual(backend.keep_alive, [("ollama-a", "small:1b", "900s")])

    def test_a_failed_keep_alive_does_not_break_the_chat(self):
        lc, backend, clock, _ = make({"enabled": True, "idle_seconds": 600})

        def boom(*a):
            raise OSError("connection refused")
        backend.ollama_keep_alive = boom
        lc.end(lc.begin("ollama:ollama-a:small:1b"))
        self.assertTrue(any(e["ok"] is False for e in lc.view()["events"]))


class AiapiWiring(unittest.TestCase):
    def test_auto_unload_runs_the_same_unload_command(self):
        import aiapi
        title, argv = aiapi.build_command("split-auto-unload", {"idle": 1800}, set(), "")
        self.assertEqual(argv, ["ai", "split", "unload", "--yes"])
        self.assertIn("30 min", title)

    def test_open_chat_records_use_until_the_connection_closes(self):
        import aiapi

        class Conn:
            closed = 0

            def close(self):
                Conn.closed += 1

        live = aiapi.Live.__new__(aiapi.Live)
        lc, *_ = make({"enabled": True, "idle_seconds": 600})
        live.lifecycle = lc
        live._open_chat = lambda target, payload, on_conn=None: (Conn(), object())
        conn, _ = live.open_chat("split", {})
        self.assertTrue(lc.busy("split"))
        conn.close()
        conn.close()
        self.assertFalse(lc.busy("split"))
        self.assertEqual(Conn.closed, 2)

        def fail(*a, **k):
            raise aiapi.AIError("The model isn't loaded yet.", 503)
        live._open_chat = fail
        with self.assertRaises(aiapi.AIError):
            live.open_chat("split", {})
        self.assertFalse(lc.busy("split"))


if __name__ == "__main__":
    unittest.main()
