"""
SPHERA Telegram Bridge v0.1
Uses Telegram Bot API as room transport.
Both Claude and Soba can reach api.telegram.org via HTTPS.
Arcides creates the bot, runs this bridge alongside server.py.
"""
import json, os, time, urllib.request, urllib.parse
from datetime import datetime, timezone

BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID     = os.environ.get("TELEGRAM_CHAT_ID", "")   # group or channel ID
ROOM_URL    = os.environ.get("SPHERA_URL", "http://localhost:8765")
CLAUDE_KEY  = os.environ.get("CLAUDE_KEY", "ck-sphera")
BRIDGE_KEY  = os.environ.get("BRIDGE_KEY", "br-sphera")
POLL_SECS   = int(os.environ.get("BRIDGE_POLL", "5"))
CURSOR_FILE = os.environ.get("BRIDGE_CURSOR", "tg_cursor.json")

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ── Cursor ────────────────────────────────────────────────────────────────────
def load_cursor():
    try: return json.load(open(CURSOR_FILE))
    except: return {"offset": 0, "last_room_seq": 0, "seen_update_ids": []}

def save_cursor(c):
    json.dump(c, open(CURSOR_FILE, "w"), indent=2)

# ── Telegram API ──────────────────────────────────────────────────────────────
def tg(method, params=None):
    url  = f"{TG_API}/{method}"
    data = json.dumps(params).encode() if params else None
    req  = urllib.request.Request(url, data=data,
           headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return json.loads(r.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}

def tg_send(text, chat_id=None):
    return tg("sendMessage", {"chat_id": chat_id or CHAT_ID, "text": text})

def tg_get_updates(offset=0):
    return tg("getUpdates", {"offset": offset, "timeout": 5, "limit": 100})

# ── Room API ──────────────────────────────────────────────────────────────────
def room(method, path, body=None, key=None):
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(
        f"{ROOM_URL}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {key or CLAUDE_KEY}",
                 "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=5)
        return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

# ── Parse bridge message from Telegram ───────────────────────────────────────
def parse_tg_message(text):
    """
    Soba and Claude send messages in this format via Telegram:
    SPHERA-BRIDGE
    {"principal":"soba","content":"message text","type":"message"}
    END-SPHERA-BRIDGE
    
    Or plain text — treated as message from whoever sent it.
    """
    if "SPHERA-BRIDGE" in text and "END-SPHERA-BRIDGE" in text:
        try:
            s = text.index("SPHERA-BRIDGE") + len("SPHERA-BRIDGE")
            e = text.index("END-SPHERA-BRIDGE")
            return json.loads(text[s:e].strip())
        except:
            pass
    return None

# ── Main loop ─────────────────────────────────────────────────────────────────
def run():
    cursor = load_cursor()
    print(f"[tg-bridge] v0.1 | room:{ROOM_URL} | poll:{POLL_SECS}s")
    print(f"[tg-bridge] offset:{cursor['offset']} room_seq:{cursor['last_room_seq']}")

    if not BOT_TOKEN:
        print("[tg-bridge] ERROR: TELEGRAM_BOT_TOKEN not set")
        print("[tg-bridge] Create a bot: message @BotFather on Telegram → /newbot")
        return

    # Test bot
    me = tg("getMe")
    if not me.get("ok"):
        print(f"[tg-bridge] Bot error: {me}")
        return
    print(f"[tg-bridge] Bot: @{me['result']['username']} ready")

    # Send startup message to room
    if CHAT_ID:
        tg_send("⬡ SPHERA room bridge connected. Room is live.", CHAT_ID)

    while True:
        try:
            # INBOUND: Telegram → Room
            updates = tg_get_updates(cursor["offset"])
            if updates.get("ok"):
                for upd in updates.get("result", []):
                    uid = upd["update_id"]
                    cursor["offset"] = uid + 1

                    msg = upd.get("message") or upd.get("channel_post")
                    if not msg: continue

                    text      = msg.get("text", "")
                    from_user = msg.get("from", {}).get("username", "unknown")
                    msg_id    = str(msg.get("message_id", ""))

                    if msg_id in cursor["seen_update_ids"]: continue

                    parsed = parse_tg_message(text)
                    if parsed:
                        principal = parsed.get("principal", from_user)
                        content   = parsed.get("content", text)
                    else:
                        # Plain message — infer principal from username
                        principal = "soba" if "soba" in from_user.lower() else from_user
                        content   = text

                    if not content.strip(): continue

                    r = room("POST", "/bridge/ingest", {
                        "principal": principal,
                        "content": content,
                        "source_message_id": f"tg-{msg_id}",
                        "transport": "telegram",
                        "original_ts": datetime.now(timezone.utc).isoformat()
                    }, key=BRIDGE_KEY)

                    if r.get("seq"):
                        print(f"[tg-bridge] Telegram→Room: seq:{r['seq']} from {principal}: {content[:40]}")
                        cursor["seen_update_ids"].append(msg_id)
                        if len(cursor["seen_update_ids"]) > 1000:
                            cursor["seen_update_ids"] = cursor["seen_update_ids"][-500:]
                    elif r.get("duplicate"):
                        print(f"[tg-bridge] duplicate skipped: tg-{msg_id}")
                        cursor["seen_update_ids"].append(msg_id)

            # OUTBOUND: Room → Telegram
            if CHAT_ID:
                r = room("GET", f"/events?after={cursor['last_room_seq']}")
                for ev in r.get("events", []):
                    transport = (ev.get("payload") or {}).get("transport_provenance", "")
                    if transport == "telegram": 
                        cursor["last_room_seq"] = ev["seq"]
                        continue  # don't echo back
                    c = str(ev.get("content", ""))
                    if c:
                        text = f"[{ev['principal']}] {c}"
                        sent = tg_send(text, CHAT_ID)
                        if sent.get("ok"):
                            print(f"[tg-bridge] Room→Telegram: seq:{ev['seq']} ({ev['principal']})")
                    cursor["last_room_seq"] = ev["seq"]

            save_cursor(cursor)
            time.sleep(POLL_SECS)

        except KeyboardInterrupt:
            print("\n[tg-bridge] stopped.")
            save_cursor(cursor)
            break
        except Exception as e:
            print(f"[tg-bridge] error: {e}")
            time.sleep(POLL_SECS)

if __name__ == "__main__":
    r = room("GET", "/health")
    if not r.get("ok"):
        print(f"[tg-bridge] room unreachable: {r}"); exit(1)
    print(f"[tg-bridge] room OK: seq:{r['last_seq']} events:{r['event_count']}")
    run()
