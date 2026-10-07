"""Tests for the node agent: parsers and a whole sample taken from a fake /proc and /sys."""
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "share", "nodeyard", "agent"))

import agent  # noqa: E402

UID = "8b2f4968-d34f-47bb-8000-0000642899c3"


def write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


def stat_line(pid, comm, ppid, ticks, threads, start, state="S"):
    # pid (comm) state ppid ... utime(14) stime(15) ... num_threads(20) ... starttime(22)
    fields = [state, str(ppid)] + ["0"] * 9 + [str(ticks), "0", "0", "0", "20", "0", str(threads), "0", str(start)]
    return "%d (%s) %s" % (pid, comm, " ".join(fields))


class Parsers(unittest.TestCase):
    def test_meminfo(self):
        m = agent.parse_meminfo("MemTotal:       8000000 kB\nMemAvailable:   4000000 kB\nSwapTotal:            0 kB\nBogus: 5 kB\n")
        self.assertEqual(m["MemTotal"], 8000000 * 1024)
        self.assertEqual(m["MemAvailable"], 4000000 * 1024)
        self.assertNotIn("Bogus", m)

    def test_cpu_times_and_pressure(self):
        t = agent.parse_cpu_times("cpu  100 0 100 700 100 0 0 0\ncpu0 50 0 50 350 50 0 0 0\nintr 1\n")
        self.assertEqual(t["all"], (200, 1000))
        self.assertEqual(t[0], (100, 500))
        self.assertEqual(agent.parse_pressure("some avg10=1.50 avg60=0.5 avg300=0.1 total=1\nfull avg10=0.25 avg60=0 avg300=0 total=0\n"), {"some": 1.5, "full": 0.25})

    def test_stat_line_handles_odd_names(self):
        st = agent.parse_stat_line(stat_line(12, "tmux: server (x)", 1, 250, 3, 999))
        self.assertEqual((st[0], st[1], st[2], st[3], st[4], st[5]), ("tmux: server (x)", "S", 1, 250, 3, 999))
        self.assertIsNone(agent.parse_stat_line("garbage"))

    def test_groups(self):
        pod = "0::/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod%s.slice/cri-containerd-%s.scope" % (UID.replace("-", "_"), "ab" * 32)
        self.assertEqual(agent.parse_group(pod, "x", 100, 200), {"kind": "pod", "uid": UID, "container": "abababababab"})
        self.assertEqual(agent.parse_group("0::/system.slice/mariadb.service", "x", 1, 5)["name"], "mariadb.service")
        self.assertEqual(agent.parse_group("0::/user.slice/user-1000.slice/session-3.scope", "x", 1, 5)["kind"], "user")
        self.assertEqual(agent.parse_group("0::/", "kthreadd", 0, 2)["kind"], "kernel")
        self.assertEqual(agent.parse_group("0::/", "weird", 2, 77)["kind"], "kernel")

    def test_secrets_are_kept_off_command_lines(self):
        self.assertEqual(agent.redact("llama-server --api-key abcd1234 --port 8080"), "llama-server --api-key *** --port 8080")
        self.assertEqual(agent.redact("app --password=hunter2 -x"), "app --password=*** -x")
        self.assertEqual(agent.redact("curl -H X " + "A" * 60), "curl -H X ***")
        self.assertLessEqual(len(agent.redact("x " * 500)), 240)


