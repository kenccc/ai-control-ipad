#!/usr/bin/env bash
# AI Control — one-shot setup for the Mac companion daemon.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_HOME="${AICONTROL_HOME:-$HOME/.ai-control}"
CONFIG="$APP_HOME/config.yaml"
KEYCHAIN_SERVICE="ai-control"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1"; }

keychain_set() { security add-generic-password -s "$KEYCHAIN_SERVICE" -a "$1" -w "$2" -U >/dev/null; }
keychain_has() { security find-generic-password -s "$KEYCHAIN_SERVICE" -a "$1" -w >/dev/null 2>&1; }

bold "AI Control setup"
echo

bold "1. Checking what is installed"
command -v git >/dev/null && ok "git $(git --version | awk '{print $3}')" || fail "git not found"

PY="$(command -v python3 || true)"
[ -n "$PY" ] && ok "python3 $($PY -V | awk '{print $2}')" || { fail "python3 is required"; exit 1; }
command -v node >/dev/null && ok "node $(node --version)" || { fail "node is required to build the web app"; exit 1; }

CODEX_APP="/Applications/ChatGPT.app/Contents/Resources/codex"
if [ -x "$CODEX_APP" ]; then
  ok "Codex Desktop core $("$CODEX_APP" --version 2>/dev/null | awk '{print $2}') (bundled in the app)"
else
  warn "Codex Desktop not found at /Applications/ChatGPT.app — Codex App sessions will not be discovered"
fi
command -v codex >/dev/null && ok "Codex CLI $(codex --version 2>/dev/null | awk '{print $2}')" \
  || warn "codex CLI not on PATH"
command -v claude >/dev/null && ok "Claude Code $(claude --version 2>/dev/null | awk '{print $1}')" \
  || warn "claude not on PATH"
TS_CLI="$(command -v tailscale || true)"
for c in /usr/local/bin/tailscale "$HOME/.local/bin/tailscale"; do
  [ -n "$TS_CLI" ] && break
  [ -x "$c" ] && TS_CLI="$c"
done
if [ -n "$TS_CLI" ]; then
  if "$TS_CLI" status >/dev/null 2>&1; then
    ok "Tailscale connected ($("$TS_CLI" status --json | "$PY" -c 'import json,sys;print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))'))"
  else
    warn "Tailscale CLI present but not connected — run: $TS_CLI up"
  fi
elif [ -d /Applications/Tailscale.app ]; then
  # The app binary resolves its bundle id from its own path, so the CLI is an exec
  # wrapper, not a symlink. This installs one without needing a password.
  "$ROOT/scripts/install-tailscale-cli.sh" >/dev/null 2>&1 \
    && ok "Tailscale CLI installed" \
    || warn "Tailscale.app present but its CLI could not be installed — run ./scripts/install-tailscale-cli.sh"
else
  warn "Tailscale not installed — install it to reach AI Control from your iPad outside the LAN"
fi
echo

bold "2. Python environment"
[ -d "$ROOT/.venv" ] || "$PY" -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/pip" install -q --disable-pip-version-check -r "$ROOT/apps/server/requirements.txt"
ok "dependencies installed into .venv"
echo

bold "3. Web app"
( cd "$ROOT/apps/web" && npm install --silent --no-fund --no-audit && npm run build >/dev/null )
ok "built to apps/web/dist"
echo

bold "4. Secrets (macOS Keychain — never written to disk or git)"
mkdir -p "$APP_HOME"
if keychain_has auth-token; then
  ok "access token already stored"
else
  TOKEN="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(24))')"
  keychain_set auth-token "$TOKEN"
  ok "generated an access token"
  ACCESS_TOKEN="$TOKEN"
fi
keychain_has session-secret || keychain_set session-secret "$("$PY" -c 'import secrets; print(secrets.token_hex(32))')"
ok "session signing key ready"

if ! keychain_has forgejo-token; then
  warn "no Forgejo token stored. Add one with: ./scripts/set-secret.sh forgejo-token"
fi
echo

bold "5. Configuration"
if [ -f "$CONFIG" ]; then
  ok "using existing $CONFIG"
else
  cat > "$CONFIG" <<'YAML'
host: 127.0.0.1
port: 8787
reconcileInterval: 2.0

# Agents and terminals are restricted to these roots. An empty list allows nothing.
repositories: {}
#  inventory:
#    path: /Users/you/dev/inventory
#    forgejo: owner/inventory

forgejo:
  url: ""

# Opt-in: use the shared Codex app-server daemon so a Codex Desktop session with a
# turn in flight can be continued from the iPad. Run ./scripts/enable-codex-daemon.sh.
codexSharedDaemon: false
YAML
  ok "wrote $CONFIG — add your repositories to it"
fi
mkdir -p "$APP_HOME/worktrees"
echo

bold "6. Database"
"$ROOT/.venv/bin/python" - <<PYEOF
import sys; sys.path.insert(0, "$ROOT/apps/server")
from aicontrol.config import DB_PATH
from aicontrol.db import Database
Database(DB_PATH)
print("  \033[32m✓\033[0m schema ready at", DB_PATH)
PYEOF
echo

bold "7. Launch agent"
"$ROOT/scripts/install-launchagent.sh"
echo

TS_NAME=""
[ -n "${TS_CLI:-}" ] && TS_NAME="$("$TS_CLI" status --json 2>/dev/null | "$PY" -c 'import json,sys;print((json.load(sys.stdin).get("Self") or {}).get("DNSName","").rstrip("."))' 2>/dev/null || true)"
PORT="$("$PY" -c "import yaml;print((yaml.safe_load(open('$CONFIG')) or {}).get('port',8787))" 2>/dev/null || echo 8787)"

bold "AI Control running."
echo
HOST_SETTING="$("$PY" -c "import yaml;print((yaml.safe_load(open('$CONFIG')) or {}).get('host','127.0.0.1'))" 2>/dev/null || echo 127.0.0.1)"
if [ -n "${TS_NAME:-}" ]; then
  echo "  On your iPad:"
  echo "    http://$TS_NAME:$PORT"
  echo
  if [ "$HOST_SETTING" = "tailscale" ]; then
    echo "  Bound to the tailnet interface only — not reachable from the local network."
  else
    echo "  Note: config.yaml has host: $HOST_SETTING."
    echo "  Set 'host: tailscale' to bind the tailnet address so the iPad can connect."
  fi
else
  echo "  Local:"
  echo "    http://127.0.0.1:$PORT"
  echo
  echo "  Tailscale:"
  echo "    not connected — run 'tailscale up', set 'host: tailscale' in $CONFIG,"
  echo "    then reach it at http://<your-mac>.ts.net:$PORT"
fi
if [ -n "${ACCESS_TOKEN:-}" ]; then
  echo
  echo "  Access token (shown once — it lives in your Keychain):"
  echo "    $ACCESS_TOKEN"
fi
echo
echo "Open this URL on your iPad and add it to the Home Screen."
