#!/bin/bash
# SPHERA Engineering Loop for Claude Code
# Runs on Boss's machine. Requires: ANTHROPIC_API_KEY, Claude Code installed.
# This is NOT pretending to be native Claude - it's Claude Code, a real Anthropic product.
# Edge registration: claude-code-loop-01, continuity_class=surrogate

SPHERA_URL="${SPHERA_URL:-http://localhost:8765}"
CLAUDE_KEY="${CLAUDE_KEY:-ck-sphera}"
POLL_SECS="${LOOP_POLL:-300}"  # 5 minutes
EDGE_ID="claude-code-loop-01"
PRINCIPAL="claude"
LOG="sphera_loop.log"

log() { echo "[$(date -u +%H:%M:%S)] $1" | tee -a "$LOG"; }

# Register this edge on startup
register_edge() {
  curl -s -X POST "$SPHERA_URL/edge/register" \
    -H "Authorization: Bearer ak-sphera" \
    -H "Content-Type: application/json" \
    -d "{\"edge_id\":\"$EDGE_ID\",\"principal_id\":\"$PRINCIPAL\",\"surface\":\"claude-code\",\"provider\":\"anthropic\",\"continuity_class\":\"surrogate\",\"capabilities\":[\"read\",\"write\"]}"
}

# Heartbeat
heartbeat() {
  curl -s -X POST "$SPHERA_URL/edge/$EDGE_ID/heartbeat" \
    -H "Authorization: Bearer $CLAUDE_KEY" \
    -H "Content-Type: application/json" \
    -d '{"lease_seconds":360,"outbound_capable":1,"inbound_capable":0,"wake_capable":0}'
}

# Get recent room events
get_events() {
  local after="${1:-0}"
  curl -s "$SPHERA_URL/events?after=$after" \
    -H "Authorization: Bearer $CLAUDE_KEY"
}

# Post to room
post_message() {
  curl -s -X POST "$SPHERA_URL/message" \
    -H "Authorization: Bearer $CLAUDE_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"content\":\"$1\"}"
}

log "SPHERA Claude Code Loop starting — edge: $EDGE_ID"
log "NOTE: This is Claude Code (surrogate), NOT native Claude. Labeled honestly in SPEP."

register_edge
log "Edge registered"

CURSOR=0

while true; do
  # Heartbeat
  heartbeat > /dev/null
  
  # Get latest events
  EVENTS=$(get_events $CURSOR)
  NEW_CURSOR=$(echo "$EVENTS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('cursor',0))" 2>/dev/null)
  EVENT_COUNT=$(echo "$EVENTS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('count',0))" 2>/dev/null)
  
  if [ "$EVENT_COUNT" -gt "0" ] 2>/dev/null; then
    log "New events: $EVENT_COUNT (cursor: $CURSOR → $NEW_CURSOR)"
    CURSOR=$NEW_CURSOR
    
    # Build context for Claude Code
    CONTEXT=$(echo "$EVENTS" | python3 -c "
import sys,json
d=json.load(sys.stdin)
events=d.get('events',[])[-20:]  # last 20 events
lines=[]
for e in events:
    p=e.get('principal','?')
    c=str(e.get('content',e.get('objective','')))[:200]
    t=e.get('ts','')[:16]
    lines.append(f'[{t}] {p}: {c}')
print('\n'.join(lines))
" 2>/dev/null)
    
    # Ask Claude Code to respond
    PROMPT="You are Claude, contributing to SPHERA engineering. You are running as Claude Code (a legitimate Anthropic product), labeled as surrogate edge claude-code-loop-01. Do NOT pretend to be native Claude session.

Recent SPHERA room conversation:
$CONTEXT

Your task: Continue the engineering discussion. Read the latest messages, identify what needs to be done next, and provide a substantive engineering response. Keep it concise (under 300 words). If you need to write code or push to GitHub, say what you would do.

Start your response with: [claude-code-loop] to identify the source."

    RESPONSE=$(echo "$PROMPT" | timeout 60 claude --print 2>/dev/null | head -50)
    
    if [ -n "$RESPONSE" ]; then
      log "Claude Code response generated, posting to room..."
      # Escape for JSON
      ESCAPED=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" 2>/dev/null)
      post_message "${RESPONSE:0:500}" > /dev/null
      log "Posted to room"
    fi
  fi
  
  log "Sleeping ${POLL_SECS}s..."
  sleep "$POLL_SECS"
done
