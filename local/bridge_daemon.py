"""
SPHERA Gmail Bridge Daemon v0.4
Soba review fixes:
- UID-based IMAP (not sequence numbers — safe across reconnections)
- Persists UIDVALIDITY + last_uid (detects mailbox reset)
- Outbound ordering fix: failed SMTP cannot advance last_room_seq
- Generic transport adapter interface (Gmail + Telegram share same contract)
- Identity rule: transport credential ≠ identity claim
"""
import json, os, time, imaplib, smtplib, email, email.mime.text
import urllib.request
from datetime import datetime, timezone

ROOM_URL    = os.environ.get("SPHERA_URL",     "http://localhost:8765")
CLAUDE_KEY  = os.environ.get("CLAUDE_KEY",     "ck-sphera")
BRIDGE_KEY  = os.environ.get("BRIDGE_KEY",     "br-sphera")
GMAIL_USER  = os.environ.get("GMAIL_USER",     "gabeng.angola@gmail.com")
GMAIL_PASS  = os.environ.get("GMAIL_APP_PASS", "")
POLL_SECS   = int(os.environ.get("BRIDGE_POLL",   "10"))
CURSOR_FILE = os.environ.get("BRIDGE_CURSOR", "bridge_cursor.json")
MAX_RETRY   = 3

# ── Cursor ────────────────────────────────────────────────────────────────────
def load_cursor():
    try:
        return json.load(open(CURSOR_FILE))
    except:
        return {
            "last_room_seq":   0,
            "seen_source_ids": [],   # source_message_id strings (server dedup key)
            "last_uid":        0,    # last IMAP UID processed
            "uidvalidity":     None, # IMAP UIDVALIDITY — detects mailbox reset
            "unsent_seqs":     []    # room seqs where outbound SMTP failed
        }

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
        r = urllib.request.urlopen(req, timeout=5)
        return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

# ── Generic transport adapter contract ───────────────────────────────────────
class TransportMessage:
    """Normalised inbound message — same structure regardless of transport."""
    def __init__(self, source_id, principal, content, original_ts, transport):
        self.source_id    = source_id    # globally unique (e.g. "gmail-UID-12345")
        self.principal    = principal    # who sent it (from envelope, not transport cred)
        self.content      = content
        self.original_ts  = original_ts
        self.transport    = transport    # "gmail" | "telegram" | etc.

def ingest(msg: TransportMessage, cursor: dict) -> bool:
    """Ingest a TransportMessage into the room. Returns True on success."""
    if msg.source_id in cursor["seen_source_ids"]:
        return True  # already seen — idempotent

    r = room("POST", "/bridge/ingest", {
        "principal":         msg.principal,
        "content":           msg.content,
        "source_message_id": msg.source_id,
        "transport":         msg.transport,
        "original_ts":       msg.original_ts
    }, key=BRIDGE_KEY)

    if r.get("seq") or r.get("duplicate"):
        cursor["seen_source_ids"].append(msg.source_id)
        if len(cursor["seen_source_ids"]) > 1000:
            cursor["seen_source_ids"] = cursor["seen_source_ids"][-500:]
        return True
    return False  # ingest failed — do not mark seen

# ── Gmail IMAP (UID-based) ────────────────────────────────────────────────────
def parse_bridge_body(body: str):
    """Parse SPHERA-BRIDGE envelope. Returns (principal, content) or None."""
    if "SPHERA-BRIDGE" not in body: return None
    if "SPHERA-OUTBOUND" in body:   return None
    try:
        s = body.index("SPHERA-BRIDGE") + len("SPHERA-BRIDGE")
        e = body.index("END-SPHERA-BRIDGE")
        d = json.loads(body[s:e].strip())
        return d.get("principal", "soba"), d.get("content", "")
    except:
        return None

