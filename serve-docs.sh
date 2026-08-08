#!/usr/bin/env bash
# Serve the GitBook-style documentation locally.
#   ./serve-docs.sh          -> http://localhost:4000
#   ./serve-docs.sh 8080     -> http://localhost:8080
set -e
cd "$(dirname "$0")"
port="${1:-4000}"
echo "Fox docs → http://localhost:${port}/   (Ctrl+C to stop)"
python3 -m http.server "$port" --directory gitbook
