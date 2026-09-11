# fox-kernel remote agent — deployable package

Lightweight deployable of the workbench execution kernel for LAN/Tailscale
hosts (e.g. axiom, 2× RTX5080). Contents of the tarball built by
`bin/build-remote-agent.sh`:

- `backend/` — execution kernel + headless server only (stdlib + 2 pip packages)
- `requirements.txt` — `fastapi`, `uvicorn[standard]`
- `install.sh` — venv + deps + token + systemd + verify (run on the host)
- `fox-kernel.service` — hardened systemd unit (also at `deploy/axiom/`)

On the remote host — pick one:

```bash
tar xzf fox-kernel-remote-<sha>.tar.gz
cd fox-kernel-remote
sudo ./deploy/remote-agent/install.sh            # or: PREFIX=~/fox-kernel ./deploy/remote-agent/install.sh (no sudo: skips systemd/ufw)
```

Docker (GPU servers that are docker-first; needs NVIDIA Container Toolkit
for GPU visibility, else the agent runs CPU-only):

```bash
cd fox-kernel-remote/deploy/remote-agent
REMOTE_TOKEN=<token> docker compose up -d --build          # GPU passthrough
REMOTE_TOKEN=<token> docker compose -f docker-compose.yml -f docker-compose.cpu.yml up -d --build   # CPU-only
```

`install.sh` prints the token once (generated on the host, never transmitted).
Paste it into the workbench **Remote tab** → **Discover** → offload with
**require GPU**. Full tunnel procedure: `docs/REMOTE-WORKBENCH.md`.
