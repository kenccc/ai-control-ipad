#!/usr/bin/env bash
# Opt in to the shared Codex app-server daemon.
#
# Without it, AI Control can read every Codex Desktop session and continue any session
# that has no turn in flight. With it, a session the desktop app is actively running
# can also be steered, interrupted and approved from the iPad.
#
# It needs the standalone Codex install, which is a separate download from OpenAI.
set -euo pipefail

CODEX="/Applications/ChatGPT.app/Contents/Resources/codex"
[ -x "$CODEX" ] || CODEX="$(command -v codex)"
STANDALONE="$HOME/.codex/packages/standalone/current/codex"

echo "This enables live write control for Codex Desktop sessions."
echo

if [ ! -x "$STANDALONE" ]; then
  echo "The standalone Codex install is required and is not present."
  echo "It is installed by OpenAI's official installer:"
  echo
  echo "    curl -fsSL https://chatgpt.com/codex/install.sh | sh"
  echo
  read -rp "Run it now? [y/N] " REPLY
  [[ "$REPLY" =~ ^[Yy]$ ]] || { echo "Skipped. AI Control keeps working in read + resume-on-idle mode."; exit 0; }
  curl -fsSL https://chatgpt.com/codex/install.sh | sh
fi

"$CODEX" app-server daemon start
"$CODEX" app-server daemon version

echo
echo "Daemon started. Set codexSharedDaemon: true in ~/.ai-control/config.yaml,"
echo "then check /diagnostics — 'Continue a session with a turn in flight' should"
echo "now read ✓. If it does not, the desktop app is not sharing this daemon and"
echo "AI Control will keep reporting the capability as unavailable rather than"
echo "offering a control that would fail."
