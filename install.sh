#!/usr/bin/env bash
# Set up the agent on this machine: a private Python environment for the CLI's
# terminal output (Rich), the `agent` command on your PATH, then `agent setup`.
# Safe to re-run (for updates too). Extra arguments go to `agent setup`.
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
BIN="$HOME/.local/bin"
VENV="$HOME/.local/share/agent/venv"
MARKER="# agent launcher, managed by"

[[ "$(id -u)" -ne 0 ]] || { echo "✗ Run as your normal user, not root or sudo." >&2; exit 1; }
for cmd in git python3 docker; do
  command -v "$cmd" >/dev/null || { echo "✗ Missing $cmd. Install it first." >&2; exit 1; }
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || { echo "✗ Python 3.9+ required." >&2; exit 1; }
chmod +x "$HERE/cli/agent.py" "$HERE"/container/bin/* "$HERE"/container/githooks/*

# Python for the CLI: a venv with Rich, or plain python3 if that isn't possible.
PY="$(command -v python3)"
if [[ -x "$VENV/bin/python" ]] || python3 -m venv "$VENV" >/dev/null 2>&1; then
  if "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade -r "$HERE/requirements.txt"; then
    PY="$VENV/bin/python"
    echo "✓ CLI environment ready ($VENV)"
  else
    echo "! couldn't install Rich (offline?). The CLI works, with plain output. Re-run later to retry."
    PY="$VENV/bin/python"
  fi
else
  echo "! couldn't create a Python venv (Ubuntu: sudo apt install python3-venv). Using plain output for now."
fi

mkdir -p "$BIN"
target="$BIN/agent"
if [[ -e "$target" || -L "$target" ]]; then
  ours=0
  [[ "$(readlink -f -- "$target")" == "$HERE/cli/agent.py" ]] && ours=1
  grep -qF "$MARKER" "$target" 2>/dev/null && ours=1
  if (( ! ours )); then
    if [[ "${1:-}" != "--replace-launcher" ]]; then
      echo "✗ $target already exists and isn't this setup's. Re-run with --replace-launcher to replace it." >&2
      exit 1
    fi
    shift
    mv -- "$target" "$target.bak.$(date +%Y%m%d-%H%M%S)"
  fi
fi
rm -f -- "$target"
cat > "$target" <<EOF
#!/bin/sh
$MARKER $HERE/install.sh
exec "$PY" "$HERE/cli/agent.py" "\$@"
EOF
chmod +x "$target"
echo "✓ installed $target"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "! $BIN is not on your PATH. Add this to your shell profile: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
# Tab completion for your shell (bash, zsh or fish); harmless if it can't.
"$PY" "$HERE/cli/agent.py" completion install >/dev/null 2>&1 && echo "✓ tab completion installed (open a new terminal)" || true
# `agent setup` asks where to keep the agent's data the first time (or pass --home PATH).
exec "$PY" "$HERE/cli/agent.py" setup "$@"
