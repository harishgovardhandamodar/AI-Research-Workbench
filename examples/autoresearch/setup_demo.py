"""Set up the autonomous-research demo on the Kaggle Titanic dataset.

Creates/refreshes a project named ``kaggle-demo``:
  - downloads the classic Titanic dataset into ``data/titanic_train.csv``
  - seeds ``research/program.md`` + ``research/experiment.py`` (autoresearch target)
  - creates the ``titanic survival`` experiment (goal metric accuracy)

Then demonstrate both loops:
  - Improve loop:  ask Fox to improve the "titanic survival" experiment
  - Autoresearch:  🤖 Autoresearch quick action or ``/autoresearch accuracy``

Usage (inside the workbench container or on a host with network):
    .venv/bin/python examples/autoresearch/setup_demo.py [project_name]
The dataset URL is a public GitHub mirror of the Kaggle Titanic dataset (no API
key needed). Point the demo at the real Kaggle API instead by using the
workbench's Kaggle import once credentials are configured.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

PROJECT = sys.argv[1] if len(sys.argv) > 1 else "kaggle-demo"
TITANIC_URL = ("https://raw.githubusercontent.com/datasciencedojo/datasets/"
               "master/titanic.csv")
HERE = Path(__file__).resolve().parent
EXPERIMENT_SRC = HERE / "titanic" / "experiment.py"
PROGRAM_SRC = HERE / "titanic" / "program.md"


def main() -> None:
    proj = Path("/app/workbench/projects") / PROJECT
    data_dir = proj / "data"
    research = proj / "research"
    data_dir.mkdir(parents=True, exist_ok=True)
    research.mkdir(parents=True, exist_ok=True)

    print(f"Downloading Titanic dataset -> {data_dir / 'titanic_train.csv'} …")
    with urllib.request.urlopen(TITANIC_URL, timeout=30) as r:
        raw = r.read()
    (data_dir / "titanic_train.csv").write_bytes(raw)
    print(f"  saved {len(raw):,} bytes")

    exp = research / "experiment.py"
    exp.write_text(EXPERIMENT_SRC.read_text())
    prog = research / "program.md"
    prog.write_text(PROGRAM_SRC.read_text())
    print(f"  research files ready: {exp} / {prog}")

    print(f"Project '{PROJECT}' ready. Create the experiment in the UI or ask "
          "Fox, then run:\n"
          "  - Improve loop:  'Improve the titanic survival experiment toward its goal'\n"
          "  - Autoresearch:  🤖 Autoresearch (or /autoresearch accuracy)")


if __name__ == "__main__":
    main()
