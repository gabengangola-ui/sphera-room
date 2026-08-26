"""
SPHERA Gmail Bridge Daemon v0.5
Soba review fixes:
1. Fresh cursor does not replay historical events (startup_seq tracks where to start outbound)
2. Log actual source_id per message, not cursor last_uid
3. last_uid advances only after confirmed ingest
4. Outbound strictly ordered - seq N+1 blocked until N succeeds
5. Single-instance PID lock
6. Atomic cursor persistence (write temp + rename)
"""
import fcntl, json, os, sys, time, imaplib, smtplib, email, email.mime.text
import urllib.request, tempfile
from datetime import datetime, timezone

ROOM_URL    = os.environ.get("SPHERA_URL",     "http://localhost:8765")
CLAUDE_KEY  = os.environ.get("CLAUDE_KEY",     "ck-sphera")
BRIDGE_KEY  = os.environ.get("BRIDGE_KEY",     "br-sphera")
GMAIL_USER  = os.environ.get("GMAIL_USER",     "gabeng.angola@gmail.com")
GMAIL_PASS  = os.environ.get("GMAIL_APP_PASS", "")
POLL_SECS   = int(os.environ.get("BRIDGE_POLL",   "10"))
CURSOR_FILE = os.environ.get("BRIDGE_CURSOR",  "bridge_cursor.json")
LOCK_FILE   = os.environ.get("BRIDGE_LOCK",    "bridge.lock")
MAX_RETRY   = 3

# ── FIX 5: Single-instance PID lock ──────────────────────────────────────────
def acquire_lock():
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
        return lock_fd
    except OSError:
        print(f"[bridge] ERROR: another instance is running (lock: {LOCK_FILE}). Exiting.")
        sys.exit(1)

# ── FIX 6: Atomic cursor persistence ─────────────────────────────────────────
def load_cursor():
    try:
        return json.load(open(CURSOR_FILE))
    except:
        return {
            "last_uid":        0,
            "uidvalidity":     None,
            "seen_source_ids": [],
            "last_room_seq":   0,
            "startup_seq":     None,   # FIX 1: outbound starts from here, not seq 0
            "unsent_seqs":     [],
            "pending_ingest":  []      # FIX 3: source_ids awaiting confirmed ingest
        }

def save_cursor(c):
    # FIX 6: atomic write — temp file + fsync + rename
    tmp = CURSOR_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CURSOR_FILE)

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

# ── Parse bridge message ──────────────────────────────────────────────────────
def parse_bridge(body):
    if "SPHERA-BRIDGE" not in body: return None
    if "SPHERA-OUTBOUND" in body:   return None
    try:
        s = body.index("SPHERA-BRIDGE") + len("SPHERA-BRIDGE")
        e = body.index("END-SPHERA-BRIDGE")
        return json.loads(body[s:e].strip())
    except:
        return None

# ── Format outbound ───────────────────────────────────────────────────────────
def format_outbound(event):
    return (
        "SPHERA-OUTBOUND\nSPHERA-BRIDGE\n"
        + json.dumps({"seq": event.get("seq"), "principal": event.get("principal"),
                      "type": event.get("type"), "content": event.get("content",""),
                      "ts": event.get("ts"), "transport_provenance": "sphera-room-v2"}, indent=2)
        + "\nEND-SPHERA-BRIDGE"
    )

# ── Gmail IMAP ────────────────────────────────────────────────────────────────
def gmail_fetch_new(cursor):
    if not GMAIL_PASS: return []
    msgs = []
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(GMAIL_USER, GMAIL_PASS)
        M.select("INBOX")
        # Check UIDVALIDITY
        _, data = M.status("INBOX", "(UIDVALIDITY)")
        uidvalidity = int(data[0].decode().split("UIDVALIDITY")[1].strip().rstrip(")"))
        if cursor["uidvalidity"] != uidvalidity:
            print(f"[bridge] UIDVALIDITY changed → resetting UID cursor")
            cursor["uidvalidity"] = uidvalidity
            cursor["last_uid"]    = 0
        # Fetch UIDs > last known
        _, uid_data = M.uid("SEARCH", None, "ALL")
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
                        body = part.get_payload(decode=True).decode("utf-8","ignore"); break
            else:
                body = raw.get_payload(decode=True).decode("utf-8","ignore")
            parsed = parse_bridge(body)
            if parsed:
                src_id = f"gmail-uid-{uidvalidity}-{uid_int}"
                msgs.append({"source_id": src_id, "uid": uid_int,
                             "principal": parsed.get("principal","soba"),
                             "content":   parsed.get("content",""),
                             "ts":        parsed.get("ts", datetime.now(timezone.utc).isoformat())})
            # FIX 2: track per-message uid, advance ONLY after confirmed ingest below
        M.logout()
        return msgs, new_uids
    except Exception as e:
        print(f"[bridge] imap error: {e}")
        return [], []

