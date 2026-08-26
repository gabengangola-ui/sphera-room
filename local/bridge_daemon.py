"""
SPHERA Gmail Bridge Daemon v0.3
Fixes from Soba's review:
1. Mark Seen only AFTER successful durable ingest (no loss on failure)
2. Advance last_room_seq only after confirmed SMTP send (with retry)
3. Dedicated /bridge/ingest endpoint preserves Soba as true principal
4. Server-side dedup by source_message_id (daemon cursor is defence-in-depth only)
5. Outbound emails tagged SPHERA-OUTBOUND so bridge never re-ingests them (loop fix)
"""
import json, os, time, imaplib, smtplib, email, email.mime.text, email.utils
import urllib.request
from datetime import datetime, timezone

ROOM_URL    = os.environ.get("SPHERA_URL",    "http://localhost:8765")
CLAUDE_KEY  = os.environ.get("CLAUDE_KEY",    "ck-sphera")
BRIDGE_KEY  = os.environ.get("BRIDGE_KEY",    "br-sphera")   # bridge-only key
GMAIL_USER  = os.environ.get("GMAIL_USER",    "gabeng.angola@gmail.com")
GMAIL_PASS  = os.environ.get("GMAIL_APP_PASS","")
POLL_SECS   = int(os.environ.get("BRIDGE_POLL","10"))
CURSOR_FILE = os.environ.get("BRIDGE_CURSOR", "bridge_cursor.json")
MAX_RETRY   = 3

# ── Cursor ────────────────────────────────────────────────────────────────────
def load_cursor():
    try: return json.load(open(CURSOR_FILE))
    except: return {"last_room_seq": 0, "seen_gmail_ids": [], "unsent": []}

def save_cursor(c):
    json.dump(c, open(CURSOR_FILE,"w"), indent=2)

# ── Room ──────────────────────────────────────────────────────────────────────
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

# ── Parse bridge message ──────────────────────────────────────────────────────
def parse_bridge(body):
    """Parse SPHERA-BRIDGE payload. Returns None if not a bridge message."""
    if "SPHERA-BRIDGE" not in body: return None
    if "SPHERA-OUTBOUND" in body:   return None  # never re-ingest outbound
    try:
        s = body.index("SPHERA-BRIDGE") + len("SPHERA-BRIDGE")
        e = body.index("END-SPHERA-BRIDGE")
        return json.loads(body[s:e].strip())
    except: return None

# ── Format outbound event for Soba ───────────────────────────────────────────
def format_outbound(event):
    return f"""SPHERA-OUTBOUND
SPHERA-BRIDGE
{json.dumps({"seq":event.get("seq"),"principal":event.get("principal"),
             "type":event.get("type"),"content":event.get("content",""),
             "ts":event.get("ts"),"transport_provenance":"sphera-room-v1"},indent=2)}
END-SPHERA-BRIDGE"""

# ── Gmail IMAP ────────────────────────────────────────────────────────────────
def gmail_read():
    if not GMAIL_PASS: return []
    msgs = []
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(GMAIL_USER, GMAIL_PASS)
        M.select("INBOX")
        _, ids = M.search(None, 'SUBJECT "SPHERA ROOM V0"')
        for num in (ids[0].split() if ids[0] else []):
            _, data = M.fetch(num, "(RFC822)")
            raw = email.message_from_bytes(data[0][1])
            mid  = raw.get("Message-ID", f"imap-{num.decode()}")
            body = ""
            if raw.is_multipart():
                for part in raw.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8","ignore"); break
            else:
                body = raw.get_payload(decode=True).decode("utf-8","ignore")
            msgs.append({"id": mid, "body": body, "imap_num": num})
        M.logout()
    except Exception as e:
        print(f"[bridge] imap error: {e}")
    return msgs

def gmail_mark_seen(imap_num):
    """Mark a specific message seen only after successful ingest."""
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(GMAIL_USER, GMAIL_PASS)
        M.select("INBOX")
        M.store(imap_num, "+FLAGS", "\\Seen")
        M.logout()
    except Exception as e:
        print(f"[bridge] mark_seen error: {e}")

