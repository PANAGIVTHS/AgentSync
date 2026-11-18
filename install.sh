#!/usr/bin/env bash
# Set up the agent on this machine: puts `agent` on your PATH, then runs `agent setup`.
# Safe to re-run (for updates too). Extra arguments go to `agent setup`.
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
BIN="$HOME/.local/bin"

[[ "$(id -u)" -ne 0 ]] || { echo "✗ Run as your normal user, not root or sudo." >&2; exit 1; }
for cmd in git python3 docker; do
  command -v "$cmd" >/dev/null || { echo "✗ Missing $cmd. Install it first." >&2; exit 1; }
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || { echo "✗ Python 3.9+ required." >&2; exit 1; }

mkdir -p "$BIN"
chmod +x "$HERE/cli/agent.py" "$HERE"/container/bin/* "$HERE"/container/githooks/*
target="$BIN/agent"
if [[ -e "$target" || -L "$target" ]] && [[ "$(readlink -f -- "$target")" != "$HERE/cli/agent.py" ]]; then
  if [[ "${1:-}" != "--replace-launcher" ]]; then
    echo "✗ $target already exists and isn't this setup's. Re-run with --replace-launcher to replace it." >&2
    exit 1
  fi
  shift
  mv -- "$target" "$target.bak.$(date +%Y%m%d-%H%M%S)"
fi
ln -sfn -- "$HERE/cli/agent.py" "$target"
echo "✓ installed $target"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "! $BIN is not on your PATH. Add this to your shell profile: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
exec python3 "$HERE/cli/agent.py" setup "$@"
