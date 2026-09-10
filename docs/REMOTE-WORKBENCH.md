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

## Troubleshooting

| Symptom | Cause → fix |
|---|---|
| Discover: `timed out` | Tailscale down or firewall: `tailscale status` both ends; `sudo ufw status` on axiom |
| Discover: `not a fox-kernel` on :8000 | That is Hive Trade Util (`{"name":"Hive Trade Util"}`) — point at `:8891` |
| `POST /run` → 403 | Token mismatch: regenerate on axiom (§3), re-enter in GUI |
| `SSH_ERROR: Errno 24 Too many open files` | Some other poller opening SSH per tick — kill it; this Remote tab has no background timers and never uses SSH |
| Token leaked | Regenerate (§3), update GUI + `/etc/fox-kernel.env`, `systemctl restart fox-kernel` |
