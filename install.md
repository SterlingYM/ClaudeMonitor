# ClaudeMonitor — Install Guide

> **This document is written for a Claude Code agent to read and execute step-by-step.**
> If you are a user: clone this repo, open Claude Code inside the repo directory, and say
> *"Read install.md and set up ClaudeMonitor for me."*
> The agent will walk through each step, asking for confirmation before running anything.

ClaudeMonitor is a local daemon + native macOS window that monitors all running Claude Code sessions on your machine. It uses the Claude Code hook system (HTTP hooks over localhost) to receive session events in real time, and a small pywebview window to display them.

---

## What this install will do

1. Install two Python packages (`pywebview`, `pillow`).
2. Merge seven hook entries into `~/.claude/settings.json` so every future Claude Code session sends events to `http://127.0.0.1:7891`.
3. Generate `icon.icns` for the macOS app bundle.
4. Rewrite `ClaudeMonitor.app/Contents/MacOS/ClaudeMonitor` so it uses the correct absolute paths for this machine.
5. Smoke-test the server, then the app.

None of these steps touch anything outside of: this project directory, `~/.claude/settings.json`, and an optional copy to `/Applications/`.

---

## Step 0 — Gather facts about this machine

Before editing anything, collect these values. Each subsequent step refers back to them.

```bash
# A) The absolute path to the Python interpreter you will install packages into.
#    You MUST use the absolute path in the .app launcher — macOS Finder starts
#    .app bundles with a minimal PATH so `python3` alone will not resolve to
#    the right interpreter.
which python3

# B) Absolute path to this project directory (the directory containing this install.md).
pwd

# C) Verify Python is 3.9 or newer.
python3 --version
```

Record the output of A and B. You will use them verbatim in Step 4.

---

## Step 1 — Install Python dependencies

From the project root (the directory containing `pyproject.toml`), run an editable install against the interpreter from Step 0(A):

```bash
python3 -m pip install -e .
```

This pulls in `pywebview` and `pillow` from `pyproject.toml`. Editable mode keeps the code in place — no wheels, no copying.

Verify:

```bash
python3 -c "import webview, PIL; print('deps OK')"
```

If you get `ModuleNotFoundError`, the user may have multiple Python installs — stop and ask them which one they want to use, then rerun `pip install -e .` with that specific interpreter's `pip`.

---

## Step 2 — Register Claude Code hooks

Read `~/.claude/settings.json`. If the file does not exist, create it as `{}`.

You must **merge** the hook block below into the existing `hooks` key, not replace the whole file. Preserve every other key, and preserve any pre-existing hook entries for events not listed here.

The block to merge:

```json
{
  "hooks": {
    "SessionStart":      [{"hooks": [{"type": "http", "url": "http://127.0.0.1:7891/events", "timeout": 3}]}],
    "Stop":              [{"hooks": [{"type": "http", "url": "http://127.0.0.1:7891/events", "timeout": 3}]}],
    "PermissionRequest": [{"hooks": [{"type": "http", "url": "http://127.0.0.1:7891/events", "timeout": 3600}]}],
    "PermissionDenied":  [{"hooks": [{"type": "http", "url": "http://127.0.0.1:7891/events", "timeout": 3}]}],
    "Notification":      [{"hooks": [{"type": "http", "url": "http://127.0.0.1:7891/events", "timeout": 3}]}],
    "SubagentStart":     [{"hooks": [{"type": "http", "url": "http://127.0.0.1:7891/events", "timeout": 3}]}],
    "SubagentStop":      [{"hooks": [{"type": "http", "url": "http://127.0.0.1:7891/events", "timeout": 3}]}]
  }
}
```

**Critical:** `PermissionRequest.timeout` must be `3600`, not `3`. The server holds that HTTP request open while waiting for the user to click Approve/Deny in the dashboard. A 3-second timeout would cause every permission request to fall back to Claude Code's inline dialog before the user can react.

After editing, verify the JSON is valid:

```bash
python3 -c "import json; json.load(open('$HOME/.claude/settings.json')); print('settings.json valid')"
```

Warn the user: these hooks are global — every Claude Code session on this machine will now POST events to `127.0.0.1:7891`. If the ClaudeMonitor server is not running, the hooks silently time out (3s) and Claude Code continues normally, so this is safe — but they should know.

---

## Step 3 — Verify the bundled icon

