"""Set up the autonomous-research demo on the Kaggle credit-card fraud dataset.

Creates/refreshes a project named ``fraud-demo``:
  - downloads the classic Kaggle creditcardfraud dataset (via the OpenML mirror
    of the ULB dataset, ARFF -> CSV) into ``data/creditcard.csv``
  - seeds ``research/program.md`` + ``research/experiment.py`` (autoresearch target)
  - creates the ``creditcard fraud`` experiment (goal metric roc_auc)

Usage (inside the workbench container):
    python examples/autoresearch/creditcard/setup_demo.py [project_name]

Then run the loop with the autoresearch MCP tool:
    research_run(project="fraud-demo", goal_metric="roc_auc", max_iters=6)
or in the UI: 🤖 Autoresearch (set goal_metric=roc_auc).
"""

from __future__ import annotations

import re
import sys
import urllib.request
from pathlib import Path

PROJECT = sys.argv[1] if len(sys.argv) > 1 else "fraud-demo"
OPENML_URL = "https://www.openml.org/data/v1/download/1673544"  # creditcard (did 1597)
HERE = Path(__file__).resolve().parent
EXPERIMENT_SRC = HERE / "experiment.py"
PROGRAM_SRC = HERE / "program.md"


def arff_to_csv(raw: str) -> str:
    """Strip the @relation/@attribute headers from an all-numeric ARFF file and
    return a CSV (header row + data rows)."""
    lines = raw.splitlines()
    data_idx = next(i for i, l in enumerate(lines) if l.strip() == "@data")
    header = re.findall(r"@attribute\s+(\S+)", "\n".join(lines[:data_idx]))
    body = [l for l in lines[data_idx + 1:] if l.strip()]
    return ",".join(header) + "\n" + "\n".join(body) + "\n"


def main() -> None:
    proj = Path("/app/workbench/projects") / PROJECT
    data_dir = proj / "data"
    research = proj / "research"
    data_dir.mkdir(parents=True, exist_ok=True)
    research.mkdir(parents=True, exist_ok=True)

    dest = data_dir / "creditcard.csv"
    if not dest.exists():
        print("Downloading Kaggle creditcardfraud dataset (OpenML mirror) …")
        with urllib.request.urlopen(OPENML_URL, timeout=300) as r:
            raw = r.read().decode(errors="replace")
        print(f"  raw {len(raw):,} chars, converting ARFF -> CSV")
        csv_text = arff_to_csv(raw)
        dest.write_text(csv_text)
        print(f"  saved {len(csv_text):,} chars to {dest}")
    else:
        print(f"dataset already present: {dest}")

    (research / "experiment.py").write_text(EXPERIMENT_SRC.read_text())
    (research / "program.md").write_text(PROGRAM_SRC.read_text())
    print(f"research files ready: {research}")

    print(f"Project '{PROJECT}' ready. Run the loop via the autoresearch MCP:\n"
          f"  research_run(project='{PROJECT}', goal_metric='roc_auc', max_iters=6)\n"
          "or in the UI: 🤖 Autoresearch")


if __name__ == "__main__":
    main()
