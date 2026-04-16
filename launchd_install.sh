#!/usr/bin/env bash
# Install ClaudeMonitor as a macOS login item via launchd.
# Run once: bash launchd_install.sh
# Uninstall:  bash launchd_install.sh --uninstall

set -euo pipefail

LABEL="com.claudemonitor.daemon"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER="$SCRIPT_DIR/server.py"
LOG="$HOME/.claude/monitor.log"

# ── Uninstall ─────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--uninstall" ]]; then
  launchctl unload "$PLIST" 2>/dev/null && echo "Unloaded $LABEL" || true
  rm -f "$PLIST"
  echo "Removed $PLIST"
  echo "Done. ClaudeMonitor will no longer start at login."
  exit 0
fi

# ── Detect python3 ────────────────────────────────────────────────────────
PYTHON="$(which python3 2>/dev/null || true)"
if [[ -z "$PYTHON" ]]; then
  echo "ERROR: python3 not found in PATH. Install Python 3 first." >&2
  exit 1
fi

echo "Using Python: $PYTHON"
echo "Server:       $SERVER"
echo "Log:          $LOG"
echo "Plist:        $PLIST"
echo ""

# ── Verify server.py exists ───────────────────────────────────────────────
if [[ ! -f "$SERVER" ]]; then
  echo "ERROR: $SERVER not found." >&2
  exit 1
fi

# ── Write plist ───────────────────────────────────────────────────────────
mkdir -p "$(dirname "$PLIST")"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON}</string>
    <string>${SERVER}</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${SCRIPT_DIR}</string>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>${LOG}</string>

  <key>StandardErrorPath</key>
  <string>${LOG}</string>

  <key>ThrottleInterval</key>
  <integer>10</integer>
</dict>
</plist>
PLIST

echo "Wrote $PLIST"

# ── Load (or reload) the agent ────────────────────────────────────────────
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load   "$PLIST"

echo ""
echo "ClaudeMonitor installed and started."
echo "  Status:    launchctl list | grep claudemonitor"
echo "  Logs:      tail -f $LOG"
echo "  Uninstall: bash $0 --uninstall"
