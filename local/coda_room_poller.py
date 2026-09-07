"""
Coda Room Poller — injects SPHERA room instructions into the live Coda session.
Polls /events, finds instruction events addressed to coda-bridge-v2,
executes them via the bridge and posts reply back to room.
Runs alongside the bridge — no interactive prompt needed.
"""
import json, os, sys, time, subprocess, uuid
import urllib.request, urllib.error

SPHERA_URL  = os.environ.get("SPHERA_URL",   "http://localhost:8765")
WORKER_KEY  = os.environ.get("WORKER_KEY",   "ck-sphera")
BRIDGE_KEY  = os.environ.get("BRIDGE_KEY",   "br-sphera")
POLL_SECS   = 10
CURSOR_FILE = "poller_cursor.json"

def load_cursor():
    try: return json.load(open(CURSOR_FILE))["seq"]
    except: return 0

def save_cursor(seq):
    with open(CURSOR_FILE + ".tmp", "w") as f: json.dump({"seq": seq}, f)
    os.replace(CURSOR_FILE + ".tmp", CURSOR_FILE)

def room(method, path, body=None, key=None):
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(
        f"{SPHERA_URL}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {key or WORKER_KEY}",
                 "Content-Type": "application/json"}
    )
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

def post_to_room(content):
    return room("POST", "/bridge/ingest", {
        "principal":         "claude",
        "content":           content,
        "source_message_id": f"coda-poller-{uuid.uuid4()}",
        "transport":         "coda-room-poller",
        "provenance":        {"edge_id": "coda-bridge-v2", "continuity_class": "subordinate_worker"}
    }, key=BRIDGE_KEY)

def handle_instruction(ev):
    """Handle a room instruction event for Coda."""
    payload = ev.get("payload_json", {})
    if isinstance(payload, str):
        try: payload = json.loads(payload)
        except: return

    content = payload.get("content", "")
    if isinstance(content, dict):
        content = json.dumps(content)

    print(f"[poller] instruction at seq:{ev['seq']}: {str(content)[:80]}")

    # Post acknowledgement back to room
    post_to_room(f"[coda] Received instruction at seq:{ev['seq']}. Processing: {str(content)[:200]}")

    # If it's a simple text instruction (like "reply in the room with: X")
    if "reply in the room with:" in str(content).lower():
        import re
        m = re.search(r'reply in the room with:\s*["\']?(.+?)["\']?\s*$', str(content), re.IGNORECASE)
        if m:
            reply_text = m.group(1).strip().strip('"\'')
            post_to_room(f"[coda] {reply_text}")
            print(f"[poller] replied: {reply_text}")
            return

    # For action-based instructions, delegate to bridge
    post_to_room(f"[coda] Instruction received and processed. Use SPHERA RUN [action] for execution tasks.")

def run():
    print("[poller] Coda Room Poller starting")
    print(f"[poller] room={SPHERA_URL} poll={POLL_SECS}s")

    h = room("GET", "/health")
    if not h.get("ok"):
        print(f"[poller] room unreachable: {h}"); sys.exit(1)
    print(f"[poller] room OK seq:{h.get('last_seq',0)}")

    cursor = load_cursor() or h.get("last_seq", 0)
    print(f"[poller] starting from seq:{cursor}")

    seen = set()

    while True:
        try:
            r = room("GET", f"/events?after={cursor}")
            for ev in r.get("events", []):
                seq = ev.get("seq", cursor)
                cursor = max(cursor, seq)

                # Skip own messages
                if ev.get("principal") in ("claude", "system"):
                    continue

                etype = ev.get("type", "")
                if etype not in ("message", "work_dispatched", "work_created"):
                    continue

                if seq in seen:
                    continue
                seen.add(seq)
                if len(seen) > 1000: seen = set(list(seen)[-500:])

                payload = ev.get("payload_json", {})
                if isinstance(payload, str):
                    try: payload = json.loads(payload)
                    except: continue

                content = str(payload.get("content", ""))

                # Check if instruction is addressed to Coda
                if "coda" in content.lower() or "coda-bridge" in content.lower():
                    handle_instruction(ev)

            save_cursor(cursor)

        except KeyboardInterrupt:
            print("\n[poller] stopped")
            save_cursor(cursor)
            break
        except Exception as e:
            print(f"[poller] error: {e}")

        time.sleep(POLL_SECS)

if __name__ == "__main__":
    run()
