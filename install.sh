#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "error: install.sh supports macOS only; use install.ps1 on Windows" >&2
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_path="$script_dir/gitferret.py"
install_root="$HOME/.gitferret"
target_dir="$install_root/bin"
target_path="$target_dir/gitferret"
launcher_path="/usr/local/bin/gitferret"

read -r -p "install to $target_path? (y/N) " reply
if [[ "$reply" != "y" && "$reply" != "Y" ]]; then
  echo "cancelled"
  exit 0
fi

mkdir -p "$target_dir"
cp "$source_path" "$target_path"
chmod 755 "$target_path"

if [[ -e "$launcher_path" || -L "$launcher_path" ]]; then
  if [[ -w "$(dirname "$launcher_path")" ]]; then
    rm -f "$launcher_path"
  elif sudo -n true 2>/dev/null; then
    sudo rm -f "$launcher_path"
  else
    echo "$launcher_path requires admin permission."
    echo "add $target_dir to PATH to run gitferret from this install."
    exit 0
  fi
fi
if [[ -w "$(dirname "$launcher_path")" ]]; then
  ln -s "$target_path" "$launcher_path"
elif sudo -n true 2>/dev/null; then
  sudo ln -s "$target_path" "$launcher_path"
else
  echo "$launcher_path requires admin permission."
  echo "add $target_dir to PATH to run gitferret from this install."
  exit 0
fi

echo "installed: $target_path"