The icon ships pre-built inside the `.app`. Confirm it's there:

```bash
ls ClaudeMonitor.app/Contents/Resources/icon.icns
```

If the file is missing, something is wrong with the clone — re-clone the repo. (No build step is needed; the icon is a committed artifact.)

---

## Step 4 — Instantiate the .app launcher from the template

The repo ships a template at `ClaudeMonitor.app/Contents/MacOS/ClaudeMonitor.template`. The live launcher (`ClaudeMonitor` without the `.template` suffix) is deliberately **not** committed — it holds an absolute path to the user's Python interpreter and must be created locally.

Copy the template and make it executable:

```bash
cp ClaudeMonitor.app/Contents/MacOS/ClaudeMonitor.template \
   ClaudeMonitor.app/Contents/MacOS/ClaudeMonitor
chmod +x ClaudeMonitor.app/Contents/MacOS/ClaudeMonitor
```

Open the new `ClaudeMonitor` file and replace the placeholder with the Python path from Step 0(A):

```bash
PYTHON="__SET_ME_TO_ABSOLUTE_PYTHON_PATH__"
```

For a user whose `which python3` returned `/opt/homebrew/bin/python3`, the line becomes:

```bash
PYTHON="/opt/homebrew/bin/python3"
```

The project directory is auto-detected from the `.app`'s own location — you only need to set `PYTHON`. **Do not edit the `.template` file itself** — it stays as the blueprint for future setups.

---

## Step 5 — Smoke-test the server

Start the server on its own to confirm the Python install and the port are both OK:

```bash
python3 server.py &
SERVER_PID=$!
sleep 1
curl -s http://127.0.0.1:7891/sessions && echo
kill $SERVER_PID
wait 2>/dev/null
```

Expected: an empty JSON array `[]` followed by a newline. If you see `Address already in use`, an older server is already running — `pkill -f 'server.py'` and retry.

---

## Step 6 — Launch the app

```bash
open ClaudeMonitor.app
```

A native window should appear showing the ClaudeMonitor dashboard. If the window fails to appear or closes immediately, read the log:

```bash
cat /tmp/claudemonitor.log
```

Common failures and fixes:

| Symptom in log                                   | Cause                                        | Fix                                                             |
| ------------------------------------------------ | -------------------------------------------- | --------------------------------------------------------------- |
| `ModuleNotFoundError: No module named 'webview'` | `PYTHON=` in Step 4 points at wrong interp. | Re-run Step 0 and update the path.                              |
| `server.py failed to start within 10s`           | Port 7891 already in use.                    | `pkill -f 'server.py'`, then reopen the app.                    |
| Nothing logged, window never appears             | `.app` not executable.                      | `chmod +x ClaudeMonitor.app/Contents/MacOS/ClaudeMonitor`.      |
| Gatekeeper "unidentified developer"              | First launch of an unsigned .app.            | Right-click the .app → **Open**, or `xattr -dr com.apple.quarantine ClaudeMonitor.app`. |

---

## Step 7 (optional) — Install to /Applications

```bash
cp -R ClaudeMonitor.app /Applications/
```

After this, the app is launchable from Spotlight and Launchpad. **Do not move the project directory afterward** — the path baked into Step 4 still points at the original location, and the `.app` is only a thin launcher for the code inside that directory.

---

## Verifying the full end-to-end flow

With the app open in the foreground:

1. Open a new terminal and run `claude` (Claude Code CLI) inside any project.
2. Within a few seconds, a tab for that project should appear in the ClaudeMonitor window.
3. Ask Claude to run any shell command. The permission request should appear both in Claude Code's inline dialog and in the ClaudeMonitor tab.

If the tab never appears, the hooks block from Step 2 is not being read — double-check the JSON and restart Claude Code.

---

## Uninstall

1. Close the ClaudeMonitor window (this also kills the background `server.py`).
2. Remove the `.app` bundle:
   ```bash
   rm -rf /Applications/ClaudeMonitor.app   # if you copied it
   ```
3. Remove the `hooks` block you added in Step 2 from `~/.claude/settings.json` (leave the rest of the file intact).
4. Remove persisted state:
   ```bash
   rm -f ~/.claude/monitor-state.json
   ```
5. Optionally uninstall the Python packages:
   ```bash
   python3 -m pip uninstall -y pywebview pillow
   ```

The project directory itself can now be deleted — nothing else on the machine references it.
