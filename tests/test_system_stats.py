"""Tests for the dgxtop-style server resource stats collector + route."""

from __future__ import annotations

import unittest
from unittest import mock

from backend import system_stats
from backend.routers import system as system_router


class TestGpuParsing(unittest.TestCase):
    def test_gpu_devices_parse(self):
        fake_out = "0, NVIDIA GB10, 42, 65536, 12000, 53536, 44, 38.5\n"
        with mock.patch.object(system_stats, "_run", return_value=fake_out):
            devs = system_stats._gpu_devices()
        self.assertEqual(len(devs), 1)
        d = devs[0]
        self.assertEqual(d["index"], 0)
        self.assertEqual(d["name"], "NVIDIA GB10")
        self.assertEqual(d["utilization_percent"], 42.0)
        self.assertEqual(d["memory_total_mb"], 65536)
        self.assertEqual(d["memory_used_mb"], 12000)
        self.assertEqual(d["memory_free_mb"], 53536)
        self.assertEqual(d["temperature_c"], 44.0)
        self.assertEqual(d["power_watts"], 38.5)

    def test_gpu_unavailable_returns_empty(self):
        with mock.patch.object(system_stats, "_run", return_value=""):
            self.assertEqual(system_stats._gpu_devices(), [])

    def test_gpu_processes_parse(self):
        fake_out = "1234, python, 2048\n42, /usr/bin/node, 512\n"
        with mock.patch.object(system_stats, "_run", return_value=fake_out):
            procs = system_stats._gpu_processes()
        self.assertEqual(procs, [
            {"pid": 1234, "name": "python", "gpu_memory_mb": 2048},
            {"pid": 42, "name": "/usr/bin/node", "gpu_memory_mb": 512},
        ])

    def test_gpu_processes_bad_line_skipped(self):
        with mock.patch.object(system_stats, "_run", return_value="garbage\n"):
            self.assertEqual(system_stats._gpu_processes(), [])


class TestCpuParsing(unittest.TestCase):
    def test_parse_cpu_line(self):
        # user=100 nice=20 system=30 idle=500 iowait=5 irq=0 softirq=0
        total, idle = system_stats._parse_cpu_line("cpu  100 20 30 500 5 0 0")
        self.assertEqual(total, 655)
        self.assertEqual(idle, 505)  # idle + iowait

    def test_parse_cpu_line_short(self):
        self.assertEqual(system_stats._parse_cpu_line("cpu 1 2"), (0, 0))

    def test_cpu_usage_delta(self):
        system_stats._LAST_CPU.update({"ts": 0.0, "total": 0, "idle": 0, "cores": {}})
        with mock.patch.object(system_stats, "_read", return_value="cpu  0 0 0 1000 0 0 0\n"):
            first = system_stats._cpu_usage()
        self.assertEqual(first["total"], 0.0)  # no baseline yet
        with mock.patch.object(system_stats, "_read",
                               return_value="cpu  100 0 0 1900 0 0 0\n"):
            second = system_stats._cpu_usage()
        # delta: total 1000->2000 (+1000), idle 1000->1900 (+900) -> 10% busy
        self.assertAlmostEqual(second["total"], 10.0, places=1)


class TestStatsShape(unittest.TestCase):
    def test_stats_shape(self):
        s = system_stats.get_stats()
        self.assertIn("host", s)
        self.assertIn("cpu", s["host"])
        self.assertIn("memory", s["host"])
        self.assertIn("gpu", s)
        self.assertIn("processes", s)
        self.assertIn("collected_at", s)
        self.assertIn("uptime_s", s["host"])
        self.assertGreaterEqual(s["host"]["cpu"]["count"], 1)

    def test_cache_returns_same_until_ttl(self):
        s1 = system_stats.get_stats()
        s2 = system_stats.get_stats()
        self.assertEqual(s1["collected_at"], s2["collected_at"])


class TestRoute(unittest.TestCase):
    def test_stats_route_registered(self):
        paths = {getattr(r, "path", "") for r in system_router.router.routes}
        self.assertIn("/api/system/stats", paths)


if __name__ == "__main__":
    unittest.main()
