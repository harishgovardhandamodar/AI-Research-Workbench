# fox-kernel remote agent — GPU-server image (e.g. axiom, 2× RTX5080).
# Build on the workbench host (repo root as context):
#   docker build -f deploy/remote-agent/Dockerfile -t fox-kernel:remote .
# Or via the compose file next to it (also enables GPU passthrough).
# Needs the NVIDIA Container Toolkit on the GPU host for nvidia-smi/CUDA
# visibility inside the container; without it the agent still runs (CPU).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FOX_KERNEL_PORT=8891

WORKDIR /opt/fox-kernel

# Agent sources only (stdlib + 2 pip packages; see requirements.txt).
COPY backend/__init__.py ./backend/__init__.py
COPY backend/paths.py ./backend/paths.py
COPY backend/kernels/__init__.py backend/kernels/server.py \
     backend/kernels/manager.py backend/kernels/python_kernel.py \
     backend/kernels/r_kernel.py backend/kernels/worker.py \
     ./backend/kernels/
COPY deploy/remote-agent/requirements.txt ./

RUN pip install -r requirements.txt

# Workspace for executed code (mounted from host in compose).
RUN mkdir -p /workspace
VOLUME /workspace

EXPOSE 8891

# No background pollers in the agent itself; health endpoint is cheap.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8891/health', timeout=4)"

# REMOTE_TOKEN must be provided at run time (compose fails fast if unset).
CMD ["sh", "-c", "python -m backend.kernels.server --host 0.0.0.0 --port ${FOX_KERNEL_PORT:-8891} --cwd /workspace"]
