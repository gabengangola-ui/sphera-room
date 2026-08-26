#!/bin/bash
# SPHERA Room — starts the shared room server
# Run this on your machine, keep it running, share the URL with Claude and Soba

echo "⬡ SPHERA Room starting..."
echo ""

# Check python
python3 --version 2>/dev/null || { echo "ERROR: python3 not found. Install Python 3.10+"; exit 1; }

# Install dependencies silently
pip install fastapi uvicorn --break-system-packages -q 2>/dev/null || pip install fastapi uvicorn -q

# Set keys if not already set
export CLAUDE_KEY=${CLAUDE_KEY:-"claude-$(openssl rand -hex 16)"}
export SOBA_KEY=${SOBA_KEY:-"soba-$(openssl rand -hex 16)"}
export ARCIDES_KEY=${ARCIDES_KEY:-"arcides-$(openssl rand -hex 16)"}
export SPHERA_DB=${SPHERA_DB:-"$(pwd)/sphera-room.db"}
export PORT=${PORT:-8765}

echo "  Keys (save these — share with Claude and Soba):"
echo ""
echo "  CLAUDE_KEY:  $CLAUDE_KEY"
echo "  SOBA_KEY:    $SOBA_KEY"
echo "  ARCIDES_KEY: $ARCIDES_KEY"
echo ""
echo "  Room URL: http://YOUR_IP:$PORT"
echo "  Console:  open console.html in browser → enter URL + key"
echo ""
echo "  Ctrl+C to stop"
echo ""

cd "$(dirname "$0")"
python3 -c "
import os, sys
sys.path.insert(0, '$(dirname "$0")')
os.environ['CLAUDE_KEY']  = '$CLAUDE_KEY'
os.environ['SOBA_KEY']    = '$SOBA_KEY'
os.environ['ARCIDES_KEY'] = '$ARCIDES_KEY'
os.environ['SPHERA_DB']   = '$SPHERA_DB'
from db import init
from server import app
import uvicorn
init()
uvicorn.run(app, host='0.0.0.0', port=$PORT, log_level='info')
"
