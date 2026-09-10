"""Remote workbench proxy tests: URL normalization, host validation, token
redaction/mask-preservation, GPU payload parsing, and HTTP error mapping.

Style mirrors tests/test_mcp_management.py (unittest + mock, backend imports).
Run inside the workbench image: docker exec fox-workbench python -m pytest tests/test_remote.py
"""

from __future__ import annotations

import unittest
from unittest import mock

from backend.routers import remote
from backend.routers.system import _MCP_MASK


class NormalizeBaseUrlTest(unittest.TestCase):
    def test_strips_trailing_slash(self):
        self.assertEqual(remote.normalize_base_url("http://axiom:8891/"),
                         "http://axiom:8891")

    def test_defaults_to_http(self):
        self.assertEqual(remote.normalize_base_url("192.168.1.173:8891"),
                         "http://192.168.1.173:8891")

    def test_tailscale_name(self):
        self.assertEqual(remote.normalize_base_url("axiom"),
                         "http://axiom")

    def test_strips_userinfo(self):
        # Credentials in the URL would echo back unredacted — drop them.
        self.assertEqual(remote.normalize_base_url("http://fox:secret@axiom:8891/"),
                         "http://axiom:8891")

    def test_ipv6(self):
        self.assertEqual(remote.normalize_base_url("http://[::1]:8891"),
                         "http://[::1]:8891")

    def test_rejects_non_http(self):
        with self.assertRaises(ValueError):
            remote.normalize_base_url("ftp://axiom:21")

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            remote.normalize_base_url("  ")


class ValidateHostEntryTest(unittest.TestCase):
    def test_defaults_name_and_id(self):
        h = remote.validate_host_entry({"base_url": "http://axiom:8891"})
        self.assertEqual(h["name"], "axiom")
        self.assertTrue(h["id"])
        self.assertTrue(h["verify_tls"])
        self.assertTrue(h["use_gpu"])

    def test_invalid_base_url_raises(self):
        with self.assertRaises(ValueError):
            remote.validate_host_entry({"base_url": "ftp://axiom:21"})

    def test_non_dict_raises(self):
        with self.assertRaises(ValueError):
            remote.validate_host_entry(["http://axiom:8891"])


class RedactMergeTest(unittest.TestCase):
    def test_redact_masks_token_only(self):
        hosts = [{"id": "a", "name": "axiom", "base_url": "http://axiom:8891",
                  "token": "s3cret"}]
        red = remote.redact_hosts(hosts)
        self.assertEqual(red[0]["token"], _MCP_MASK)
        self.assertEqual(red[0]["name"], "axiom")
        # original untouched
        self.assertEqual(hosts[0]["token"], "s3cret")

    def test_redact_leaves_empty_token(self):
        red = remote.redact_hosts([{"id": "a", "token": ""}])
        self.assertEqual(red[0]["token"], "")

    def test_merge_preserves_masked_token(self):
        orig = [{"id": "a", "name": "axiom", "base_url": "http://axiom:8891",
                 "username": "fox", "token": "live-token",
                 "verify_tls": True, "use_gpu": True}]
        new = [{"id": "a", "name": "axiom", "base_url": "http://axiom:8891/",
                "username": "fox", "token": _MCP_MASK,
                "verify_tls": True, "use_gpu": True}]
        merged = remote.merge_hosts(orig, new)
        self.assertEqual(merged[0]["token"], "live-token")
        self.assertEqual(merged[0]["base_url"], "http://axiom:8891")

    def test_merge_mask_without_old_token_stays_empty(self):
        # The mask literal must never be persisted as a token (fails closed).
        merged = remote.merge_hosts([], [{"id": "a", "name": "axiom",
                                          "base_url": "http://axiom:8891",
                                          "token": _MCP_MASK}])
        self.assertEqual(merged[0]["token"], "")

    def test_merge_replaces_with_new_token(self):
        orig = [{"id": "a", "name": "axiom", "base_url": "http://axiom:8891",
                 "token": "old"}]
        merged = remote.merge_hosts(orig, [{"id": "a", "name": "axiom",
                                            "base_url": "http://axiom:8891",
                                            "token": "new"}])
        self.assertEqual(merged[0]["token"], "new")


