#!/usr/bin/env python3
"""ClaudeMonitor TUI — terminal dashboard for monitoring Claude Code sessions.

Connects to a running ClaudeMonitor server via SSE and displays live session
state.  Supports permission approval/denial from the terminal.

Usage:
    claudemonitor --inline [--url http://127.0.0.1:7891]
"""

import argparse
import json
import threading
import time
import urllib.request
from datetime import datetime, timezone

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Label, Static

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STATUS_ICONS = {
    "starting": "⟳",
    "running": "●",
    "idle": "●",
    "waiting_permission": "⚠",
    "completed": "✓",
    "dead": "✗",
}

STATUS_STYLES = {
    "starting": "dim",
    "running": "bold blue",
    "idle": "green",
    "waiting_permission": "bold yellow",
    "completed": "dim",
    "dead": "red",
}


def short_id(sid: str) -> str:
    return (sid or "?")[:8]


def split_cwd(cwd: str) -> str:
    parts = (cwd or "?").rstrip("/").split("/")
    return parts[-1] if parts else cwd


def time_ago(iso: str) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        sec = int((datetime.now(timezone.utc) - dt).total_seconds())
        if sec < 5:
            return "just now"
        if sec < 60:
            return f"{sec}s ago"
        m = sec // 60
        return f"{m}m ago" if m < 60 else f"{m // 60}h ago"
    except ValueError:
        return "—"


def project_status(sessions: list[dict]) -> str:
    for st in ("waiting_permission", "running", "starting", "idle"):
        if any(s["status"] == st for s in sessions):
            return st
    return "dead"


