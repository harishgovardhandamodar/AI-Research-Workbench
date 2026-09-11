# fox-kernel remote agent — deployable package

Lightweight deployable of the workbench execution kernel for LAN/Tailscale
hosts (e.g. axiom, 2× RTX5080). Contents of the tarball built by
`bin/build-remote-agent.sh`:

- `backend/` — execution kernel + headless server only (stdlib + 2 pip packages)
- `requirements.txt` — `fastapi`, `uvicorn[standard]`
- `install.sh` — venv + deps + token + systemd + verify (run on the host)
- `fox-kernel.service` — hardened systemd unit (also at `deploy/axiom/`)

On the remote host:

```bash
tar xzf fox-kernel-remote-<sha>.tar.gz
cd fox-kernel-remote
sudo ./install.sh            # or: PREFIX=~/fox-kernel ./install.sh (no sudo: skips systemd/ufw)
```

`install.sh` prints the token once (generated on the host, never transmitted).
Paste it into the workbench **Remote tab** → **Discover** → offload with
**require GPU**. Full tunnel procedure: `docs/REMOTE-WORKBENCH.md`.
