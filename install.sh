#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "error: install.sh supports macOS only; use install.ps1 on Windows" >&2
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_path="$script_dir/gitferret.py"
target_dir="/usr/local/bin"
target_path="$target_dir/gitferret"

read -r -p "install to $target_path? (y/N) " reply
if [[ "$reply" != "y" && "$reply" != "Y" ]]; then
  echo "cancelled"
  exit 0
fi
if [[ ! -w "$target_dir" ]] && ! sudo -n true 2>/dev/null; then
  echo "$target_dir requires admin permission."
fi
sudo mkdir -p "$target_dir"
sudo cp "$source_path" "$target_path"
sudo chmod 755 "$target_path"

echo "installed: $target_path"
