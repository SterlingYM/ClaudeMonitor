#!/usr/bin/env python3
"""Start the ClaudeMonitor daemon (if not already running) and open the UI.

    claudemonitor            # native pywebview window (default)
    claudemonitor --inline   # terminal TUI via textual
"""

import argparse
import atexit
import os
import subprocess
import sys
import time
import urllib.request

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


def _ensure_server() -> None:
    """Start server.py in the background if not already running."""
    if server_is_up():
        return
    proc = subprocess.Popen(
        [sys.executable, SERVER_PY],
        cwd=HERE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    atexit.register(lambda: proc.poll() is None and proc.terminate())
    if not wait_for_server():
        print("[launcher] server.py failed to start within 10s", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="ClaudeMonitor launcher")
    parser.add_argument(
        "--inline", action="store_true",
        help="Run in terminal TUI mode instead of opening a native window",
    )
    parser.add_argument(
        "--url", default=URL,
        help=f"Server URL (default: {URL})",
    )
    args = parser.parse_args()

    _ensure_server()

    if args.inline:
        from tui import main as tui_main
        tui_main(url=args.url)
    else:
        import webview
        webview.create_window("ClaudeMonitor", args.url, width=820, height=700)
        webview.start()


if __name__ == "__main__":
    main()
