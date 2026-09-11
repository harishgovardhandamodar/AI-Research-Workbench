#!/usr/bin/env bash
# bin/build-remote-agent.sh — build the deployable fox-kernel tarball.
# Packages ONLY the execution kernel (stdlib + fastapi/uvicorn), not the full
# workbench: backend/__init__.py, backend/paths.py, backend/kernels/.
#
#   ./bin/build-remote-agent.sh
#   # -> dist/fox-kernel-remote-<shortsha>.tar.gz
#
# Copy the tarball to the remote host (scp/usb), extract, run install.sh there.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

SHA="$(git rev-parse --short HEAD 2>/dev/null || echo nosha)"
NAME="fox-kernel-remote-$SHA"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
PKG="$STAGE/$NAME"
mkdir -p "$PKG/backend/kernels" "$PKG"

# Repo-relative layout is preserved so docker compose (context: ../..) and
# install.sh work identically from a repo checkout or the unpacked tarball.
# Kernel sources only (verified: stdlib + fastapi/uvicorn/pydantic).
# remote.py is the workbench-side CLIENT — excluded on purpose (it would pull
# httpx/websockets and is never imported by the agent).
mkdir -p "$PKG/backend/kernels" "$PKG/deploy/remote-agent" "$PKG/deploy/axiom"
cp backend/__init__.py backend/paths.py "$PKG/backend"/
cp backend/kernels/__init__.py backend/kernels/server.py \
   backend/kernels/manager.py backend/kernels/python_kernel.py \
   backend/kernels/r_kernel.py backend/kernels/worker.py \
   "$PKG/backend/kernels"/
cp deploy/remote-agent/requirements.txt deploy/remote-agent/install.sh \
   deploy/remote-agent/README.md deploy/remote-agent/Dockerfile \
   deploy/remote-agent/docker-compose.yml \
   deploy/remote-agent/docker-compose.cpu.yml "$PKG/deploy/remote-agent"/
cp deploy/axiom/fox-kernel.service "$PKG/deploy/axiom"/
chmod +x "$PKG/deploy/remote-agent/install.sh"

# Safety: the tarball must never contain secrets.
if grep -rniE "password1|sk-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{8,}" "$PKG" 2>/dev/null; then
  echo "FAIL: possible secret in package" >&2
  exit 1
fi

# Syntax-check every file with stdlib only (no deps needed).
python3 -m py_compile $(find "$PKG" -name "*.py")
echo "py_compile: OK ($(find "$PKG" -name '*.py' | wc -l | tr -d ' ') files)"

mkdir -p dist
# COPYFILE_DISABLE=1: stop macOS bsdtar from adding AppleDouble (._*) xattr files.
COPYFILE_DISABLE=1 tar czf "dist/$NAME.tar.gz" -C "$STAGE" "$NAME"
echo "Built: dist/$NAME.tar.gz ($(du -h "dist/$NAME.tar.gz" | cut -f1))"
echo "Contents:"
tar tzf "dist/$NAME.tar.gz"
