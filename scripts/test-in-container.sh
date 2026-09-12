#!/bin/sh
# Rebuild the fox image from the working tree, then run the full suite
# inside it. The container runs a COPY of the repo (not a live mount),
# so running tests without rebuilding validates stale code.
set -eu
cd "$(dirname "$0")/.."
docker compose build fox
docker compose up -d fox
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8765/api/health >/dev/null 2>&1; then break; fi
  sleep 2
done
docker exec fox-workbench python -m unittest discover -s tests
