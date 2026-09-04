#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Setting up and installing gitferret globally..."
make -C "$SCRIPT_DIR" install-global

BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"

# Verify PATH
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  echo ""
  echo "Notice: $BIN_DIR is not found in your current PATH."
  echo "Add the following line to your ~/.zprofile or ~/.bashrc to enable global access:"
  echo "  export PATH=\"$BIN_DIR:\$PATH\""
fi

echo ""
echo "Installation complete!"
echo "Global CLI commands available:"
echo "  gitferret"
echo "  git-ferret (or 'git ferret')"
