#!/usr/bin/env bash
# Install the `tailscale` CLI for the standalone Tailscale.app.
#
# The app's binary resolves its own bundle identifier from its path, so symlinking to
# it aborts with "The current bundleIdentifier is unknown to the registry". The app's
# own installer writes a small exec wrapper instead; this does the same, and falls back
# to ~/.local/bin when /usr/local/bin needs a password you would rather not type.
set -euo pipefail

APP_BIN="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
[ -x "$APP_BIN" ] || { echo "Tailscale.app is not installed at /Applications." >&2; exit 1; }

write_wrapper() {
  printf '#!/bin/sh\n# Tailscale CLI wrapper (see scripts/install-tailscale-cli.sh).\nexec %s "$@"\n' \
    "$APP_BIN" > "$1"
  chmod +x "$1"
}

if [ -w /usr/local/bin ] || mkdir -p /usr/local/bin 2>/dev/null && [ -w /usr/local/bin ]; then
  write_wrapper /usr/local/bin/tailscale
  TARGET=/usr/local/bin/tailscale
else
  mkdir -p "$HOME/.local/bin"
  write_wrapper "$HOME/.local/bin/tailscale"
  TARGET="$HOME/.local/bin/tailscale"
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "Note: add \$HOME/.local/bin to your PATH to use it from a shell." ;;
  esac
fi

echo "Installed $TARGET"
"$TARGET" status >/dev/null 2>&1 \
  && echo "Tailscale is connected." \
  || echo "Tailscale is installed but not connected yet. Run: $TARGET up"