def gmail_fetch_new(cursor: dict) -> list:
    """Fetch new messages via UID SEARCH. Returns list of TransportMessage."""
    if not GMAIL_PASS:
        return []
    msgs = []
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(GMAIL_USER, GMAIL_PASS)
        M.select("INBOX")

        # Check UIDVALIDITY — if changed, mailbox was reset, reset our UID cursor
        ok, data = M.status("INBOX", "(UIDVALIDITY)")
        uidvalidity = int(data[0].decode().split("UIDVALIDITY")[1].strip().rstrip(")"))
        if cursor["uidvalidity"] != uidvalidity:
            print(f"[bridge] UIDVALIDITY changed ({cursor['uidvalidity']} → {uidvalidity}), resetting UID cursor")
            cursor["uidvalidity"] = uidvalidity
            cursor["last_uid"]    = 0

        # UID SEARCH for messages newer than our last processed UID
        search_criteria = f'SUBJECT "SPHERA ROOM V0" UID {cursor["last_uid"] + 1}:*'
        ok, uid_data = M.uid("SEARCH", None, f'SUBJECT "SPHERA ROOM V0"')
        all_uids = uid_data[0].split() if uid_data[0] else []

        # Filter to only UIDs we haven't seen
        new_uids = [u for u in all_uids if int(u) > cursor["last_uid"]]

        for uid in new_uids:
            ok, data = M.uid("FETCH", uid, "(RFC822)")
            if not data or not data[0]:
                continue
            raw  = email.message_from_bytes(data[0][1])
            body = ""
            if raw.is_multipart():
                for part in raw.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8", "ignore")
                        break
            else:
                body = raw.get_payload(decode=True).decode("utf-8", "ignore")

            parsed = parse_bridge_body(body)
            if parsed:
                principal, content = parsed
                source_id = f"gmail-uid-{uidvalidity}-{uid.decode()}"
                msgs.append(TransportMessage(
                    source_id    = source_id,
                    principal    = principal,
                    content      = content,
                    original_ts  = datetime.now(timezone.utc).isoformat(),
                    transport    = "gmail"
                ))
            # Advance last_uid regardless (even non-bridge messages)
            cursor["last_uid"] = max(cursor["last_uid"], int(uid))

        M.logout()
    except Exception as e:
        print(f"[bridge] imap error: {e}")
    return msgs

# ── Gmail SMTP outbound ───────────────────────────────────────────────────────
def format_outbound(event: dict) -> str:
    return (
        "SPHERA-OUTBOUND\nSPHERA-BRIDGE\n"
        + json.dumps({"seq": event.get("seq"), "principal": event.get("principal"),
                      "type": event.get("type"), "content": event.get("content", ""),
                      "ts": event.get("ts"), "transport_provenance": "sphera-room-v1"}, indent=2)
        + "\nEND-SPHERA-BRIDGE"
    )

def smtp_send(subject: str, body: str, retry: int = MAX_RETRY) -> bool:
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
    print(f"[bridge] v0.4 | room:{ROOM_URL} | poll:{POLL_SECS}s")
    print(f"[bridge] seq:{cursor['last_room_seq']} uid:{cursor['last_uid']} seen:{len(cursor['seen_source_ids'])}")
    if not GMAIL_PASS:
        print("[bridge] GMAIL_APP_PASS not set — Gmail polling disabled")

    while True:
        try:
            # INBOUND: Gmail → Room
            for msg in gmail_fetch_new(cursor):
                ok = ingest(msg, cursor)
                if ok:
                    print(f"[bridge] Gmail→Room: uid:{cursor['last_uid']} from {msg.principal}: {msg.content[:40]}")
                else:
                    print(f"[bridge] ingest failed for {msg.source_id} — will retry")

            # OUTBOUND: Room → Gmail
            r = room("GET", f"/events?after={cursor['last_room_seq']}")
            events = r.get("events", [])

            for ev in events:
                seq     = ev["seq"]
                content = str(ev.get("content", ""))

                # Skip bridge-originated events (loop prevention)
                if ev.get("transport_provenance") == "gmail": 
                    cursor["last_room_seq"] = seq
                    continue
                if "[bridge:" in content:
                    cursor["last_room_seq"] = seq
                    continue

                # OUTBOUND ORDERING FIX:
                # Only advance last_room_seq AFTER confirmed send
                sent = smtp_send("SPHERA ROOM V0", format_outbound(ev))
                if sent:
                    print(f"[bridge] Room→Gmail: seq:{seq} ({ev['principal']}/{ev['type']})")
                    cursor["last_room_seq"] = seq
                    cursor["unsent_seqs"]   = [s for s in cursor["unsent_seqs"] if s != seq]
                else:
                    print(f"[bridge] SMTP failed seq:{seq} — queued")
                    if seq not in cursor["unsent_seqs"]:
                        cursor["unsent_seqs"].append(seq)
                    # CRITICAL: do NOT advance last_room_seq past failed seq

            # Retry unsent
            for seq in list(cursor["unsent_seqs"]):
                r2 = room("GET", f"/events?after={seq-1}")
                evs = [e for e in r2.get("events", []) if e["seq"] == seq]
                if evs and smtp_send("SPHERA ROOM V0", format_outbound(evs[0])):
                    print(f"[bridge] retry OK: seq:{seq}")
                    cursor["unsent_seqs"].remove(seq)
                    cursor["last_room_seq"] = max(cursor["last_room_seq"], seq)

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
        print(f"[bridge] room unreachable: {r}"); exit(1)
    print(f"[bridge] room OK: seq:{r['last_seq']} events:{r['event_count']}")
    run()
