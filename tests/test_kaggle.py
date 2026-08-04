"""Kaggle import tests: slug validation, zip extraction (with zip-slip guard),
single-file downloads, and error mapping. Network is mocked out."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from backend import kaggle
from backend.kaggle import KaggleError, import_dataset, validate_slug


class FakeRT:
    def __init__(self, base: Path):
        self.dir = base


class ValidateSlugTests(unittest.TestCase):

    def test_valid_slugs(self):
        self.assertEqual(validate_slug("alexisbcook/titanic"), ("alexisbcook", "titanic"))
        self.assertEqual(validate_slug("  owner/dataset-2  "), ("owner", "dataset-2"))

    def test_invalid_slugs(self):
        for bad in ("", "noslash", "a/b/c", "a b/c", "../etc/passwd", "owner/../x"):
            with self.assertRaises(ValueError, msg=bad):
                validate_slug(bad)


class ImportTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.rt = FakeRT(self.tmp)
        self.old_download = kaggle._download
        self.old_user = os.environ.get("KAGGLE_USERNAME")
        self.old_key = os.environ.get("KAGGLE_KEY")
        os.environ["KAGGLE_USERNAME"] = "test-user"
        os.environ["KAGGLE_KEY"] = "test-key"

    def tearDown(self):
        kaggle._download = self.old_download
        if self.old_user is None:
            os.environ.pop("KAGGLE_USERNAME", None)
        else:
            os.environ["KAGGLE_USERNAME"] = self.old_user
        if self.old_key is None:
            os.environ.pop("KAGGLE_KEY", None)
        else:
            os.environ["KAGGLE_KEY"] = self.old_key

    def _zip_bytes(self, entries: dict[str, bytes]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        return buf.getvalue()

    def test_extracts_zip_into_data_dir(self):
        data = self._zip_bytes({"train.csv": "a,b\n1,2\n", "sub/t.csv": "x\n"})

        def fake_download(url, dest):
            dest.write_bytes(data)

        kaggle._download = fake_download
        result = import_dataset(self.rt, "owner/ds")
        self.assertEqual(result["dataset"], "owner/ds")
        self.assertEqual(result["dir"], "data/owner__ds")
        self.assertIn("data/owner__ds/train.csv", result["files"])
        self.assertIn("data/owner__ds/sub/t.csv", result["files"])
        self.assertTrue((self.tmp / "data/owner__ds/train.csv").is_file())

    def test_zip_slip_is_blocked(self):
        data = self._zip_bytes({"../../escape.txt": "boom"})

        def fake_download(url, dest):
            dest.write_bytes(data)

        kaggle._download = fake_download
        with self.assertRaises(KaggleError):
            import_dataset(self.rt, "owner/ds")
        self.assertFalse((self.tmp / "escape.txt").exists())

    def test_single_file_download(self):
        def fake_download(url, dest):
            dest.write_bytes(b"col1,col2\n1,2\n")

        kaggle._download = fake_download
        result = import_dataset(self.rt, "owner/ds")
        self.assertEqual(result["files"], ["data/owner__ds/ds"])
        self.assertTrue((self.tmp / "data/owner__ds/ds").read_text().startswith("col1"))

if __name__ == "__main__":
    unittest.main()
