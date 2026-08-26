#!/usr/bin/env bash
# Install the LaunchAgent so AI Control starts at login and restarts if it dies.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.aicontrol.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/.ai-control/logs"
mkdir -p "$LOG_DIR" "$(dirname "$PLIST")"

sed -e "s|__ROOT__|$ROOT|g" -e "s|__LOGDIR__|$LOG_DIR|g" \
    "$ROOT/macos/launchd/com.aicontrol.agent.plist.template" > "$PLIST"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
launchctl kickstart -k "gui/$UID/$LABEL"
printf '  \033[32m✓\033[0m LaunchAgent installed at %s\n' "$PLIST"
printf '    logs: %s/aicontrol.log\n' "$LOG_DIR"
