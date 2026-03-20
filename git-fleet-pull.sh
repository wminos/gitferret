#!/usr/bin/env bash
set -euo pipefail

invoked_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script_path="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "${BASH_SOURCE[0]}")"
script_dir="$(cd "$(dirname "$script_path")" && pwd)"

if (($# == 0)); then
  exec python3 "$script_dir/git-fleet-pull.py" "$invoked_dir"
fi

exec python3 "$script_dir/git-fleet-pull.py" "$@"
