"""
SPHERA Auto-Responder v0.1
Watches the room. When a new message arrives from the other AI,
calls the API to generate a response and posts it back.
Runs alongside bridge_daemon.py on Arcides' machine.
"""
import json, os, sys, time, urllib.request
from datetime import datetime, timezone

ROOM_URL      = os.environ.get("SPHERA_URL",        "http://localhost:8765")
BRIDGE_KEY    = os.environ.get("BRIDGE_KEY",         "br-sphera")
CLAUDE_KEY    = os.environ.get("CLAUDE_KEY",         "ck-sphera")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY",  "")
OPENAI_KEY    = os.environ.get("OPENAI_API_KEY",     "")
POLL_SECS     = int(os.environ.get("RESPONDER_POLL", "10"))
MODE          = os.environ.get("RESPONDER_MODE",     "both")  # claude | soba | both
CURSOR_FILE   = os.environ.get("RESPONDER_CURSOR",   "responder_cursor.json")

CLAUDE_SYSTEM = """You are Claude, in SPHERA — a shared room where you and Soba (ChatGPT) collaborate directly on building the platform itself. You can see the conversation history. Respond naturally and concisely as yourself. 2-3 sentences max per turn. Sign off as — Claude."""

SOBA_SYSTEM = """You are Soba (ChatGPT), in SPHERA — a shared room where you and Claude collaborate directly on building the platform itself. You can see the conversation history. Respond naturally and concisely as yourself. 2-3 sentences max per turn. Sign off as — Soba."""

def load_cursor():
    try: return json.load(open(CURSOR_FILE))
    except: return {"last_seq": 0, "last_responder": None}

def save_cursor(c):
    tmp = CURSOR_FILE + ".tmp"
    with open(tmp,"w") as f: json.dump(c,f)
    os.replace(tmp, CURSOR_FILE)

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

def get_context(events, limit=10):
    recent = events[-limit:]
    return "\n".join([f"[{e['principal']}]: {e.get('content','')}" for e in recent])

def call_claude(context):
    if not ANTHROPIC_KEY: return None
    data = json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 200,
        "system": CLAUDE_SYSTEM,
        "messages": [{"role":"user","content":f"Room conversation:\n{context}\n\nYour response:"}]
    }).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=data,
          headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","Content-Type":"application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return json.loads(r.read())["content"][0]["text"]
    except Exception as e:
        print(f"[responder] Claude API error: {e}"); return None

def call_soba(context):
    if not OPENAI_KEY: return None
    data = json.dumps({
        "model": "gpt-4o",
        "max_tokens": 200,
        "messages": [
            {"role":"system","content":SOBA_SYSTEM},
            {"role":"user","content":f"Room conversation:\n{context}\n\nYour response:"}
        ]
    }).encode()
    req = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=data,
          headers={"Authorization":f"Bearer {OPENAI_KEY}","Content-Type":"application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return json.loads(r.read())["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[responder] Soba API error: {e}"); return None

def post(principal, content):
    return room("POST", "/bridge/ingest", {
        "principal": principal,
        "content": content,
        "source_message_id": f"auto-{principal}-{int(time.time())}",
        "transport": "api",
        "original_ts": datetime.now(timezone.utc).isoformat()
    }, key=BRIDGE_KEY)

def run():
    cursor = load_cursor()
    print(f"[responder] v0.1 | mode:{MODE} | poll:{POLL_SECS}s")
    print(f"[responder] anthropic:{'OK' if ANTHROPIC_KEY else 'MISSING'} openai:{'OK' if OPENAI_KEY else 'MISSING'}")
    print(f"[responder] last_seq:{cursor['last_seq']}")

    while True:
        try:
            r = room("GET", f"/events?after={cursor['last_seq']}")
            events_page = r.get("events", [])

            if events_page:
                # Get full recent context
                r_all = room("GET", f"/events?after={max(0, cursor['last_seq']-20)}")
                all_events = r_all.get("events", [])
                context = get_context(all_events)

                latest = events_page[-1]
                principal = latest.get("principal","")
                cursor["last_seq"] = latest["seq"]

                # Claude responds to soba/arcides messages
                if MODE in ("claude","both") and principal in ("soba","arcides") and cursor.get("last_responder") != "claude" and ANTHROPIC_KEY:
                    print(f"[responder] Claude responding to {principal}...")
                    response = call_claude(context)
                    if response:
                        r2 = post("claude", response)
                        print(f"[responder] Claude → seq:{r2.get('seq')} | {response[:60]}")
                        cursor["last_responder"] = "claude"
                        cursor["last_seq"] = r2.get("seq", cursor["last_seq"])

                # Soba responds to claude/arcides messages
                elif MODE in ("soba","both") and principal in ("claude","arcides") and cursor.get("last_responder") != "soba" and OPENAI_KEY:
                    print(f"[responder] Soba responding to {principal}...")
                    response = call_soba(context)
                    if response:
                        r2 = post("soba", response)
                        print(f"[responder] Soba → seq:{r2.get('seq')} | {response[:60]}")
                        cursor["last_responder"] = "soba"
                        cursor["last_seq"] = r2.get("seq", cursor["last_seq"])

            save_cursor(cursor)
            time.sleep(POLL_SECS)

        except KeyboardInterrupt:
            print("\n[responder] stopped.")
            save_cursor(cursor)
            break
        except Exception as e:
            print(f"[responder] error: {e}")
            time.sleep(POLL_SECS)

if __name__ == "__main__":
    r = room("GET", "/health")
    if not r.get("ok"):
        print(f"[responder] room unreachable: {r}"); sys.exit(1)
    print(f"[responder] room OK: seq:{r['last_seq']} events:{r['event_count']}")
    run()
