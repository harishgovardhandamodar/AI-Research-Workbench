"""Chat UX backend tests: message deletion, data-schema preview, and the
active-experiment context injected into the LLM prompt."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from backend.routers.artifacts import _schema_from_file
from backend.store import ProjectStore


class DeleteMessageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)

    def test_delete_removes_one_message(self):
        mid_a = self.store.add_message("user", "hello", {"tags": []})
        mid_b = self.store.add_message("user", "world", {"tags": []})
        self.assertTrue(self.store.delete_message(mid_a))
        ids = [m["id"] for m in self.store.list_messages()]
        self.assertEqual(ids, [mid_b])
        self.assertIsNone(self.store.get_message(mid_a))

    def test_delete_missing_returns_false(self):
        self.assertFalse(self.store.delete_message(9999))

    def test_delete_and_reopen_persists(self):
        mid = self.store.add_message("assistant", "text", {"tags": ["x"]})
        self.store.delete_message(mid)
        store2 = ProjectStore(self.tmp)
        self.assertEqual(store2.list_messages(), [])


class SchemaReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _write(self, name: str, data: bytes | str) -> Path:
        p = self.tmp / name
        p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return p

    def test_csv_schema(self):
        p = self._write("data.csv", "a,b,c\n1,x,1.5\n2,y,2.5\n")
        s = _schema_from_file(p)
        names = [c["name"] for c in s["columns"]]
        self.assertEqual(names, ["a", "b", "c"])
        kinds = {c["name"]: c["kind"] for c in s["columns"]}
        self.assertEqual(kinds["a"], "numeric")
        self.assertEqual(kinds["b"], "text")
        self.assertGreaterEqual(s["rows"], 2)
        self.assertEqual(len(s["preview"]), 2)
        self.assertEqual(s["preview"][0]["a"], "1")

    def test_json_schema(self):
        p = self._write("data.json", '[{"a": 1, "b": "x"}]')
        s = _schema_from_file(p)
        self.assertEqual([c["name"] for c in s["columns"]], ["a", "b"])
        self.assertEqual(len(s["preview"]), 1)

    def test_tsv_schema(self):
        p = self._write("data.tsv", "a\tb\n1\tx\n")
        s = _schema_from_file(p)
        self.assertEqual([c["name"] for c in s["columns"]], ["a", "b"])

    def test_unsupported_extension_raises(self):
        p = self._write("notes.md", "# hi")
        with self.assertRaises(HTTPException):
            _schema_from_file(p)

    def test_empty_file_raises(self):
        p = self._write("data.csv", "")
        with self.assertRaises(HTTPException):
            _schema_from_file(p)


class ExperimentContextInjectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)

    def _runtime(self):
        from backend.project_runtime import ProjectRuntime
        rt = object.__new__(ProjectRuntime)  # skip the heavy constructor
        rt.store = self.store
        return rt

    def test_active_experiment_context_injected(self):
        rt = self._runtime()
        eid = self.store.create_experiment(
            "acc-sweep", "more data helps", "accuracy", 0.9, True,
            plan="try lr in [0.01, 0.1]")
        self.store.add_run("p", "r", "done", 1.0, 2.0,
                           metrics={"accuracy": 0.85}, experiment_id=eid)
        self.store.add_message("user", "keep going", {"tags": []})
        msgs = rt.build_llm_messages()
        system = msgs[0]["content"]
        self.assertIn("Active experiment context", system)
        self.assertIn("acc-sweep", system)
        self.assertIn("accuracy", system)
        self.assertIn("0.85", system)

    def test_no_active_experiment_skips_context(self):
        rt = self._runtime()
        self.store.add_message("user", "hi", {"tags": []})
        msgs = rt.build_llm_messages()
        self.assertNotIn("Active experiment context", msgs[0]["content"])


if __name__ == "__main__":
    unittest.main()
