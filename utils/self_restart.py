"""Shared helper so both luna.py and irc_bridge.py can dispatch a successor
run via the GitHub Actions workflow API before this process exits.

Why in its own module: luna.py and irc_bridge.py have a circular dependency
risk (bridge is imported BY luna), so neither can cleanly import from the
other at module scope. A tiny utility module they both import is the clean
seam.

Honours GH_PAT (classic PAT with workflow scope); without it, logs and
no-ops so the cron is the backstop (same semantics as before Phase 1).
Blocking urllib call — this is called just before the process exits, so
not using asyncio keeps it callable from any thread or synchronous path.
"""
from __future__ import annotations

import json as _json
import os as _os
import urllib.error as _urllib_error
import urllib.request as _urllib_request


def dispatch_successor(reason: str, workflow_file: str = "luna.yml") -> None:
    """Trigger a fresh workflow_dispatch run so the room is not left bot-less
    while GitHub's throttled cron catches up. Called from signal handler,
    asyncio-wrapper crash path, and IRC-bridge blocked-address exit.
    """
    token = _os.getenv("GH_PAT", "").strip()
    if not token:
        print(f"[self-restart] no GH_PAT set — cron is the backstop ({reason})", flush=True)
        return
    repo = _os.getenv("GITHUB_REPOSITORY", "batcaveirc/batcave-luna")
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/dispatches"
    body = _json.dumps({"ref": "main"}).encode("utf-8")
    req = _urllib_request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    })
    try:
        with _urllib_request.urlopen(req, timeout=5) as res:
            print(f"[self-restart] successor dispatched ({reason}, HTTP {res.status})", flush=True)
    except _urllib_error.HTTPError as e:
        try:
            body_s = e.read()[:120]
        except Exception:
            body_s = b""
        print(f"[self-restart] HTTP {e.code} — {body_s!r} ({reason})", flush=True)
    except Exception as e:  # noqa: BLE001 — must never block exit
        print(f"[self-restart] {e} ({reason})", flush=True)
