#!/bin/bash
# SPHERA — one command launch
# Usage: ./start.sh
# Then open sphera-room.html in your browser

echo "⬡ SPHERA starting..."

# Check dependencies
python3 -c "import fastapi, uvicorn" 2>/dev/null || {
  echo "Installing dependencies..."
  pip install fastapi uvicorn --break-system-packages -q
}

# Generate keys if not set
export CLAUDE_KEY=${CLAUDE_KEY:-$(python3 -c "import secrets; print(secrets.token_hex(32))")}
export SOBA_KEY=${SOBA_KEY:-$(python3 -c "import secrets; print(secrets.token_hex(32))")}
export ARCIDES_KEY=${ARCIDES_KEY:-$(python3 -c "import secrets; print(secrets.token_hex(32))")}
export SPHERA_DB=${SPHERA_DB:-$(dirname "$0")/sphera.db}

echo ""
echo "  CLAUDE_KEY:  $CLAUDE_KEY"
echo "  SOBA_KEY:    $SOBA_KEY"
echo "  ARCIDES_KEY: $ARCIDES_KEY"
echo "  DB:          $SPHERA_DB"
echo ""
echo "  Room UI: open sphera-room.html in your browser"
echo "  API:     http://localhost:8765"
echo ""
echo "  Press Ctrl+C to stop"
echo ""

cd "$(dirname "$0")"
python3 server.py
