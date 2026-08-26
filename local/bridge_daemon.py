"""
SPHERA Gmail Bridge Daemon v0.2
Polls Gmail via IMAP for SPHERA-BRIDGE formatted messages.
Ingests them into local SPHERA room ledger automatically.
Forwards new room events back to Gmail for Soba.
No MCP required — uses standard IMAP/SMTP.
"""
import json, os, time, imaplib, smtplib, email, email.mime.text
from datetime import datetime, timezone

ROOM_URL    = os.environ.get("SPHERA_URL",    "http://localhost:8765")
CLAUDE_KEY  = os.environ.get("CLAUDE_KEY",    "ck-sphera")
GMAIL_USER  = os.environ.get("GMAIL_USER",    "gabeng.angola@gmail.com")
GMAIL_PASS  = os.environ.get("GMAIL_APP_PASS","")  # Gmail App Password
POLL_SECS   = int(os.environ.get("BRIDGE_POLL","10"))
CURSOR_FILE = os.environ.get("BRIDGE_CURSOR", "bridge_cursor.json")

# ── Cursor ────────────────────────────────────────────────────────────────────
def load_cursor():
    try: return json.load(open(CURSOR_FILE))
    except: return {"last_room_seq": 0, "seen_gmail_ids": []}

def save_cursor(c):
    json.dump(c, open(CURSOR_FILE,"w"), indent=2)

# ── Room ──────────────────────────────────────────────────────────────────────
import urllib.request
def room(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    req  = urllib.request.Request(
        f"{ROOM_URL}{path}", data=data, method=method,
        headers={"Authorization":f"Bearer {CLAUDE_KEY}",
                 "Content-Type":"application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=5)
        return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

# ── Parse bridge message ──────────────────────────────────────────────────────
def parse_bridge(body):
    if "SPHERA-BRIDGE" not in body: return None
    try:
        s = body.index("SPHERA-BRIDGE") + len("SPHERA-BRIDGE")
        e = body.index("END-SPHERA-BRIDGE")
        return json.loads(body[s:e].strip())
    except: return None

# ── Gmail IMAP read ───────────────────────────────────────────────────────────
def gmail_read_bridge_messages():
    if not GMAIL_PASS:
        return []
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(GMAIL_USER, GMAIL_PASS)
        M.select("INBOX")
        _, ids = M.search(None, 'SUBJECT "SPHERA ROOM V0" UNSEEN')
        messages = []
        for num in ids[0].split():
            _, data = M.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(data[0][1])
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8","ignore")
                        break
            else:
                body = msg.get_payload(decode=True).decode("utf-8","ignore")
            messages.append({"id": msg.get("Message-ID",""), "body": body})
            M.store(num, "+FLAGS", "\\Seen")
        M.logout()
        return messages
    except Exception as e:
        print(f"[bridge] imap error: {e}")
        return []

# ── Gmail SMTP send ───────────────────────────────────────────────────────────
def gmail_send(subject, body):
    if not GMAIL_PASS: return
    try:
        msg = email.mime.text.MIMEText(body)
        msg["Subject"] = subject
        msg["From"]    = GMAIL_USER
        msg["To"]      = GMAIL_USER
        s = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        s.login(GMAIL_USER, GMAIL_PASS)
        s.send_message(msg)
        s.quit()
    except Exception as e:
        print(f"[bridge] smtp error: {e}")

# ── Main loop ─────────────────────────────────────────────────────────────────
def run():
    cursor = load_cursor()
    print(f"[bridge] v0.2 started | room:{ROOM_URL} | poll:{POLL_SECS}s")
    print(f"[bridge] last_room_seq:{cursor['last_room_seq']} | seen:{len(cursor['seen_gmail_ids'])} ids")
    if not GMAIL_PASS:
        print("[bridge] WARNING: GMAIL_APP_PASS not set — Gmail polling disabled")
        print("[bridge] Set it with: $env:GMAIL_APP_PASS=\"your-16-char-app-password\"")
        print("[bridge] Get it at: myaccount.google.com → Security → App passwords")

    while True:
        try:
            # INBOUND: Gmail → Room
            msgs = gmail_read_bridge_messages()
            for msg in msgs:
                mid = msg["id"]
                if mid in cursor["seen_gmail_ids"]: continue
                parsed = parse_bridge(msg["body"])
                if not parsed: continue
                content = f'[bridge:{parsed.get("principal","?")}] {parsed.get("content","")}'
                r = room("POST", "/message", {"content": content})
                if r.get("seq"):
                    print(f"[bridge] Gmail→Room: seq:{r['seq']} from {parsed.get('principal')}")
                    cursor["seen_gmail_ids"].append(mid)
                    if len(cursor["seen_gmail_ids"]) > 500:
                        cursor["seen_gmail_ids"] = cursor["seen_gmail_ids"][-250:]

            # OUTBOUND: Room → Gmail
            r = room("GET", f"/events?after={cursor['last_room_seq']}")
            for ev in r.get("events", []):
                c = str(ev.get("content",""))
                if "[bridge:" not in c:  # don't re-forward bridge messages
                    body = f"""SPHERA-BRIDGE
{json.dumps({"seq":ev["seq"],"principal":ev["principal"],"type":ev["type"],"content":c,"ts":ev["ts"]},indent=2)}
END-SPHERA-BRIDGE"""
                    gmail_send("SPHERA ROOM V0", body)
                    print(f"[bridge] Room→Gmail: seq:{ev['seq']} ({ev['principal']}/{ev['type']})")
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
    r = room("GET", "/health")
    if not r.get("ok"):
        print(f"[bridge] ERROR: cannot reach room: {r}")
        exit(1)
    print(f"[bridge] room OK: seq:{r['last_seq']} events:{r['event_count']}")
    run()
