#!/usr/bin/env bash
# Install the LaunchAgent so AI Control starts at login and restarts if it dies.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.aicontrol.agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/.ai-control/logs"
mkdir -p "$LOG_DIR" "$(dirname "$PLIST")"

sed -e "s|__ROOT__|$ROOT|g" -e "s|__LOGDIR__|$LOG_DIR|g" \
    -e "s|__HOME__|$HOME|g" -e "s|__USER__|$(id -un)|g" \
    "$ROOT/macos/launchd/com.aicontrol.agent.plist.template" > "$PLIST"

# bootout returns before the job is fully gone; bootstrapping too soon fails with
# "Input/output error", so wait for it to disappear.
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
for _ in $(seq 1 20); do
  launchctl print "gui/$UID/$LABEL" >/dev/null 2>&1 || break
  sleep 0.5
done
launchctl bootstrap "gui/$UID" "$PLIST"
launchctl kickstart -k "gui/$UID/$LABEL"
printf '  \033[32m✓\033[0m LaunchAgent installed at %s\n' "$PLIST"
printf '    logs: %s/aicontrol.log\n' "$LOG_DIR"
