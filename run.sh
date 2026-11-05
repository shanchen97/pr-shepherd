#!/bin/bash
# launchd entrypoint: all machine-specific values come from config.env.
set -euo pipefail
SHEPHERD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SHEPHERD_DIR/config.env"
if [ ! -f "$CONFIG" ]; then
    echo "missing $CONFIG — copy config.example.env to config.env and fill it in" >&2
    exit 1
fi
set -a
# shellcheck source=/dev/null
. "$CONFIG"
set +a
export PATH="${EXTRA_PATH:+$EXTRA_PATH:}/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
exec python3 -I "$SHEPHERD_DIR/runner.py" "$SHEPHERD_DIR"
