#!/usr/bin/env python3
"""Start the ClaudeMonitor daemon (if not already running) and open a native window."""

import atexit
import os
import subprocess
import sys
import time
import urllib.request

import webview

HERE      = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(HERE, "server.py")
URL       = "http://127.0.0.1:7891"
HEALTH    = URL + "/sessions"


def server_is_up() -> bool:
    try:
        urllib.request.urlopen(HEALTH, timeout=0.4)
        return True
    except Exception:
        return False


def wait_for_server(timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if server_is_up():
            return True
        time.sleep(0.15)
    return False


def main() -> None:
    server_proc = None
    if not server_is_up():
        server_proc = subprocess.Popen(
            [sys.executable, SERVER_PY],
            cwd=HERE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        atexit.register(
            lambda: server_proc and server_proc.poll() is None and server_proc.terminate()
        )
        if not wait_for_server():
            print("[launcher] server.py failed to start within 10s", file=sys.stderr)
            sys.exit(1)

    webview.create_window("ClaudeMonitor", URL, width=1100, height=750)
    webview.start()


if __name__ == "__main__":
    main()
