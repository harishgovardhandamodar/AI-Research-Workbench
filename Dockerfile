FROM python:3.12-slim

LABEL org.opencontainers.image.title="Fox — AI Science Workbench"
LABEL org.opencontainers.image.description="Local AI science workbench (chat + agent + persistent kernels + notebooks)"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FOX_WORKBENCH_DIR=/app/workbench

WORKDIR /app

# System deps: fonts for matplotlib figures, build tools for numpy/scipy wheels
# and R-style packages are NOT needed; R kernel simply reports unavailable.
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-dejavu-core \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml ./

# Core + kernel stack + optional MCP + jupyter_server (matching the local setup).
RUN pip install -r requirements.txt \
        numpy pandas scipy matplotlib scikit-learn \
        jupyter_server

COPY . .

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health')" || exit 1

CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8765"]
