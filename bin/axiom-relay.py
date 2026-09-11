#!/usr/bin/env python3
"""axiom-relay.py — Mac-host TCP relay: workbench container -> LAN GPU box.

Why this exists: Docker Desktop for Mac blocks container->LAN egress
(192.168.1.x times out from inside containers), while host.docker.internal
works. So the workbench talks to this relay on the Mac (via
http://host.docker.internal:8892) and the relay forwards to axiom:8891.
Stdlib only, no passwords, no SSH — plain TCP forward to the token-guarded
fox-kernel (Bearer auth happens end-to-end between workbench and agent).

Usage:
    python3 bin/axiom-relay.py [--listen-port 8892] [--target-host 192.168.1.173] [--target-port 8891]
    # persist across reboots: see deploy/remote-agent/axiom-relay.plist (LaunchAgent)
"""
from __future__ import annotations

import argparse
import logging
import select
import socket
import threading
import time

BUF = 65536
# Generous: the relay is a dumb pipe — endpoints enforce their own timeouts
# (proxy ≤600s, kernel per-request). Must exceed the longest supported run or
# long silent executions get severed mid-flight (observed as ~60s deaths).
SOCK_TIMEOUT = 600.0
IDLE_TIMEOUT = 660.0

log = logging.getLogger("axiom-relay")


def _pump(src: socket.socket, dst: socket.socket, deadline: float) -> None:
    try:
        while time.time() < deadline:
            r, _, _ = select.select([src], [], [], 10.0)
            if not r:
                continue
            chunk = src.recv(BUF)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _handle(client: socket.socket, target: tuple[str, int]) -> None:
    upstairs = socket.create_connection(target, timeout=10.0)
    upstairs.settimeout(SOCK_TIMEOUT)
    client.settimeout(SOCK_TIMEOUT)
    deadline = time.time() + IDLE_TIMEOUT
    t = threading.Thread(target=_pump, args=(upstairs, client, deadline), daemon=True)
    t.start()
    _pump(client, upstairs, deadline)
    t.join(timeout=5.0)
    upstairs.close()
    client.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Relay Mac localhost -> axiom fox-kernel.")
    ap.add_argument("--listen-host", default="127.0.0.1")
    ap.add_argument("--listen-port", type=int, default=8892)
    ap.add_argument("--target-host", default="192.168.1.173")
    ap.add_argument("--target-port", type=int, default=8891)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.listen_host, args.listen_port))
    srv.listen(32)
    log.info("listening %s:%d -> %s:%d", args.listen_host, args.listen_port,
             args.target_host, args.target_port)
    target = (args.target_host, args.target_port)
    while True:
        client, addr = srv.accept()
        log.info("conn from %s", addr[0])
        threading.Thread(target=_handle, args=(client, target), daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