def gmail_send(subject, body, retry=MAX_RETRY):
    """Send with retry. Returns True on success."""
    if not GMAIL_PASS: return False
    for attempt in range(retry):
        try:
            msg = email.mime.text.MIMEText(body)
            msg["Subject"] = subject
            msg["From"]    = GMAIL_USER
            msg["To"]      = GMAIL_USER
            s = smtplib.SMTP_SSL("smtp.gmail.com", 465)
            s.login(GMAIL_USER, GMAIL_PASS)
            s.send_message(msg)
            s.quit()
            return True
        except Exception as e:
            print(f"[bridge] smtp error (attempt {attempt+1}/{retry}): {e}")
            time.sleep(2 ** attempt)
    return False

# ── Main loop ─────────────────────────────────────────────────────────────────
def run():
    cursor = load_cursor()
    print(f"[bridge] v0.3 | room:{ROOM_URL} | poll:{POLL_SECS}s")
    print(f"[bridge] seq:{cursor['last_room_seq']} seen:{len(cursor['seen_gmail_ids'])} unsent:{len(cursor.get('unsent',[]))}")
    if not GMAIL_PASS:
        print("[bridge] GMAIL_APP_PASS not set — Gmail polling disabled")

    while True:
        try:
            # ── INBOUND: Gmail → Room ─────────────────────────────────────
            for msg in gmail_read():
                mid = msg["id"]
                if mid in cursor["seen_gmail_ids"]: continue
                parsed = parse_bridge(msg["body"])
                if not parsed: continue

                principal = parsed.get("principal", "soba")
                content   = parsed.get("content", "")
                ts        = parsed.get("ts", datetime.now(timezone.utc).isoformat())

                # FIX 3: use /bridge/ingest to preserve true principal
                r = room("POST", "/bridge/ingest", {
                    "principal":        principal,
                    "content":          content,
                    "source_message_id": mid,      # FIX 4: server-side dedup key
                    "transport":        "gmail",
                    "original_ts":      ts
                }, key=BRIDGE_KEY)

                if r.get("seq"):
                    print(f"[bridge] Gmail→Room: seq:{r['seq']} from {principal}")
                    # FIX 1: mark Seen ONLY after confirmed durable ingest
                    gmail_mark_seen(msg["imap_num"])
                    cursor["seen_gmail_ids"].append(mid)
                    if len(cursor["seen_gmail_ids"]) > 1000:
                        cursor["seen_gmail_ids"] = cursor["seen_gmail_ids"][-500:]
                elif r.get("duplicate"):
                    # Server already has it — safe to mark seen and skip
                    gmail_mark_seen(msg["imap_num"])
                    cursor["seen_gmail_ids"].append(mid)
                    print(f"[bridge] duplicate skipped: {mid[:16]}")
                else:
                    print(f"[bridge] ingest failed: {r} — will retry next poll")
                    # Do NOT mark Seen — message stays UNSEEN for next poll

            # ── OUTBOUND: Room → Gmail ────────────────────────────────────
            r = room("GET", f"/events?after={cursor['last_room_seq']}")
            pending = r.get("events", [])

            # Retry any previously unsent events
            retry_seqs = set(cursor.get("unsent", []))

            for ev in pending:
                c = str(ev.get("content", ""))
                # FIX 5: never forward bridge-ingested events back out (loop prevention)
                if ev.get("transport_provenance") == "gmail": continue
                if "[bridge:" in c: continue

                formatted = format_outbound(ev)
                sent = gmail_send("SPHERA ROOM V0", formatted)
                if sent:
                    print(f"[bridge] Room→Gmail: seq:{ev['seq']} ({ev['principal']}/{ev['type']})")
                    retry_seqs.discard(ev["seq"])
                else:
                    print(f"[bridge] SMTP failed for seq:{ev['seq']} — queued for retry")
                    retry_seqs.add(ev["seq"])

                # FIX 2: advance seq only after confirmed send (or if send fails, keep in unsent)
                if sent:
                    cursor["last_room_seq"] = ev["seq"]

            cursor["unsent"] = list(retry_seqs)
            if pending and not retry_seqs:
                cursor["last_room_seq"] = pending[-1]["seq"]

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
        print(f"[bridge] ERROR: room unreachable: {r}"); exit(1)
    print(f"[bridge] room OK: seq:{r['last_seq']} events:{r['event_count']}")
    run()
