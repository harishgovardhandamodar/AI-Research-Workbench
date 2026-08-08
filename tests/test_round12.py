"""Round-12 tests: LLM retry-with-backoff + the proactive next-research agenda."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.llm import LLMClient, LLMError
from backend.next_research import _goal_reached, next_research_agenda
from backend.store import ProjectStore


class _FakeCompletions:
    def __init__(self, fail_count, exc, result=None):
        self.calls = 0
        self.fail_count = fail_count
        self.exc = exc
        self.result = result

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_count:
            raise self.exc
        return self.result


class _FakeMsg:
    class _Choice:
        def __init__(self):
            self.message = type("M", (), {"content": "hi", "tool_calls": None})()

    def __init__(self):
        self.choices = [self._Choice()]


class LLMRetryTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, completions):
        client = LLMClient.__new__(LLMClient)
        client.retries = 2
        client.retry_backoff = 0.0
        client.model = "m"
        client.temperature = 0.2
        client.max_tokens = 10

        class _Chat:
            def __init__(self):
                self.completions = completions

        class _C:
            chat = _Chat()

        client._tool = _C()
        client._gateway = _C()
        return client

    async def test_retries_transient_then_succeeds(self):
        fake = _FakeCompletions(2, ConnectionError("reset"), result=_FakeMsg())
        client = self._client(fake)
        out = await client.complete([{"role": "user", "content": "x"}], tools=None)
        self.assertEqual(out["content"], "hi")
        self.assertEqual(fake.calls, 3)

    async def test_non_transient_raises_immediately(self):
        fake = _FakeCompletions(5, ValueError("bad request"))
        client = self._client(fake)
        with self.assertRaises(LLMError):
            await client.complete([{"role": "user", "content": "x"}], tools=None)
        self.assertEqual(fake.calls, 1)

    async def test_exhausts_retries_on_persistent_transient(self):
        fake = _FakeCompletions(10, ConnectionError("down"))
        client = self._client(fake)
        with self.assertRaises(LLMError):
            await client.complete([{"role": "user", "content": "x"}], tools=None)
        self.assertEqual(fake.calls, 3)  # retries + 1


class NextResearchTests(unittest.TestCase):
    def setUp(self):
        self.store = ProjectStore(Path(tempfile.mkdtemp()))

    def test_agenda_lists_experiment_below_target(self):
        eid = self.store.create_experiment("A", "", "acc", 0.9, True)
        self.store.add_run("p", "r", "done", 1.0, 2.0,
                           metrics={"acc": 0.7}, experiment_id=eid)
        agenda = next_research_agenda(_Rt(self.store))
        self.assertIn("Push toward target", agenda)
        self.assertIn("A", agenda)

    def test_agenda_lists_no_gain_learnings(self):
        eid = self.store.create_experiment("A", "", "acc", 0.9, True)
        rid = self.store.add_run("p", "r", "done", 1.0, 2.0,
                                 metrics={"acc": 0.7}, experiment_id=eid)
        self.store.add_learning(eid, rid, "acc", 0.7, 0.7, 0.0, 0,
                                "Tried X: acc 0.7->0.7 (no gain).", "suggestion")
        agenda = next_research_agenda(_Rt(self.store))
        self.assertIn("What didn't work", agenda)

    def test_agenda_settled_message(self):
        agenda = next_research_agenda(_Rt(self.store))
        self.assertIn("looks settled", agenda)

    def test_goal_reached(self):
        g = {"metric": "acc", "target": 0.8, "higher_better": True}
        self.assertFalse(_goal_reached(self.store, g))
        eid = self.store.create_experiment("A", "", "acc", 0.9, True)
        self.store.add_run("p", "r", "done", 1.0, 2.0,
                           metrics={"acc": 0.85}, experiment_id=eid)
        self.assertTrue(_goal_reached(self.store, g))


class _Rt:
    def __init__(self, store):
        self.store = store


if __name__ == "__main__":
    unittest.main()
