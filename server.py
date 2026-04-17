#!/usr/bin/env python3
"""
ClaudeMonitor server.py — stateful daemon for monitoring Claude Code sessions.

Usage:
    python3 server.py [--port 7891]

Endpoints:
  POST /events                 — hook events from Claude Code
  GET  /stream                 — SSE stream of full session state
  GET  /sessions               — JSON snapshot of all sessions
  POST /sessions/<id>/approve  — approve a pending permission request
  POST /sessions/<id>/deny     — deny a pending permission request
  GET  /                       — serves index.html when available (Phase 3)
"""

import argparse
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_HERE        = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE  = os.path.join(_HERE, "config.json")
STATE_FILE   = os.path.expanduser("~/.claude/monitor-state.json")

def _load_config() -> dict:
    defaults: dict = {
        "port":                   7891,
        "dead_after_minutes":     30,
        "perm_timeout_seconds":   85,
    }
    try:
        with open(CONFIG_FILE) as f:
            defaults.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[config] warning: {e}", file=sys.stderr)
    return defaults

_cfg         = _load_config()
PORT         = _cfg["port"]
MAX_EVENTS   = 50
DEAD_AFTER   = int(_cfg["dead_after_minutes"])  * 60
PERM_TIMEOUT = int(_cfg["perm_timeout_seconds"]) if _cfg["perm_timeout_seconds"] is not None else None

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()

_sse_queues: list[queue.Queue] = []
_sse_lock = threading.Lock()

# session_id -> {"event": threading.Event, "decision": str | None}
_pending_perms: dict[str, dict] = {}
_perm_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _broadcast(payload: dict) -> None:
    """Push a JSON payload to every connected SSE client."""
    data = json.dumps(payload)
    with _sse_lock:
        dead = []
        for q in _sse_queues:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_queues.remove(q)


def _snapshot() -> list[dict]:
    """Return a serialisable list of all sessions (copy under lock)."""
    with _sessions_lock:
        return [dict(s) for s in _sessions.values()]


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def _upsert_session(session_id: str, **kwargs) -> dict:
    """Create or update a session entry, then broadcast the change."""
    with _sessions_lock:
        if session_id not in _sessions:
            _sessions[session_id] = {
                "id":               session_id,
                "cwd":              "",
                "status":           "starting",
                "last_event":       None,
                "last_event_time":  None,
                "last_tool":        None,
                "is_subagent":      False,
                "parent_session_id": None,
                "subagent_ids":     [],
                "permission_request": None,
                "events":           [],
            }
        sess = _sessions[session_id]
        for k, v in kwargs.items():
            if k == "events":
                continue  # events are managed via _append_event
            if k == "cwd" and sess.get("cwd"):
                continue  # pin cwd to the SessionStart value
            sess[k] = v
        sess["last_event_time"] = _now_iso()
    _broadcast({"type": "state", "sessions": _snapshot()})
    return _sessions[session_id]


def _append_event(session_id: str, entry: dict) -> None:
    """Append to a session's event log, capped at MAX_EVENTS."""
    with _sessions_lock:
        sess = _sessions.get(session_id)
        if sess is None:
            return
        sess["events"].append(entry)
        if len(sess["events"]) > MAX_EVENTS:
            sess["events"] = sess["events"][-MAX_EVENTS:]


def _get_cwd(session_id: str, fallback: str = "") -> str:
    """Return the stored cwd for a session, or fallback."""
    with _sessions_lock:
        return _sessions.get(session_id, {}).get("cwd") or fallback


# ---------------------------------------------------------------------------
# Dead-session reaper
# ---------------------------------------------------------------------------

def _reaper_loop() -> None:
    """Background thread: mark sessions dead after prolonged silence."""
    while True:
        time.sleep(60)
        changed = False
        now = time.time()
        with _sessions_lock:
            for sess in _sessions.values():
                if sess["status"] not in ("running", "idle", "starting"):
                    continue
                ts = sess.get("last_event_time")
                if not ts:
                    continue
                try:
                    last = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
                if now - last > DEAD_AFTER:
                    sess["status"] = "dead"
                    changed = True
        if changed:
            _broadcast({"type": "state", "sessions": _snapshot()})


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _save_state() -> None:
    """Write the session registry to STATE_FILE."""
    with _sessions_lock:
        payload = {
            "version":  1,
            "saved_at": _now_iso(),
            "sessions": list(_sessions.values()),
        }
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        print(f"[state] save failed: {e}", file=sys.stderr)


