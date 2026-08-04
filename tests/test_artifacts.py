"""Artifact store tests: find_by_name + report-name normalization."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.artifacts.store import Artifact, ArtifactStore, _normalize_artifact_name


class ArtifactStoreTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ArtifactStore(self.tmp)

    def _fig(self, name, code="plt.plot()"):
        art = Artifact(kind="figure", name=name, description="fig",
                       code=code, env={})
        self.store.add_artifact(art, data=b"\x89PNG", data_type="png")
        return art.id

    def test_find_by_exact_name(self):
        fid = self._fig("fig_peer_coverage")
        art = self.store.find_by_name("fig_peer_coverage")
        self.assertIsNotNone(art)
        self.assertEqual(art.id, fid)

    def test_find_by_report_decorated_name(self):
        fid = self._fig("fig_peer_coverage")
        # Reports reference 'new_fig_peer_coverage_seed<digits>'.
        art = self.store.find_by_name("new_fig_peer_coverage_seed482917563")
        self.assertIsNotNone(art, "decorated name should resolve to base artifact")
        self.assertEqual(art.id, fid)

    def test_normalize(self):
        self.assertEqual(_normalize_artifact_name("new_fig_x_seed123"), "fig_x")
        self.assertEqual(_normalize_artifact_name("fig_x"), "fig_x")
        self.assertEqual(_normalize_artifact_name("new_a_b_seed9"), "a_b")

    def test_unknown_name_returns_none(self):
        self.assertIsNone(self.store.find_by_name("definitely_missing_artifact"))


if __name__ == "__main__":
    unittest.main()
