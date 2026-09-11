# Remote workbench tunnel — Mac workbench ↔ axiom (no passwords exchanged)

Goal: the workbench (Mac, Docker) offloads experiments to axiom (2× RTX5080)
over an encrypted tunnel, with GPU discovery and reporting back. Nothing here
uses password authentication between the machines.

Recommended path: **Tailscale** (WireGuard, NAT traversal, MagicDNS name
`axiom`, tailnet ACLs). Plain LAN (`http://192.168.1.173:8891`) works when both
are home, but Tailscale also works away and encrypts the bearer token in
transit. The workbench itself never opens SSH sessions (that per-poll SSH
pattern is what causes `Errno 24 Too many open files`).

## 0. One-time access you already have

Connect to axiom however you do today, then make the rest passwordless.
After this guide, disable SSH password logins on axiom.

## 1. Tailscale on both machines

Mac (already 1.98.10 here — skip if present):

```bash
tailscale version   # want >= 1.60
sudo tailscale up   # browser SSO login, once
tailscale status    # axiom should appear as soon as it joins
```

Axiom (Ubuntu):

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up   # same tailnet account as the Mac
tailscale ip -4     # e.g. 100.x.y.z
tailscale status    # Mac should appear
```

From the Mac, verify the tunnel (no password involved — tailnet identity only):

```bash
tailscale ping axiom          # MagicDNS name; falls back to 100.x.y.z
curl -s http://axiom:8891/health   # 404/refused until step 3 — that is fine
```

## 2. Passwordless shell (pick one, then disable passwords)

Preferred — **Tailscale SSH** (zero keys, zero passwords, tailnet identity):

```bash
# on axiom, once:
sudo tailscale up --ssh
# from the Mac, no password prompt, ever:
tailscale ssh fox@axiom
```

Fallback — **key-only OpenSSH**, then turn passwords off:

```bash
# on the Mac (new key, no passphrase needed for automation):
ssh-keygen -t ed25519 -f ~/.ssh/axiom_ed25519 -N ""
ssh-copy-id -i ~/.ssh/axiom_ed25519 fox@axiom   # last password use
# on axiom, disable password logins:
sudo sed -i 's/^#*PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl reload ssh
ssh -i ~/.ssh/axiom_ed25519 fox@axiom   # must work with no prompt
```

## 3. Token without exchanging passwords

Generate the API token **on axiom** (never in chat, git, or logs):

```bash
# on axiom:
openssl rand -hex 32
echo 'REMOTE_TOKEN=<paste>' | sudo tee /etc/fox-kernel.env
sudo chmod 600 /etc/fox-kernel.env
```

Enter the same token once in the workbench **Remote tab** (password field).
It is stored in local `config.json` only, redacted as `***REDACTED***` in every
API response, and never logged. Rotate with `openssl rand -hex 32` any time.

## 4. Firewall (axiom)

With Tailscale, expose the agent to the tailnet only — no LAN port needed:

```bash
sudo ufw allow in on tailscale0 to any port 8891 proto tcp
# only if you also want plain-LAN fallback on trusted home wifi:
# sudo ufw allow from 192.168.1.0/24 to any port 8891 proto tcp
```

## 5. Run the agent on axiom (persistent)

Preferred — deployable package (52K, no full clone, closed dep set):

```bash
# on the Mac: build once per release
./bin/build-remote-agent.sh   # -> dist/fox-kernel-remote-<sha>.tar.gz
# copy ONE file to axiom (scp / tailscale file / usb), then on axiom:
tar xzf fox-kernel-remote-<sha>.tar.gz && cd fox-kernel-remote
sudo ./install.sh             # venv + deps + token + systemd + smoke test
# user-local alternative (no sudo): PREFIX=~/fox-kernel ./install.sh
journalctl -u fox-kernel -f   # watch startup (systemd installs only)
```

Docker — for GPU servers that are docker-first (needs NVIDIA Container Toolkit
on the host for GPU visibility; without it the agent runs CPU-only):

```bash
# on axiom, from the unpacked tarball (or the repo's deploy/remote-agent/):
cd fox-kernel-remote
REMOTE_TOKEN=<token> docker compose up -d --build
# CPU-only hosts / Mac test:
# REMOTE_TOKEN=<token> docker compose -f docker-compose.yml -f docker-compose.cpu.yml up -d --build
# CUDA variant — torch + numpy baked in so GPU experiments survive recreates
# (the base image is slim; pip-installed torch is ephemeral):
# REMOTE_TOKEN=<token> docker compose -f docker-compose.yml -f docker-compose.cuda.yml up -d --build
# (.cpu and .cuda overlays are mutually exclusive.)
docker inspect fox-kernel --format '{{.State.Health.Status}}'  # healthy
```

Fallback — full clone (heavier, same result):

```bash
# on axiom:
git clone <this-repo> ~/AI-Research-Workbench   # or sync it
cd ~/AI-Research-Workbench && python3 -m venv .venv && .venv/bin/pip install -e .
mkdir -p ~/axiom-workspace
sudo cp deploy/axiom/fox-kernel.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now fox-kernel
journalctl -u fox-kernel -f   # watch startup
```

Verify from the Mac (through the tunnel, no SSH):

```bash
curl -s http://axiom:8891/health
curl -s http://axiom:8891/api/kernel/gpu   # expect 2× RTX5080 devices
```

Then in the workbench **Remote tab**: add host `axiom` → `http://axiom:8891`
(+ token) → **Discover** (expect ✓ fox-kernel + 2 GPUs) → offload with
**require GPU** checked. Results return as `kind="remote"` runs.

## 6. Mac relay (Docker Desktop containers cannot reach LAN)

Docker Desktop for Mac blocks container → LAN egress (`192.168.x.x` times out
from inside containers, while `host.docker.internal` works). The workbench
therefore cannot call axiom directly — run this stdlib-only TCP relay **on the
Mac host** (no passwords, no SSH; Bearer auth stays end-to-end):

```bash
python3 bin/axiom-relay.py   # 127.0.0.1:8892 -> axiom:8891, logs to stderr
# persist across reboots:
cp deploy/remote-agent/axiom-relay.plist ~/Library/LaunchAgents/com.fox.axiom-relay.plist
# (edit the checkout path inside first)
launchctl load ~/Library/LaunchAgents/com.fox.axiom-relay.plist
```

Then register `http://host.docker.internal:8892` (not the LAN IP) in the
Remote tab. Verified live: Discover → ✓ fox-kernel, 14ms, 2× RTX5080; GPU
offload (`nvidia-smi` via execute) recorded as a `kind="remote"` run.

## Troubleshooting

| Symptom | Cause → fix |
|---|---|
| Discover: `timed out` | Tailscale down or firewall: `tailscale status` both ends; `sudo ufw status` on axiom |
| Discover: `not a fox-kernel` on :8000 | That is Hive Trade Util (`{"name":"Hive Trade Util"}`) — point at `:8891` |
| `POST /run` → 403 | Token mismatch: regenerate on axiom (§3), re-enter in GUI |
| `SSH_ERROR: Errno 24 Too many open files` | Some other poller opening SSH per tick — kill it; this Remote tab has no background timers and never uses SSH |
| Token leaked | Regenerate (§3), update GUI + `/etc/fox-kernel.env`, `systemctl restart fox-kernel` |
