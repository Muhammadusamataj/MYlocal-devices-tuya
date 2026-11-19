"""Entry point called from OfflineActivity via Chaquopy.

Runs the project's Flask app with waitress entirely on the device. All writable
state (session, encrypted caches, preferences) lives in the app's private files
directory, which Java passes in as `data_dir`.
"""

from __future__ import annotations

import os
import sys
import threading

_LOCK = threading.Lock()
_STARTED = False


def start(data_dir: str, host: str = "127.0.0.1", port: int = 8000,
          network_hint: str = "", tailscale_ip: str = "") -> None:
    global _STARTED
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True

    os.makedirs(data_dir, exist_ok=True)
    os.environ["SESSION_FILE"] = os.path.join(data_dir, "session.json")
    os.environ["PORT"] = str(port)
    os.environ["SERVER_HOST"] = str(host)
    if network_hint:
        os.environ["TUYA_NETWORK_HINT"] = str(network_hint).strip()
    if tailscale_ip:
        os.environ["TAILSCALE_IP"] = str(tailscale_ip).strip()

    # Chaquopy adds its source directory to sys.path; keep this robust.
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)

    from app import app, auto_setup_if_needed
    from waitress import serve

    # If a previous login left devices cached without LAN IPs, finish the setup
    # in the background. A fresh login starts its own auto-setup in app.py.
    auto_setup_if_needed()
    serve(app, host=host, port=int(port), threads=8)
