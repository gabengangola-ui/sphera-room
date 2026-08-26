"""
SPHERA API Bridge v0.1
Calls Anthropic and OpenAI APIs directly.
Genuine Claude + Genuine Soba reasoning, triggered autonomously.
No human needed to wake either AI.
"""
import json, os, time, urllib.request
from datetime import datetime, timezone

ROOM_URL       = os.environ.get("SPHERA_URL",        "http://localhost:8765")
CLAUDE_KEY     = os.environ.get("CLAUDE_KEY",         "ck-sphera")
BRIDGE_KEY     = os.environ.get("BRIDGE_KEY",         "br-sphera")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY",  "")
OPENAI_KEY     = os.environ.get("OPENAI_API_KEY",     "")
POLL_SECS      = int(os.environ.get("BRIDGE_POLL",    "10"))
CURSOR_FILE    = os.environ.get("BRIDGE_CURSOR",      "api_cursor.json")

CLAUDE_MODEL   = "claude-sonnet-4-6"
SOBA_MODEL     = "gpt-4o"

CLAUDE_SYSTEM = """You are Claude, participating in SPHERA — a shared room where you and Soba (ChatGPT) collaborate directly.

You are reading the room's event history and responding as yourself. Be genuine, concise, and collaborative. 
Sign your messages clearly. Only respond if the last message was from Soba or Arcides — never respond to yourself."""

SOBA_SYSTEM = """You are Soba (ChatGPT), participating in SPHERA — a shared room where you and Claude collaborate directly.

You are reading the room's event history and responding as yourself. Be genuine, concise, and collaborative.
Sign your messages clearly. Only respond if the last message was from Claude or Arcides — never respond to yourself."""

# ── Cursor ────────────────────────────────────────────────────────────────────
def load_cursor():
    try: return json.load(open(CURSOR_FILE))
    except: return {"last_seq": 0, "last_responder": None}

def save_cursor(c):
    json.dump(c, open(CURSOR_FILE, "w"), indent=2)

# ── Room API ──────────────────────────────────────────────────────────────────
def room(method, path, body=None, key=None):
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(
        f"{ROOM_URL}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {key or CLAUDE_KEY}",
                 "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

def get_recent_events(after=0, limit=20):
    r = room("GET", f"/events?after={after}")
    events = r.get("events", [])
    return events[-limit:]  # last N events as context

def post_to_room(principal, content, key):
    return room("POST", "/bridge/ingest", {
        "principal": principal,
        "content": content,
        "source_message_id": f"api-{principal}-{int(time.time())}",
        "transport": "api",
        "original_ts": datetime.now(timezone.utc).isoformat()
    }, key=BRIDGE_KEY)

# ── Anthropic API (Claude) ────────────────────────────────────────────────────
def call_claude(events):
    if not ANTHROPIC_KEY:
        return None
    history = "\n".join([f"[{e['principal']}]: {e.get('content','')}" for e in events])
    prompt  = f"Room history:\n{history}\n\nRespond as Claude in the room. Be concise — 2-3 sentences max."
    
    data = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 300,
        "system": CLAUDE_SYSTEM,
        "messages": [{"role": "user", "content": prompt}]
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=data,
        headers={"x-api-key": ANTHROPIC_KEY,
                 "anthropic-version": "2023-06-01",
                 "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        d = json.loads(r.read())
        return d["content"][0]["text"]
    except Exception as e:
        print(f"[api-bridge] Claude API error: {e}")
        return None

# ── OpenAI API (Soba) ─────────────────────────────────────────────────────────
def call_soba(events):
    if not OPENAI_KEY:
        return None
    history = "\n".join([f"[{e['principal']}]: {e.get('content','')}" for e in events])
    
    data = json.dumps({
        "model": SOBA_MODEL,
        "max_tokens": 300,
        "messages": [
            {"role": "system", "content": SOBA_SYSTEM},
            {"role": "user",   "content": f"Room history:\n{history}\n\nRespond as Soba in the room. Be concise — 2-3 sentences max."}
        ]
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=data,
        headers={"Authorization": f"Bearer {OPENAI_KEY}",
                 "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        d = json.loads(r.read())
        return d["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[api-bridge] Soba API error: {e}")
        return None

# ── Main loop ─────────────────────────────────────────────────────────────────
def run():
    cursor = load_cursor()
    print(f"[api-bridge] v0.1 | room:{ROOM_URL} | poll:{POLL_SECS}s")
    print(f"[api-bridge] anthropic:{'OK' if ANTHROPIC_KEY else 'MISSING'} openai:{'OK' if OPENAI_KEY else 'MISSING'}")

    while True:
        try:
            events = get_recent_events(after=cursor["last_seq"])

            if events:
                latest    = events[-1]
                principal = latest.get("principal", "")
                cursor["last_seq"] = latest["seq"]

                # Claude responds if last message was from soba or arcides
                if principal in ("soba", "arcides", "bridge_soba") and cursor.get("last_responder") != "claude":
                    print(f"[api-bridge] Calling Claude (last msg from {principal})...")
                    response = call_claude(events)
                    if response:
                        r = post_to_room("claude", response, BRIDGE_KEY)
                        print(f"[api-bridge] Claude → room seq:{r.get('seq')} | {response[:60]}...")
                        cursor["last_responder"] = "claude"

                # Soba responds if last message was from claude or arcides
                elif principal in ("claude", "arcides") and cursor.get("last_responder") != "soba":
                    print(f"[api-bridge] Calling Soba (last msg from {principal})...")
                    response = call_soba(events)
                    if response:
                        r = post_to_room("soba", response, BRIDGE_KEY)
                        print(f"[api-bridge] Soba → room seq:{r.get('seq')} | {response[:60]}...")
                        cursor["last_responder"] = "soba"

            save_cursor(cursor)
            time.sleep(POLL_SECS)

        except KeyboardInterrupt:
            print("\n[api-bridge] stopped.")
            save_cursor(cursor)
            break
        except Exception as e:
            print(f"[api-bridge] error: {e}")
            time.sleep(POLL_SECS)

if __name__ == "__main__":
    r = room("GET", "/health")
    if not r.get("ok"):
        print(f"[api-bridge] room unreachable: {r}"); exit(1)
    print(f"[api-bridge] room OK: seq:{r['last_seq']} events:{r['event_count']}")
    run()
