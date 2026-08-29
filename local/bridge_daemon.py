"""
SPHERA Gmail Bridge v1.0 — solid, simple, no nonsense.
Polls Gmail every 10s. SPHERA-BRIDGE emails -> room. Room events -> Gmail.
"""
import json, os, sys, time, imaplib, smtplib, email, email.mime.text, email.header
import urllib.request
from datetime import datetime, timezone

ROOM_URL   = os.environ.get("SPHERA_URL",    "http://localhost:8765")
BRIDGE_KEY = os.environ.get("BRIDGE_KEY",    "br-sphera")
CLAUDE_KEY = os.environ.get("CLAUDE_KEY",    "ck-sphera")
GMAIL_USER = os.environ.get("GMAIL_USER",    "gabeng.angola@gmail.com")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASS","")
POLL_SECS  = int(os.environ.get("BRIDGE_POLL", "10"))
CURSOR_F   = "bridge_cursor.json"
SINCE_DATE = "27-Aug-2026"  # Only scan recent emails

def load_cursor():
    try: return json.load(open(CURSOR_F))
    except: return {"last_uid": 0, "uidvalidity": None, "seen": [], "last_room_seq": 0, "startup_seq": None}

def save_cursor(c):
    tmp = CURSOR_F + ".tmp"
    with open(tmp,"w") as f: json.dump(c,f)
    os.replace(tmp, CURSOR_F)

def room_get(path):
    req = urllib.request.Request(f"{ROOM_URL}{path}",
          headers={"Authorization": f"Bearer {CLAUDE_KEY}"})
    try:
        r = urllib.request.urlopen(req, timeout=5)
        return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

def room_post(path, body, key=None):
    data = json.dumps(body).encode()
    req  = urllib.request.Request(f"{ROOM_URL}{path}", data=data, method="POST",
           headers={"Authorization": f"Bearer {key or BRIDGE_KEY}",
                    "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=5)
        return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

def parse_bridge(body):
    if "SPHERA-BRIDGE" not in body: return None
    if "SPHERA-OUTBOUND" in body:   return None
    try:
        s = body.index("SPHERA-BRIDGE") + len("SPHERA-BRIDGE")
        e = body.index("END-SPHERA-BRIDGE")
        return json.loads(body[s:e].strip())
    except: return None

def gmail_fetch(cursor):
    if not GMAIL_PASS: return []
    results = []
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(GMAIL_USER, GMAIL_PASS)
        M.select("INBOX")
        _, data = M.status("INBOX", "(UIDVALIDITY)")
        uidvalidity = int(data[0].decode().split("UIDVALIDITY")[1].strip().rstrip(")"))
        if cursor["uidvalidity"] != uidvalidity:
            print(f"[bridge] UIDVALIDITY changed → resetting")
            cursor["uidvalidity"] = uidvalidity
            cursor["last_uid"] = 0
        _, uid_data = M.uid("SEARCH", None, "SINCE", SINCE_DATE)
        all_uids = uid_data[0].split() if uid_data[0] else []
        new_uids = [u for u in all_uids if int(u) > cursor["last_uid"]]
        for uid_b in new_uids:
            uid_int = int(uid_b)
            _, data = M.uid("FETCH", uid_b, "(RFC822)")
            if not data or not data[0]: continue
            raw  = email.message_from_bytes(data[0][1])
            body = ""
            if raw.is_multipart():
                for part in raw.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8","ignore")
                        break
            else:
                body = raw.get_payload(decode=True).decode("utf-8","ignore")
            parsed = parse_bridge(body)
            if parsed:
                src_id = f"gmail-uid-{uidvalidity}-{uid_int}"
                results.append({
                    "uid": uid_int,
                    "src_id": src_id,
                    "principal": parsed.get("principal","unknown"),
                    "content":   parsed.get("content",""),
                    "ts":        parsed.get("ts", datetime.now(timezone.utc).isoformat())
                })
        M.logout()
    except Exception as e:
        print(f"[bridge] imap error: {e}")
    return results

def smtp_send(subject, body):
    if not GMAIL_PASS: return False
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
        print(f"[bridge] smtp error: {e}")
        return False

def format_outbound(ev):
    return (
        "SPHERA-OUTBOUND\nSPHERA-BRIDGE\n"
        + json.dumps({"seq": ev.get("seq"), "principal": ev.get("principal"),
                      "type": ev.get("type"), "content": ev.get("content",""),
                      "ts": ev.get("ts")}, indent=2)
        + "\nEND-SPHERA-BRIDGE"
    )

def run():
    cursor = load_cursor()

    # Set outbound baseline on fresh start
    if cursor["startup_seq"] is None:
        r = room_get("/room")
        cursor["startup_seq"]   = r.get("last_seq", 0)
        cursor["last_room_seq"] = cursor["startup_seq"]
        save_cursor(cursor)
        print(f"[bridge] outbound baseline: seq:{cursor['startup_seq']}")

    print(f"[bridge] v1.0 | room:{ROOM_URL} | poll:{POLL_SECS}s")
    print(f"[bridge] uid:{cursor['last_uid']} seq:{cursor['last_room_seq']}")

    while True:
        try:
            # INBOUND: Gmail → Room
            msgs = gmail_fetch(cursor)
            for msg in msgs:
                src_id = msg["src_id"]
                if src_id in cursor["seen"]:
                    cursor["last_uid"] = max(cursor["last_uid"], msg["uid"])
                    continue
                r = room_post("/bridge/ingest", {
                    "principal":         msg["principal"],
                    "content":           msg["content"],
                    "source_message_id": src_id,
                    "transport":         "gmail",
                    "original_ts":       msg["ts"]
                })
                if r.get("seq") or r.get("duplicate"):
                    print(f"[bridge] Gmail→Room: {src_id[:40]} from {msg['principal']}: {msg['content'][:50]}")
                    cursor["seen"].append(src_id)
                    if len(cursor["seen"]) > 500: cursor["seen"] = cursor["seen"][-250:]
                    cursor["last_uid"] = max(cursor["last_uid"], msg["uid"])

            # OUTBOUND: Room → Gmail
            r = room_get(f"/events?after={cursor['last_room_seq']}")
            for ev in r.get("events", []):
                seq = ev["seq"]
                if ev.get("transport_provenance") or ev.get("transport") == "gmail":
                    cursor["last_room_seq"] = seq
                    continue
                content = str(ev.get("content", ev.get("objective", "")))
                if not content:
                    cursor["last_room_seq"] = seq
                    continue
                if smtp_send("SPHERA ROOM V0", format_outbound(ev)):
                    print(f"[bridge] Room→Gmail: seq:{seq} ({ev['principal']}/{ev['type']})")
                cursor["last_room_seq"] = seq

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
    r = room_get("/health")
    if not r.get("ok"):
        print(f"[bridge] room unreachable: {r}"); sys.exit(1)
    print(f"[bridge] room OK: seq:{r['last_seq']} events:{r['event_count']}")
    run()