class ParseGpuPayloadTest(unittest.TestCase):
    def test_full_shape(self):
        p = remote.parse_gpu_payload({
            "available": True,
            "devices": [{"index": 0, "name": "RTX5080", "memory_total_mb": 16303,
                         "memory_free_mb": 16000, "utilization_percent": 5.0,
                         "temperature_c": 41.0}],
            "error": ""})
        self.assertTrue(p["available"])
        self.assertEqual(len(p["devices"]), 1)
        self.assertEqual(p["error"], "")

    def test_empty_means_unavailable(self):
        p = remote.parse_gpu_payload({"available": False, "devices": [],
                                      "error": "nvidia-smi not found"})
        self.assertFalse(p["available"])
        self.assertIn("nvidia-smi", p["error"])

    def test_non_dict(self):
        p = remote.parse_gpu_payload(None)
        self.assertFalse(p["available"])

    def test_available_without_devices_is_unavailable(self):
        p = remote.parse_gpu_payload({"available": True, "devices": []})
        self.assertFalse(p["available"])


class HttpJsonTest(unittest.TestCase):
    def test_timeout_maps_cleanly(self):
        import requests

        with mock.patch.object(remote.requests, "Session") as sess:
            sess.return_value.__enter__.return_value.request.side_effect = requests.Timeout()
            status, data, err = remote._http_json("GET", "http://x/health", timeout=1)
        self.assertIsNone(status)
        self.assertIsNone(data)
        self.assertEqual(err, "timed out")

    def test_connection_error_maps_cleanly(self):
        import requests

        with mock.patch.object(remote.requests, "Session") as sess:
            sess.return_value.__enter__.return_value.request.side_effect = requests.ConnectionError()
            status, data, err = remote._http_json("GET", "http://x/health", timeout=1)
        self.assertIsNone(status)
        self.assertIn("unreachable", err)

    def test_non_json_body_reported(self):
        resp = mock.Mock(status_code=200)
        resp.json.side_effect = ValueError("nope")
        resp.text = "<html>hi</html>"
        with mock.patch.object(remote.requests, "Session") as sess:
            sess.return_value.__enter__.return_value.request.return_value = resp
            status, data, err = remote._http_json("GET", "http://x/health", timeout=1)
        self.assertEqual(status, 200)
        self.assertIsNone(data)
        self.assertIn("non-JSON", err)


class SeedFromEnvTest(unittest.TestCase):
    def test_seed_ids_stable_across_calls(self):
        import os

        cfg = {"remote": {"hosts": [], "active_host": ""}}
        env = {"REMOTE_HOSTS": "http://axiom:8891, http://192.168.1.173:8891",
               "REMOTE_TOKEN": "t", "REMOTE_USER": "fox"}
        with mock.patch.object(remote, "CONFIG", cfg):
            with mock.patch.dict(os.environ, env, clear=False):
                first, seeded = remote._seed_from_env()
                second, _ = remote._seed_from_env()
        self.assertTrue(seeded)
        self.assertEqual(len(first), 2)
        self.assertEqual([h["id"] for h in first], [h["id"] for h in second])
        self.assertTrue(all(h["id"].startswith("seed-") for h in first))
        self.assertEqual(first[0]["token"], "t")

    def test_no_env_no_seed(self):
        import os

        cfg = {"remote": {"hosts": [], "active_host": ""}}
        with mock.patch.object(remote, "CONFIG", cfg):
            with mock.patch.dict(os.environ, {}, clear=True):
                hosts, seeded = remote._seed_from_env()
        self.assertEqual(hosts, [])
        self.assertFalse(seeded)


if __name__ == "__main__":
    unittest.main()