def _load_state() -> None:
    """Restore the session registry from STATE_FILE on startup."""
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except Exception as e:
        print(f"[state] load failed: {e}", file=sys.stderr)
        return

    cutoff = time.time() - 24 * 3600   # discard sessions older than 24 h
    restored = 0
    with _sessions_lock:
        for sess in data.get("sessions", []):
            ts = sess.get("last_event_time")
            if ts:
                try:
                    age = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                    if age < cutoff:
                        continue
                except ValueError:
                    pass
            # Clear any stale permission request — we can't relay it after restart
            if sess.get("status") == "waiting_permission":
                sess["status"] = "idle"
            sess.pop("permission_request", None)
            sess.setdefault("events", [])
            _sessions[sess["id"]] = sess
            restored += 1

    if restored:
        print(f"[state] restored {restored} session(s) from {STATE_FILE}")


def _periodic_save_loop() -> None:
    """Save state to disk every 60 s so crashes lose at most 1 min of history."""
    while True:
        time.sleep(60)
        _save_state()


# ---------------------------------------------------------------------------
# Hook event processing
# ---------------------------------------------------------------------------

def _process_event(event: dict) -> dict | None:
    """
    Process one hook event, update session state.
    Returns a JSON-serialisable response dict for PermissionRequest (decision relay),
    or None for a plain {"ok": true} response.
    """
    hook = event.get("hook_event_name", "unknown")
    sid  = event.get("session_id")
    cwd  = event.get("cwd") or ""
    ts   = _now_iso()

    if not sid:
        return None

    log_entry: dict = {"time": ts, "event": hook, "tool": None, "extra": {}}

    # ── SessionStart ────────────────────────────────────────────────────────
    if hook == "SessionStart":
        _upsert_session(sid, cwd=cwd, status="running", last_event=hook)

    # ── Stop ────────────────────────────────────────────────────────────────
    elif hook == "Stop":
        _upsert_session(sid, status="idle", last_event=hook,
                        permission_request=None)

    # ── SubagentStart ────────────────────────────────────────────────────────
    elif hook == "SubagentStart":
        parent_id = event.get("parent_session_id")
        # Don't overwrite an existing main session as a subagent
        with _sessions_lock:
            existing = _sessions.get(sid)
        if existing and not existing.get("is_subagent"):
            # Session already exists as a main session — just update status
            _upsert_session(sid, status="running", last_event=hook)
        else:
            _upsert_session(sid, cwd=cwd or _get_cwd(parent_id or ""),
                            status="running", last_event=hook,
                            is_subagent=True, parent_session_id=parent_id)
        if parent_id:
            with _sessions_lock:
                if parent_id in _sessions:
                    if sid not in _sessions[parent_id]["subagent_ids"]:
                        _sessions[parent_id]["subagent_ids"].append(sid)
            _broadcast({"type": "state", "sessions": _snapshot()})

    # ── SubagentStop ─────────────────────────────────────────────────────────
    elif hook == "SubagentStop":
        _upsert_session(sid, status="idle", last_event=hook)

    # ── PreToolUse ───────────────────────────────────────────────────────────
    elif hook == "PreToolUse":
        tool = event.get("tool_name") or ""
        log_entry["tool"] = tool
        _upsert_session(sid, status="running", last_event=hook, last_tool=tool,
                        cwd=cwd or _get_cwd(sid))

    # ── PostToolUse ──────────────────────────────────────────────────────────
    elif hook == "PostToolUse":
        tool = event.get("tool_name") or ""
        log_entry["tool"] = tool
        _upsert_session(sid, status="idle", last_event=hook, last_tool=tool)

    # ── Notification ─────────────────────────────────────────────────────────
    elif hook == "Notification":
        notif_type = event.get("notification_type") or ""
        log_entry["extra"]["notification_type"] = notif_type
        if notif_type:
            log_entry["extra"]["message"] = event.get("message") or ""
        # Don't downgrade from waiting_permission — a permission relay is active
        with _sessions_lock:
            curr_status = _sessions.get(sid, {}).get("status")
        upd: dict = {"last_event": hook}
        if curr_status != "waiting_permission":
            upd["status"] = "idle"
        _upsert_session(sid, **upd)

    # ── PermissionRequest ────────────────────────────────────────────────────
    elif hook == "PermissionRequest":
        tool = event.get("tool_name") or ""
        perm_details = {
            "tool_name":   tool,
            "tool_input":  event.get("tool_input") or {},
            "requested_at": ts,
        }
        log_entry["tool"]  = tool
        log_entry["extra"]["permission_request"] = perm_details

        _upsert_session(sid, status="waiting_permission", last_event=hook,
                        last_tool=tool, permission_request=perm_details,
                        cwd=cwd or _get_cwd(sid))
        _append_event(sid, log_entry)

        print(f"[perm] PENDING  {sid[:8]} → {tool}  (waiting for UI…)")

        # Block this handler thread until the UI sends a decision (or timeout)
        ev   = threading.Event()
        slot: dict = {"event": ev, "decision": None}
        with _perm_lock:
            _pending_perms[sid] = slot

        ev.wait(timeout=PERM_TIMEOUT)

        with _perm_lock:
            decision = slot.get("decision")
            _pending_perms.pop(sid, None)

        if decision == "allow":
            print(f"[perm] ALLOWED  {sid[:8]} → {tool}")
            _upsert_session(sid, status="running", permission_request=None)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "allow"},
                }
            }
        elif decision == "deny":
            print(f"[perm] DENIED   {sid[:8]} → {tool}")
            _upsert_session(sid, status="idle", permission_request=None)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {
                        "behavior": "deny",
                        "message": "Denied by user via ClaudeMonitor",
                    },
                }
            }
        elif decision == "external":
            # Resolved via Claude's own dialog before UI could act
            print(f"[perm] EXTERNAL {sid[:8]} → {tool}  (resolved via Claude dialog)")
            _upsert_session(sid, status="idle", last_event="PermissionDenied",
                            permission_request=None)
            return None
        else:
            # Hook-level timeout (PERM_TIMEOUT elapsed or hook_timeout from settings.json)
            limit = f"{PERM_TIMEOUT}s" if PERM_TIMEOUT else "hook timeout"
            print(f"[perm] TIMEOUT  {sid[:8]} → {tool}  ({limit})")
            _upsert_session(sid, status="idle", permission_request=None)
            return None

    # ── PermissionDenied ─────────────────────────────────────────────────────
    elif hook == "PermissionDenied":
        # If a blocking relay thread is still waiting, wake it up so it doesn't leak.
        # Mark it "external" so the relay thread knows not to return a decision.
        with _perm_lock:
            slot = _pending_perms.get(sid)
        if slot and not slot["event"].is_set():
            print(f"[perm] EXT-DENY {sid[:8]}  (Claude dialog resolved first)")
            slot["decision"] = "external"
            slot["event"].set()
            # relay thread will update session state when it unblocks
        else:
            _upsert_session(sid, status="idle", last_event=hook,
                            permission_request=None)

    # ── Unknown / future hooks ───────────────────────────────────────────────
    else:
        _upsert_session(sid, last_event=hook, cwd=cwd or _get_cwd(sid))

    _append_event(sid, log_entry)
    return None


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class MonitorHandler(BaseHTTPRequestHandler):

    # ── POST ─────────────────────────────────────────────────────────────────

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw    = self.rfile.read(length) if length else b""

        if self.path == "/events":
            try:
                event = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                event = {}

            response = _process_event(event)

            body = json.dumps(response).encode() if response else b'{"ok": true}'
            self._send_json(body)

        elif self.path.startswith("/sessions/"):
            parts = self.path.rstrip("/").split("/")
            # /sessions/<id>/approve  or  /sessions/<id>/deny
            if len(parts) == 4 and parts[1] == "sessions" and parts[3] in ("approve", "deny"):
                sid      = parts[2]
                decision = "allow" if parts[3] == "approve" else "deny"
                self._relay_permission(sid, decision)
            elif len(parts) == 4 and parts[1] == "sessions" and parts[3] == "dismiss":
                self._dismiss_session(parts[2])
            else:
                self._not_found()

        else:
            self._not_found()

    def _dismiss_session(self, session_id: str) -> None:
        with _sessions_lock:
            if session_id in _sessions:
                _sessions[session_id]["status"] = "dead"
                _sessions[session_id]["permission_request"] = None
        # Wake any pending permission relay so its thread doesn't leak
        with _perm_lock:
            slot = _pending_perms.get(session_id)
        if slot and not slot["event"].is_set():
            slot["decision"] = "external"
            slot["event"].set()
        _broadcast({"type": "state", "sessions": _snapshot()})
        self._send_json(json.dumps({"ok": True}).encode())

    def _relay_permission(self, session_id: str, decision: str) -> None:
        with _perm_lock:
            slot = _pending_perms.get(session_id)
        if slot:
            slot["decision"] = decision
            slot["event"].set()
            self._send_json(json.dumps({"ok": True, "decision": decision}).encode())
        else:
            self._send_json(
                json.dumps({"ok": False, "reason": "no pending permission for that session"}).encode()
            )

    # ── GET ──────────────────────────────────────────────────────────────────

    def do_GET(self):
        if self.path in ("/stream", "/stream/"):
            self._sse_stream()
        elif self.path in ("/sessions", "/sessions/"):
            self._send_json(json.dumps(_snapshot()).encode())
        elif self.path in ("/", "/index.html"):
            self._serve_index()
        elif self.path.endswith(".png") and "/" not in self.path[1:]:
            self._serve_static(self.path[1:], "image/png")
        else:
            self._not_found()

    def _sse_stream(self):
        self.send_response(200)
        self.send_header("Content-Type",  "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection",    "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        q: queue.Queue = queue.Queue(maxsize=200)
        with _sse_lock:
            _sse_queues.append(q)

        # Send current state immediately on connect
        initial = json.dumps({"type": "state", "sessions": _snapshot()})
        try:
            self.wfile.write(f"event: update\ndata: {initial}\n\n".encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            with _sse_lock:
                if q in _sse_queues:
                    _sse_queues.remove(q)
            return

        try:
            while True:
                try:
                    payload = q.get(timeout=15)
                    self.wfile.write(f"event: update\ndata: {payload}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    # Keepalive ping so the connection doesn't time out
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with _sse_lock:
                if q in _sse_queues:
                    _sse_queues.remove(q)

    def _serve_index(self):
        index_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
        if os.path.exists(index_path):
            with open(index_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type",   "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = (
                b"<html><head><title>ClaudeMonitor</title></head>"
                b"<body><h1>ClaudeMonitor</h1>"
                b"<p>Daemon is running. <code>index.html</code> (Phase 3) not yet built.</p>"
                b"<p><a href='/sessions'>GET /sessions</a> &mdash; JSON snapshot</p>"
                b"</body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type",   "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _serve_static(self, filename: str, content_type: str):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
        if os.path.exists(path):
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(body)
        else:
            self._not_found()

    # ── Shared response helpers ───────────────────────────────────────────────

    def _send_json(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type",   "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        # Suppress default Apache-style access log; keep terminal clean
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="ClaudeMonitor daemon")
    parser.add_argument("--port", type=int, default=PORT,
                        help=f"Port to listen on (default {PORT})")
    args = parser.parse_args()

    # Restore previous session state
    _load_state()

    # Background threads
    threading.Thread(target=_reaper_loop,        daemon=True, name="reaper").start()
    threading.Thread(target=_periodic_save_loop, daemon=True, name="saver").start()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), MonitorHandler)
    server.timeout = 1.0   # allows signal checks between requests

    # Graceful shutdown: save state before exit
    stop = threading.Event()

    def _shutdown(sig, frame):
        stop.set()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    perm_str = f"{PERM_TIMEOUT}s" if PERM_TIMEOUT else "indefinite"
    print(f"ClaudeMonitor  http://127.0.0.1:{args.port}  (perm_timeout={perm_str})")
    print(f"  POST /events                — hook receiver")
    print(f"  GET  /stream                — SSE session feed")
    print(f"  GET  /sessions              — JSON snapshot")
    print(f"  POST /sessions/<id>/approve — relay permission approve")
    print(f"  POST /sessions/<id>/deny    — relay permission deny")
    print(f"  GET  /                      — dashboard")
    print(f"\nPress Ctrl+C to stop.\n")

    while not stop.is_set():
        server.handle_request()

    print("\n[state] saving session state…")
    _save_state()
    print("[state] done. Stopped.")


if __name__ == "__main__":
    main()
