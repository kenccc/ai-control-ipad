#!/usr/bin/env bash
# Store a secret in the macOS Keychain. Nothing sensitive is ever written to config.
set -euo pipefail
ACCOUNT="${1:-}"
if [ -z "$ACCOUNT" ]; then
  echo "usage: $0 <forgejo-token|auth-token|session-secret>" >&2
  exit 1
fi
printf 'Value for %s (input hidden): ' "$ACCOUNT"
read -rs VALUE
echo
security add-generic-password -s ai-control -a "$ACCOUNT" -w "$VALUE" -U
echo "Stored $ACCOUNT in the login keychain."