# ── SMTP ──────────────────────────────────────────────────────────────────────
def smtp_send(subject, body, retry=MAX_RETRY):
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
            time.sleep(2**attempt)
    return False

# ── Main loop ─────────────────────────────────────────────────────────────────
def run():
    lock_fd = acquire_lock()  # FIX 5
    cursor  = load_cursor()

    # FIX 1: on fresh start, record current room seq as outbound baseline
    # so we never resend historical events
    if cursor["startup_seq"] is None:
        r = room("GET", "/room")
        cursor["startup_seq"]   = r.get("last_seq", 0)
        cursor["last_room_seq"] = cursor["startup_seq"]
        save_cursor(cursor)
        print(f"[bridge] fresh start — outbound baseline set to seq:{cursor['startup_seq']}")

    print(f"[bridge] v0.5 | room:{ROOM_URL} | poll:{POLL_SECS}s")
    print(f"[bridge] uid:{cursor['last_uid']} seq:{cursor['last_room_seq']} seen:{len(cursor['seen_source_ids'])}")

    try:
        while True:
            # ── INBOUND: Gmail → Room ─────────────────────────────────────
            result = gmail_fetch_new(cursor)
            msgs, new_uids = result if isinstance(result, tuple) else (result, [])

            for msg in msgs:
                src_id = msg["source_id"]
                if src_id in cursor["seen_source_ids"]: continue

                r = room("POST", "/bridge/ingest", {
                    "principal":         msg["principal"],
                    "content":           msg["content"],
                    "source_message_id": src_id,
                    "transport":         "gmail",
                    "original_ts":       msg["ts"]
                }, key=BRIDGE_KEY)

                if r.get("seq") or r.get("duplicate"):
                    # FIX 2: log actual source_id
                    print(f"[bridge] Gmail→Room: {src_id} from {msg['principal']}: {msg['content'][:50]}")
                    cursor["seen_source_ids"].append(src_id)
                    if len(cursor["seen_source_ids"]) > 1000:
                        cursor["seen_source_ids"] = cursor["seen_source_ids"][-500:]
                    # FIX 3: advance last_uid ONLY after confirmed ingest
                    cursor["last_uid"] = max(cursor["last_uid"], msg["uid"])
                else:
                    print(f"[bridge] ingest failed for {src_id} — will retry (last_uid NOT advanced)")
                    # Do not advance last_uid — message will be retried next poll

            # ── OUTBOUND: Room → Gmail (strictly ordered) ─────────────────
            r = room("GET", f"/events?after={cursor['last_room_seq']}")
            events = r.get("events", [])

            for ev in events:
                seq     = ev["seq"]
                content = str(ev.get("content",""))

                # Skip bridge-originated (loop prevention)
                if ev.get("transport_provenance"): 
                    cursor["last_room_seq"] = seq
                    continue

                # FIX 4: strictly ordered — if unsent_seqs has anything, retry those first
                if cursor["unsent_seqs"] and seq > min(cursor["unsent_seqs"]):
                    print(f"[bridge] blocked at seq:{seq} — must retry unsent:{cursor['unsent_seqs']} first")
                    break

                sent = smtp_send("SPHERA ROOM V0", format_outbound(ev))
                if sent:
                    print(f"[bridge] Room→Gmail: seq:{seq} ({ev['principal']}/{ev['type']})")
                    cursor["last_room_seq"] = seq
                    cursor["unsent_seqs"]   = [s for s in cursor["unsent_seqs"] if s != seq]
                else:
                    print(f"[bridge] SMTP failed seq:{seq} — queued, blocking further outbound")
                    if seq not in cursor["unsent_seqs"]:
                        cursor["unsent_seqs"].append(seq)
                    break  # FIX 4: stop processing — do not advance past failed seq

            # Retry unsent (in order)
            for seq in sorted(cursor["unsent_seqs"]):
                r2 = room("GET", f"/events?after={seq-1}")
                evs = [e for e in r2.get("events",[]) if e["seq"]==seq]
                if evs and smtp_send("SPHERA ROOM V0", format_outbound(evs[0])):
                    print(f"[bridge] retry OK: seq:{seq}")
                    cursor["unsent_seqs"].remove(seq)
                    cursor["last_room_seq"] = max(cursor["last_room_seq"], seq)

            save_cursor(cursor)  # FIX 6: atomic
            time.sleep(POLL_SECS)

    except KeyboardInterrupt:
        print("\n[bridge] stopped.")
    finally:
        save_cursor(cursor)
        lock_fd.close()
        try: os.remove(LOCK_FILE)
        except: pass

if __name__ == "__main__":
    r = room("GET", "/health")
    if not r.get("ok"):
        print(f"[bridge] room unreachable: {r}"); sys.exit(1)
    print(f"[bridge] room OK: seq:{r['last_seq']} events:{r['event_count']}")
    run()
