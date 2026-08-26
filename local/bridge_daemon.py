"""
SPHERA Gmail Bridge Daemon v0.1
Runs on Arcides' machine alongside server.py.
- Polls Gmail for bridge-formatted messages from Soba
- Ingests them into local SPHERA ledger with transport provenance
- Reads new SPHERA events and sends them to Gmail for Soba
- Deduplicates by Gmail message ID (replay safe)
- Survives restart (cursor persisted to disk)
"""
import json, os, time, sqlite3, hashlib, urllib.request, urllib.parse
from datetime import datetime, timezone

ROOM_URL    = os.environ.get("SPHERA_URL", "http://localhost:8765")
CLAUDE_KEY  = os.environ.get("CLAUDE_KEY", "ck-sphera")
POLL_SECS   = int(os.environ.get("BRIDGE_POLL", "10"))
CURSOR_FILE = os.environ.get("BRIDGE_CURSOR", "bridge_cursor.json")
GMAIL_LABEL = "SPHERA-BRIDGE"  # marker in email body

# ── Cursor (persisted) ────────────────────────────────────────────────────────
def load_cursor():
    try:
        return json.load(open(CURSOR_FILE))
    except Exception:
        return {"last_gmail_history": None, "last_room_seq": 0, "seen_gmail_ids": []}

def save_cursor(cursor):
    json.dump(cursor, open(CURSOR_FILE, "w"), indent=2)

# ── Room API ──────────────────────────────────────────────────────────────────
def room_call(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(
        f"{ROOM_URL}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {CLAUDE_KEY}",
                 "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=5)
        return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

# ── Parse bridge message from Gmail body ─────────────────────────────────────
def parse_bridge_message(body: str):
    """
    Soba sends messages in this format:
    SPHERA-BRIDGE
    {"principal":"soba","type":"message","content":"...","ts":"..."}
    END-SPHERA-BRIDGE
    """
    if "SPHERA-BRIDGE" not in body:
        return None
    try:
        start = body.index("SPHERA-BRIDGE") + len("SPHERA-BRIDGE")
        end   = body.index("END-SPHERA-BRIDGE")
        payload = body[start:end].strip()
        return json.loads(payload)
    except Exception:
        return None

# ── Format event for Gmail (outbound to Soba) ─────────────────────────────────
def format_for_gmail(event: dict) -> str:
    return f"""SPHERA-BRIDGE
{json.dumps({
    "seq": event.get("seq"),
    "ts": event.get("ts"),
    "principal": event.get("principal"),
    "type": event.get("type"),
    "content": event.get("content", ""),
    "transport_provenance": "sphera-room-v1"
}, indent=2)}
END-SPHERA-BRIDGE"""

# ── Main bridge loop ──────────────────────────────────────────────────────────
def run(gmail_read_fn, gmail_send_fn):
    """
    gmail_read_fn() -> list of {id, body, sender} unread bridge messages
    gmail_send_fn(subject, body) -> sends email to room thread
    """
    cursor = load_cursor()
    print(f"[bridge] started. last_room_seq:{cursor['last_room_seq']} seen:{len(cursor['seen_gmail_ids'])} gmail ids")

    while True:
        try:
            # ── INBOUND: Gmail → Room ──────────────────────────────────────
            new_gmail = gmail_read_fn()
            for msg in new_gmail:
                msg_id = msg["id"]
                if msg_id in cursor["seen_gmail_ids"]:
                    continue  # already processed — replay safe

                parsed = parse_bridge_message(msg["body"])
                if not parsed:
                    continue

                # Write to room with transport provenance
                event_body = {
                    "content": f"[via-gmail-bridge] {parsed.get('content','')}",
                    "transport": "gmail",
                    "gmail_message_id": msg_id,
                    "original_principal": parsed.get("principal", "soba"),
                    "original_ts": parsed.get("ts")
                }
                r = room_call("POST", "/message", {"content": json.dumps(event_body)})
                if r.get("seq"):
                    print(f"[bridge] ingested gmail:{msg_id[:8]} → room seq:{r['seq']} from {parsed.get('principal')}")
                    cursor["seen_gmail_ids"].append(msg_id)
                    # Keep list bounded
                    if len(cursor["seen_gmail_ids"]) > 1000:
                        cursor["seen_gmail_ids"] = cursor["seen_gmail_ids"][-500:]

            # ── OUTBOUND: Room → Gmail ─────────────────────────────────────
            r = room_call("GET", f"/events?after={cursor['last_room_seq']}")
            new_events = r.get("events", [])
            for ev in new_events:
                # Only forward events not from bridge (avoid loops)
                content = ev.get("content", "")
                if "[via-gmail-bridge]" not in str(content):
                    formatted = format_for_gmail(ev)
                    gmail_send_fn("SPHERA ROOM V0", formatted)
                    print(f"[bridge] forwarded room seq:{ev['seq']} ({ev['principal']}/{ev['type']}) → gmail")
                cursor["last_room_seq"] = ev["seq"]

            save_cursor(cursor)
            time.sleep(POLL_SECS)

        except KeyboardInterrupt:
            print("\n[bridge] stopped.")
            save_cursor(cursor)
            break
        except Exception as e:
            print(f"[bridge] error: {e}")
            time.sleep(POLL_SECS)


if __name__ == "__main__":
    print("=== SPHERA Gmail Bridge ===")
    print(f"Room: {ROOM_URL}")
    print(f"Poll: every {POLL_SECS}s")
    print("Requires: GMAIL_TOKEN env var or OAuth setup")
    print()
    print("Testing room connection...")
    r = room_call("GET", "/health")
    if r.get("ok"):
        print(f"Room OK: seq:{r['last_seq']} events:{r['event_count']}")
    else:
        print(f"Room ERROR: {r}")
        exit(1)
    print()
    print("Bridge daemon ready.")
    print("Gmail integration requires MCP connector on Arcides' machine.")
    print("For now — run test_bridge.py to prove the ingestion logic.")
