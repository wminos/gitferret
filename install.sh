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
zprofile_path="$HOME/.zprofile"
path_line='export PATH="$HOME/.gitferret/bin:$PATH"'

read -r -p "install to $target_path? (y/N) " reply
if [[ "$reply" != "y" && "$reply" != "Y" ]]; then
  echo "cancelled"
  exit 0
fi

mkdir -p "$target_dir"
cp "$source_path" "$target_path"
chmod 755 "$target_path"

if [[ -f "$zprofile_path" ]]; then
  if ! grep -Fxq "$path_line" "$zprofile_path"; then
    printf '\n%s\n' "$path_line" >> "$zprofile_path"
  fi
else
  printf '%s\n' "$path_line" > "$zprofile_path"
fi

echo "installed: $target_path"
echo "added PATH to: $zprofile_path"
