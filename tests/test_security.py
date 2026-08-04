"""WebSocket origin validation (CSWSH protection)."""

from __future__ import annotations

import unittest

from backend.main import origin_allowed


class TestOriginAllowed(unittest.TestCase):
    def test_same_origin_allowed(self):
        self.assertTrue(origin_allowed("http://localhost:8765", "localhost:8765"))
        self.assertTrue(origin_allowed("http://192.168.1.5:8765", "192.168.1.5:8765"))

    def test_cross_origin_denied(self):
        self.assertFalse(origin_allowed("http://evil.example", "localhost:8765"))
        self.assertFalse(origin_allowed("http://localhost:9999", "localhost:8765"))

    def test_non_browser_allowed(self):
        self.assertTrue(origin_allowed("", "localhost:8765"))
        self.assertTrue(origin_allowed("null", "localhost:8765"))

    def test_malformed_origin_denied(self):
        self.assertFalse(origin_allowed("not-a-url", "localhost:8765"))