def group_by_cwd(sessions: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for s in sessions:
        groups.setdefault(s.get("cwd") or "?", []).append(s)
    return groups


# ---------------------------------------------------------------------------
# SSE client
# ---------------------------------------------------------------------------

class SSEClient:
    def __init__(self, url: str, on_update):
        self._url = url + "/stream"
        self._on_update = on_update
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                resp = urllib.request.urlopen(self._url, timeout=30)
                backoff = 1.0
                buf = ""
                while not self._stop.is_set():
                    ch = resp.read(1).decode("utf-8", errors="replace")
                    if not ch:
                        break
                    buf += ch
                    if buf.endswith("\n\n"):
                        self._parse(buf)
                        buf = ""
            except Exception:
                if self._stop.is_set():
                    return
                time.sleep(backoff)
                backoff = min(backoff * 2, 15.0)

    def _parse(self, block: str):
        etype, data = "", []
        for line in block.strip().split("\n"):
            if line.startswith("event:"):
                etype = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if etype == "update" and data:
            try:
                self._on_update(json.loads("".join(data)))
            except json.JSONDecodeError:
                pass


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class ClaudeMonitorTUI(App):
    TITLE = "ClaudeMonitor"
    CSS = """
    #main-layout {
        height: 1fr;
    }
    #left-panel {
        width: 36;
        border-right: solid $surface-lighten-2;
    }
    #logo {
        height: 3;
        padding: 0 1;
        background: $surface-darken-2;
    }
    #project-label {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        background: $surface-darken-1;
    }
    #project-list {
        height: 1fr;
        padding: 0;
        overflow-y: auto;
    }
    #right-panel {
        width: 1fr;
    }
    #session-label {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        background: $surface-darken-1;
    }
    #session-bar {
        height: 1;
        background: $surface-darken-2;
        color: $text-muted;
        padding: 0 1;
    }
    #meta-bar {
        height: 1;
        padding: 0 1;
    }
    #perm-box {
        height: auto;
        max-height: 12;
        padding: 1 2;
        border: tall $warning;
        margin: 0 1;
    }
    #perm-box.hidden {
        display: none;
    }
    #event-log {
        height: 1fr;
        margin: 0 1;
    }
    #empty-msg {
        content-align: center middle;
        height: 1fr;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("a", "approve", "Approve", show=True),
        Binding("d", "deny", "Deny", show=True),
        Binding("x", "dismiss", "Dismiss", show=True),
        Binding("left", "prev_session", "", show=True, priority=True),
        Binding("right", "next_session", "Select Session", show=True, priority=True),
        Binding("up", "prev_project", "", show=True, priority=True),
        Binding("down", "next_project", "Select Project", show=True, priority=True),
        Binding("shift+up", "reorder_project_up", "", show=True),
        Binding("shift+down", "reorder_project_down", "Reorder Project", show=True),
        Binding("shift+left", "reorder_session_left", "", show=True),
        Binding("shift+right", "reorder_session_right", "Reorder Session", show=True),
        Binding("h", "scroll_log_left", "", show=True, priority=True),
        Binding("j", "scroll_log_down", "", show=True, priority=True),
        Binding("k", "scroll_log_up", "", show=True, priority=True),
        Binding("l", "scroll_log_right", "hjkl Log Scroll", show=True, priority=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(self, server_url: str = "http://127.0.0.1:7891"):
        super().__init__()
        self._server_url = server_url
        self._sessions: list[dict] = []
        self._project_order: list[str] = []
        self._selected_cwd: str | None = None
        self._selected_sid: str | None = None
        self._dismissed: set[str] = set()
        self._sse: SSEClient | None = None
        self._session_orders: dict[str, list[str]] = {}  # cwd → ordered session IDs

    def compose(self) -> ComposeResult:
        yield Static("", id="logo")
        with Horizontal(id="main-layout"):
            with Vertical(id="left-panel"):
                yield Static(
                    "[dim]Projects[/]  [bold cyan]↑ ↓[/] [dim]cycle[/]",
                    id="project-label",
                )
                yield Static("", id="project-list")
            with Vertical(id="right-panel"):
                yield Static(
                    "[dim]Sessions[/]  [bold cyan]← →[/] [dim]cycle[/]  [bold cyan]hjkl[/] [dim]scroll log[/]",
                    id="session-label",
                )
                yield Static("", id="session-bar")
                yield Static("", id="meta-bar")
                yield Static("", id="perm-box", classes="hidden")
                yield DataTable(id="event-log")
        yield Label(
            "No active Claude sessions.\n"
            "Start a Claude Code session and it will appear here.",
            id="empty-msg",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._update_logo()
        table = self.query_one("#event-log", DataTable)
        table.add_columns("Time", "Event", "Detail")
        table.cursor_type = "none"
        self._sse = SSEClient(self._server_url, self._on_sse)
        self._sse.start()

    def on_resize(self) -> None:
        self._update_logo()

    def _update_logo(self) -> None:
        w = self.size.width - 2  # account for padding
        title_line = "[bold #a78bfa]  │[/] [bold #a78bfa]M[/]   [bold white]Claude Monitor[/] [dim]v0.1.0[/]"
        # "Claude Monitor v0.1.0" starts at col 6, version ends around col 28
        # Right-align "by YSM" so it ends at same column as "v0.1.0"
        prefix = "[bold #a78bfa]  ╰──[/]"
        tag = "[dim]by[/] [#6b6b80]YSM[/]"
        # Plain length of line 2 content up to end of version: "  │ M   Claude Monitor v0.1.0"
        version_end = 29
        # Plain length of "  ╰──" = 5, plain length of "by YSM" = 6
        pad = version_end - 5 - 6
        bottom_line = prefix + " " * max(pad, 1) + tag
        self.query_one("#logo").update(
            f"[bold #a78bfa]  ╭──[/]\n{title_line}\n{bottom_line}"
        )

    def on_unmount(self) -> None:
        if self._sse:
            self._sse.stop()

    def _on_sse(self, payload: dict) -> None:
        if payload.get("type") == "state":
            self.call_from_thread(self._apply_state, payload["sessions"])

    # ── state ──

    def _apply_state(self, incoming: list[dict]) -> None:
        self._sessions = [s for s in incoming if s["id"] not in self._dismissed]
        groups = group_by_cwd(self._sessions)

        live = set(groups.keys())
        self._project_order = [c for c in self._project_order if c in live]
        for c in groups:
            if c not in self._project_order:
                self._project_order.append(c)

        if not self._selected_cwd or self._selected_cwd not in groups:
            self._selected_cwd = self._project_order[0] if self._project_order else None
            self._selected_sid = None

        if self._selected_cwd:
            group = groups.get(self._selected_cwd, [])
            if self._selected_sid and not any(s["id"] == self._selected_sid for s in group):
                self._selected_sid = None
            if not self._selected_sid and group:
                w = next((s for s in group if s["status"] == "waiting_permission"), None)
                self._selected_sid = (w or group[0])["id"]

        self._render()

    # ── rendering ──

    def _render(self) -> None:
        groups = group_by_cwd(self._sessions)
        has = bool(self._sessions)

        self.query_one("#empty-msg").display = not has
        self.query_one("#main-layout").display = has
        if not has:
            self.query_one("#perm-box").add_class("hidden")
            return

        self._render_project_list(groups)
        self._render_session_bar(groups)
        self._render_panel(groups)

    def _render_project_list(self, groups: dict) -> None:
        # Build entries and calculate panel width from info lines
        entries = []
        max_info_w = 20  # minimum
        for cwd in self._project_order:
            if cwd not in groups:
                continue
            group = groups[cwd]
            name = split_cwd(cwd)
            st = project_status(group)
            icon = STATUS_ICONS.get(st, "?")
            style = STATUS_STYLES.get(st, "")
            n = len([s for s in group if not s.get("is_subagent")])
            last_ms = max((s.get("last_event_time") or "") for s in group)
            ago = time_ago(last_ms) if last_ms else "—"
            active = cwd == self._selected_cwd

            info = f"{n} agents · {ago} · {icon} {st.replace('_', ' ')}"
            max_info_w = max(max_info_w, len(info) + 2)  # +2 for padding
            entries.append((cwd, name, info, style, active))

        # Clamp panel width
        pw = min(max(max_info_w, 28), 48)
        inner = pw - 2  # usable chars after padding

        # Update left panel width
        try:
            self.query_one("#left-panel").styles.width = pw + 1  # +1 for border
        except Exception:
            pass

        # Render each project entry: separator + 2 lines
        lines: list[str] = []
        for i, (cwd, name, info, style, active) in enumerate(entries):
            lines.append(f"[dim]{'─' * pw}[/]")
            display_name = name if len(name) <= inner else name[: inner - 1] + "…"
            l1 = f" {display_name} ".ljust(pw)
            l2 = f" {info} ".ljust(pw)

            if active:
                lines.append(f"[bold on #3a3a48]{l1}[/]")
                lines.append(f"[{style} on #3a3a48]{l2}[/]")
            else:
                lines.append(f"[bold]{l1}[/]")
                lines.append(f"[{style}]{l2}[/]")


        self.query_one("#project-list").update("\n".join(lines) if lines else "")

    def _ordered_sessions(self, cwd: str, group: list[dict]) -> list[dict]:
        """Return sessions in user-defined order, appending new ones."""
        order = self._session_orders.get(cwd, [])
        live_ids = {s["id"] for s in group}
        # Remove gone sessions, append new ones
        order = [sid for sid in order if sid in live_ids]
        for s in group:
            if s["id"] not in order:
                order.append(s["id"])
        self._session_orders[cwd] = order
        by_id = {s["id"]: s for s in group}
        return [by_id[sid] for sid in order if sid in by_id]

    def _render_session_bar(self, groups: dict) -> None:
        group = groups.get(self._selected_cwd, [])
        sorted_s = self._ordered_sessions(self._selected_cwd, group)
        parts = []
        for s in sorted_s:
            icon = STATUS_ICONS.get(s["status"], "?")
            style = STATUS_STYLES.get(s["status"], "")
            sub = " sub" if s.get("is_subagent") else ""
            active = s["id"] == self._selected_sid
            tab = f"[{style}]{icon}[/] {short_id(s['id'])}{sub}"
            if active:
                tab = f"[reverse] {tab} [/]"
            else:
                tab = f" {tab} "
            parts.append(tab)
        self.query_one("#session-bar").update("│".join(parts) if parts else "")

    def _render_panel(self, groups: dict) -> None:
        group = groups.get(self._selected_cwd, [])
        sess = next((s for s in group if s["id"] == self._selected_sid), None)
        if not sess:
            self.query_one("#meta-bar").update("")
            self.query_one("#perm-box").add_class("hidden")
            self.query_one("#event-log", DataTable).clear()
            return

        # meta
        st = sess["status"]
        icon = STATUS_ICONS.get(st, "?")
        style = STATUS_STYLES.get(st, "")
        tool = sess.get("last_tool") or "—"
        sid = sess["id"]
        meta = f"[{style}]{icon} {st.replace('_', ' ')}[/] │ tool: {tool} │ [dim]{sid}[/]"
        if sess.get("is_subagent") and sess.get("parent_session_id"):
            meta += f" │ parent: [blue]{short_id(sess['parent_session_id'])}[/]"
        self.query_one("#meta-bar").update(meta)

        # permission box
        perm_box = self.query_one("#perm-box")
        pr = sess.get("permission_request")
        if pr and st == "waiting_permission":
            tool_name = pr.get("tool_name", "?")
            inp = pr.get("tool_input", {})
            inp_str = json.dumps(inp, indent=2) if isinstance(inp, dict) else str(inp)
            if len(inp_str) > 500:
                inp_str = inp_str[:500] + "\n…"
            perm_box.update(
                f"[bold yellow]⚠ Permission Request[/]  Tool: [bold]{tool_name}[/]\n"
                f"[dim]{inp_str}[/]\n"
                f"[reverse] a [/] approve  [reverse] d [/] deny"
            )
            perm_box.remove_class("hidden")
        else:
            perm_box.add_class("hidden")

        # event log
        table = self.query_one("#event-log", DataTable)
        table.clear()
        for e in sess.get("events", []):
            t = ""
            if e.get("time"):
                try:
                    t = datetime.fromisoformat(e["time"].replace("Z", "+00:00")).strftime("%H:%M:%S")
                except ValueError:
                    pass
            ev = e.get("event", "?")
            detail = " · ".join(
                p for p in [
                    e.get("tool") or "",
                    (e.get("extra") or {}).get("notification_type", ""),
                    (e.get("extra") or {}).get("message", ""),
                ] if p
            )
            table.add_row(t, ev, detail)
        if table.row_count > 0:
            table.move_cursor(row=table.row_count - 1)

    # ── navigation ──

    def _cycle_project(self, d: int) -> None:
        if not self._project_order or not self._selected_cwd:
            return
        idx = self._project_order.index(self._selected_cwd)
        self._selected_cwd = self._project_order[(idx + d) % len(self._project_order)]
        self._selected_sid = None
        self._apply_state(self._sessions)

    def _cycle_session(self, d: int) -> None:
        groups = group_by_cwd(self._sessions)
        group = groups.get(self._selected_cwd, [])
        if not group:
            return
        ids = [s["id"] for s in group]
        idx = ids.index(self._selected_sid) if self._selected_sid in ids else -1
        self._selected_sid = ids[(idx + d) % len(ids)]
        self._render()

    def action_prev_project(self) -> None:
        self._cycle_project(-1)

    def action_next_project(self) -> None:
        self._cycle_project(1)

    def action_prev_session(self) -> None:
        self._cycle_session(-1)

    def action_next_session(self) -> None:
        self._cycle_session(1)

    def action_reorder_project_up(self) -> None:
        self._reorder_project(-1)

    def action_reorder_project_down(self) -> None:
        self._reorder_project(1)

    def _reorder_project(self, d: int) -> None:
        if not self._selected_cwd or len(self._project_order) < 2:
            return
        idx = self._project_order.index(self._selected_cwd)
        new_idx = idx + d
        if new_idx < 0 or new_idx >= len(self._project_order):
            return
        self._project_order[idx], self._project_order[new_idx] = (
            self._project_order[new_idx], self._project_order[idx]
        )
        self._render()

    def action_reorder_session_left(self) -> None:
        self._reorder_session(-1)

    def action_reorder_session_right(self) -> None:
        self._reorder_session(1)

    def _reorder_session(self, d: int) -> None:
        if not self._selected_cwd or not self._selected_sid:
            return
        order = self._session_orders.get(self._selected_cwd, [])
        if self._selected_sid not in order or len(order) < 2:
            return
        idx = order.index(self._selected_sid)
        new_idx = idx + d
        if new_idx < 0 or new_idx >= len(order):
            return
        order[idx], order[new_idx] = order[new_idx], order[idx]
        self._render()

    def action_scroll_log_left(self) -> None:
        table = self.query_one("#event-log", DataTable)
        table.scroll_relative(x=-5)

    def action_scroll_log_right(self) -> None:
        table = self.query_one("#event-log", DataTable)
        table.scroll_relative(x=5)

    def action_scroll_log_up(self) -> None:
        table = self.query_one("#event-log", DataTable)
        table.scroll_relative(y=-3)

    def action_scroll_log_down(self) -> None:
        table = self.query_one("#event-log", DataTable)
        table.scroll_relative(y=3)

    def action_approve(self) -> None:
        self._try_relay("approve")

    def action_deny(self) -> None:
        self._try_relay("deny")

    def action_dismiss(self) -> None:
        if self._selected_sid:
            self._dismissed.add(self._selected_sid)
            self._do_dismiss(self._selected_sid)
            self._apply_state(self._sessions)

    def _try_relay(self, decision: str) -> None:
        if not self._selected_sid:
            return
        sess = next((s for s in self._sessions if s["id"] == self._selected_sid), None)
        if sess and sess["status"] == "waiting_permission":
            self._do_relay(self._selected_sid, decision)

    @work(thread=True)
    def _do_relay(self, sid: str, decision: str) -> None:
        try:
            req = urllib.request.Request(
                f"{self._server_url}/sessions/{sid}/{decision}", method="POST", data=b""
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            self.call_from_thread(self.notify, f"Relay failed: {e}", severity="error")

    @work(thread=True)
    def _do_dismiss(self, sid: str) -> None:
        try:
            req = urllib.request.Request(
                f"{self._server_url}/sessions/{sid}/dismiss", method="POST", data=b""
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(url: str = "http://127.0.0.1:7891") -> None:
    ClaudeMonitorTUI(server_url=url).run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ClaudeMonitor TUI")
    parser.add_argument("--url", default="http://127.0.0.1:7891")
    args = parser.parse_args()
    main(url=args.url)