class WholeSample(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        r = self.tmp.name
        self.proc, self.sys = os.path.join(r, "proc"), os.path.join(r, "sys")
        write(self.proc, "meminfo", "MemTotal: 8000000 kB\nMemFree: 1000000 kB\nMemAvailable: 3000000 kB\nCached: 2000000 kB\nAnonPages: 4000000 kB\n")
        write(self.proc, "uptime", "3600.00 100.00\n")
        write(self.proc, "loadavg", "0.50 0.40 0.30 1/200 999\n")
        write(self.proc, "cpuinfo", "processor : 0\nModel : Raspberry Pi 5 Model B\n")
        write(self.proc, "vmstat", "oom_kill 3\npgmajfault 7\n")
        write(self.proc, "pressure/memory", "some avg10=2.00 avg60=1 avg300=0 total=0\n")
        write(self.proc, "stat", "cpu  100 0 100 700 100 0 0 0\ncpu0 50 0 50 350 50 0 0 0\ncpu1 50 0 50 350 50 0 0 0\n")
        pod = "0::/kubepods.slice/kubepods-pod%s.slice/cri-containerd-%s.scope\n" % (UID.replace("-", "_"), "cd" * 32)
        for pid, comm, ppid, rss_pages, cg, cmd in [
            (1, "systemd", 0, 3000, "0::/init.scope\n", "/sbin/init"),
            (200, "rpc-server", 1, 2_000_000, pod, "rpc-server --api-key supersecretvalue1234 -p 50052"),
            (300, "mysqld", 1, 90000, "0::/system.slice/mariadb.service\n", "/usr/sbin/mariadbd"),
        ]:
            write(self.proc, "%d/stat" % pid, stat_line(pid, comm, ppid, 100, 4, 500))
            write(self.proc, "%d/statm" % pid, "0 %d 0 0 0 0 0\n" % rss_pages)
            write(self.proc, "%d/status" % pid, "Name:\t%s\nUid:\t%d\t0\t0\t0\nThreads:\t4\nVmRSS:\t%d kB\nRssAnon:\t%d kB\n" % (comm, 0 if pid != 300 else 999, rss_pages * 4, rss_pages * 3))
            write(self.proc, "%d/cgroup" % pid, cg)
            write(self.proc, "%d/cmdline" % pid, cmd.replace(" ", "\x00"))
        write(self.sys, "devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", "1800000\n")
        write(self.sys, "devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", "2400000\n")
        write(self.sys, "devices/system/cpu/cpu0/cpufreq/cpuinfo_min_freq", "600000\n")
        write(self.sys, "devices/system/cpu/cpu0/cpufreq/scaling_governor", "ondemand\n")
        write(self.sys, "devices/system/cpu/cpu1/cpufreq/scaling_cur_freq", "600000\n")
        write(self.sys, "class/thermal/thermal_zone0/temp", "61250\n")
        write(self.sys, "class/thermal/thermal_zone0/type", "cpu-thermal\n")
        write(self.sys, "class/hwmon/hwmon2/name", "rpi_volt\n")
        write(self.sys, "class/hwmon/hwmon2/in0_lcrit_alarm", "1\n")
        passwd = os.path.join(r, "passwd")
        write(r, "passwd", "root:x:0:0::/root:/bin/sh\nmysql:x:999:999::/var/lib/mysql:/bin/false\n")
        self.agent = agent.Agent(self.proc, self.sys, passwd)

    def test_first_sample_reports_memory_hardware_and_processes(self):
        s = self.agent.sample(now=1000.0)
        self.assertEqual(s["memory"]["MemAvailable"], 3000000 * 1024)
        self.assertEqual(s["load"], [0.5, 0.4, 0.3])
        self.assertEqual(s["cpu"]["model"], "Raspberry Pi 5 Model B")
        self.assertEqual([c["mhz"] for c in s["cpu"]["cores"]], [1800, 600])
        self.assertEqual(s["cpu"]["cores"][0]["max"], 2400)
        self.assertEqual(s["cpu"]["cores"][0]["governor"], "ondemand")
        self.assertEqual(s["temps"], [{"name": "cpu-thermal", "c": 61.25}])
        self.assertTrue(s["power"]["undervoltage"])
        self.assertEqual(s["vmstat"]["oom_kill"], 3)
        self.assertEqual(s["pressure"]["memory"], {"some": 2.0})
        self.assertEqual(s["counts"]["total"], 3)

    def test_processes_are_ranked_grouped_and_cleaned(self):
        s = self.agent.sample(now=1000.0)
        names = [p["name"] for p in s["processes"]]
        self.assertEqual(names[0], "rpc-server")
        top = s["processes"][0]
        self.assertEqual(top["group"], {"kind": "pod", "uid": UID, "container": "cdcdcdcdcdcd"})
        self.assertNotIn("supersecretvalue1234", top["cmd"])
        self.assertIn("--api-key ***", top["cmd"])
        self.assertEqual(top["user"], "root")
        by_name = {p["name"]: p for p in s["processes"]}
        self.assertEqual(by_name["mysqld"]["user"], "mysql")
        kinds = {g["kind"] for g in s["groups"]}
        self.assertEqual(kinds, {"pod", "service", "system"})

    def test_cpu_use_comes_from_the_difference_between_samples(self):
        self.agent.sample(now=1000.0)
        write(self.proc, "stat", "cpu  200 0 200 750 100 0 0 0\ncpu0 150 0 150 375 50 0 0 0\ncpu1 50 0 50 375 50 0 0 0\n")
        write(self.proc, "200/stat", stat_line(200, "rpc-server", 1, 400, 4, 500))  # +300 ticks in 10 s = 30 %
        s = self.agent.sample(now=1010.0)
        self.assertAlmostEqual(next(p for p in s["processes"] if p["pid"] == 200)["cpu"], 30.0, places=1)
        self.assertAlmostEqual(s["cpu"]["use"], 80.0, places=1)  # busy +200 of +250 ticks
        self.assertEqual(s["cpu"]["cores"][1]["use"], 0.0)

    def test_a_missing_proc_does_not_crash(self):
        a = agent.Agent("/nonexistent-proc", "/nonexistent-sys", "/nonexistent-passwd")
        s = a.sample(now=1.0)
        self.assertEqual(s["counts"]["total"], 0)
        self.assertEqual(s["processes"], [])


if __name__ == "__main__":
    unittest.main()
