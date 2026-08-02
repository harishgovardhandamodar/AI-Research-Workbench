#!/usr/bin/env bash
# Run the Fox AI Science Workbench as an addon inside a Jupyter server.
# The workbench becomes available at  http://localhost:8888/fox/
set -euo pipefail
cd "$(dirname "$0")"

# Ensure the extension is importable and registered (editable install).
.venv/bin/pip install -q -e . || true

# Enable the extension if it isn't already (config under .venv/etc/jupyter).
if ! .venv/bin/jupyter server extension list 2>/dev/null | grep -q jupyter_fox; then
  .venv/bin/jupyter server extension enable jupyter_fox || true
fi

# Force-enable via our config file so it loads even if the CLI validation hiccups.
mkdir -p .venv/etc/jupyter
cat > .venv/etc/jupyter/jupyter_server_config.json <<'JSON'
{
  "ServerApp": {
    "jpserver_extensions": { "jupyter_fox": true }
  }
}
JSON

PORT="${PORT:-8888}"
echo "Starting Jupyter server on :${PORT} — Fox workbench at http://localhost:${PORT}/fox/"
exec .venv/bin/jupyter server --ServerApp.port="${PORT}" --ServerApp.open_browser=false \
  --ServerApp.config_file=.venv/etc/jupyter/jupyter_server_config.json
