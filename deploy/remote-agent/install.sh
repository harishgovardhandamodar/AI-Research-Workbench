#!/usr/bin/env bash
# install.sh — install the fox-kernel remote agent on a LAN/Tailscale host.
# Works from a repo checkout (this file's dir is deploy/remote-agent/) or from
# the unpacked tarball (same relative layout). No passwords exchanged: the API
# token is generated here and shown once.
#
#   sudo ./install.sh    # from deploy/remote-agent/ or tarball root narration below
#
# Canonical use (tarball on the remote host):
#   tar xzf fox-kernel-remote-<sha>.tar.gz && cd fox-kernel-remote
#   sudo ./deploy/remote-agent/install.sh
# User-local, no sudo (skips systemd/ufw):
#   PREFIX=~/fox-kernel ./deploy/remote-agent/install.sh
#
# Env overrides: PREFIX (default /opt/fox-kernel), PORT (default 8891),
# WORKSPACE_DIR (default $HOME/axiom-workspace), REMOTE_TOKEN (else generated).
set -euo pipefail

SRC="$(cd "$(dirname "$0")/../.." && pwd)"
PREFIX="${PREFIX:-/opt/fox-kernel}"
PORT="${PORT:-8891}"
WORKSPACE_DIR="${WORKSPACE_DIR:-$HOME/axiom-workspace}"
SERVICE_SRC="${SERVICE_SRC:-$SRC/deploy/axiom/fox-kernel.service}"

have_sudo=false
if [ "$(id -u)" -eq 0 ] || command -v sudo >/dev/null 2>&1; then
  have_sudo=true
fi

echo "==> [1/5] Installing files to $PREFIX ..."
if [ -w "$(dirname "$PREFIX")" ]; then
  mkdir -p "$PREFIX"
  cp -r "$SRC/backend" "$SRC/deploy" "$PREFIX"/
else
  sudo mkdir -p "$PREFIX"
  sudo cp -r "$SRC/backend" "$SRC/deploy" "$PREFIX"/
  sudo chown -R "$(id -u):$(id -g)" "$PREFIX"
fi
mkdir -p "$WORKSPACE_DIR"

echo "==> [2/5] Creating venv + installing deps (fastapi, uvicorn) ..."
python3 -m venv "$PREFIX/.venv"
"$PREFIX/.venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/.venv/bin/pip" install --quiet -r "$PREFIX/deploy/remote-agent/requirements.txt"

echo "==> [3/5] API token (generated on this host, shown once, never logged) ..."
if [ -n "${REMOTE_TOKEN:-}" ]; then
  TOKEN="$REMOTE_TOKEN"
  echo "    using REMOTE_TOKEN from environment."
else
  TOKEN="$("$PREFIX/.venv/bin/python" -c "import secrets; print(secrets.token_hex(32))")"
fi

echo "==> [4/5] Smoke test (health + gpu, no token needed) ..."
"$PREFIX/.venv/bin/python" -m backend.kernels.server --help >/dev/null
(
  cd "$PREFIX"
  REMOTE_TOKEN="$TOKEN" ./.venv/bin/python -m backend.kernels.server \
    --host 127.0.0.1 --port "$PORT" --cwd "$WORKSPACE_DIR" >/tmp/fox-kernel-smoke.log 2>&1 &
  SRV=$!
  for _ in $(seq 1 20); do
    curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
    sleep 1
  done
  echo "    health: $(curl -s "http://127.0.0.1:$PORT/health")"
  echo "    gpu: $(curl -s "http://127.0.0.1:$PORT/api/kernel/gpu")"
  # token guard must reject anonymous execute with 403:
  CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "http://127.0.0.1:$PORT/api/kernel/execute" \
    -H 'Content-Type: application/json' -d '{"code":"print(1)"}')
  echo "    anonymous execute -> HTTP $CODE (want 403)"
  kill "$SRV" 2>/dev/null || true
)

if [ "$have_sudo" = true ] && [ "$PREFIX" = "/opt/fox-kernel" ]; then
  echo "==> [5/5] systemd + firewall ..."
  sudo cp "$SERVICE_SRC" /etc/systemd/system/fox-kernel.service
  echo "REMOTE_TOKEN=$TOKEN" | sudo tee /etc/fox-kernel.env >/dev/null
  sudo chmod 600 /etc/fox-kernel.env
  sudo systemctl daemon-reload
  sudo systemctl enable --now fox-kernel
  echo "    ufw (tailnet-only): sudo ufw allow in on tailscale0 to any port $PORT proto tcp"
else
  echo "==> [5/5] Skipped systemd (user-local install). Run manually:"
  echo "    cd $PREFIX && REMOTE_TOKEN=<token> ./.venv/bin/python -m backend.kernels.server --host 0.0.0.0 --port $PORT --cwd $WORKSPACE_DIR"
fi

echo ""
echo "    TOKEN (paste once into workbench Remote tab, then forget it): $TOKEN"
echo "    Verify from workbench host: curl -s http://axiom:$PORT/health"
echo "    Logs (systemd): journalctl -u fox-kernel -f"
