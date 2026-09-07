"""
SPHERA Northbound Ingress v1.0
Polls Gmail for SPHERA-NORTHBOUND command envelopes from authorised issuers.
Validates, dedupes, and injects approved work items into the local room.
Boss is never the courier.

Command envelope format (sent by Soba via Gmail):
  Subject: SPHERA ROOM V0
  Body:
    SPHERA-NORTHBOUND
    {"issuer":"soba","target_edge":"claude-code-local-01","work_id":"<uuid>",
     "mission_id":"<uuid>","instruction":"python: ...","approval_state":"APPROVED",
     "nonce":"<hex>","capability":"python_execution"}
    END-SPHERA-NORTHBOUND
"""
import json, os, sys, time, uuid, imaplib, email
import urllib.request, urllib.error
from datetime import datetime, timezone

SPHERA_URL   = os.environ.get("SPHERA_URL",    "http://localhost:8765")
ARCIDES_KEY  = os.environ.get("ARCIDES_KEY",   "ak-sphera")
BRIDGE_KEY   = os.environ.get("BRIDGE_KEY",    "br-sphera")
GMAIL_USER   = os.environ.get("GMAIL_USER",    "gabeng.angola@gmail.com")
GMAIL_PASS   = os.environ.get("GMAIL_APP_PASS","")
POLL_SECS    = int(os.environ.get("NB_POLL",   "15"))
CURSOR_FILE  = "nb_cursor.json"
SEEN_FILE    = "nb_seen.json"

# Authorised issuers — only these can submit northbound commands
AUTHORISED_ISSUERS = {"soba", "claude", "coda"}
# Allowed target edges
ALLOWED_EDGES = {"claude-code-local-01"}

def utcnow(): return datetime.now(timezone.utc).isoformat()

def load_seen():
    try: return set(json.load(open(SEEN_FILE)))
    except: return set()

def save_seen(seen):
    tmp = SEEN_FILE + ".tmp"
    with open(tmp,"w") as f: json.dump(list(seen)[-1000:], f)
    os.replace(tmp, SEEN_FILE)

def load_cursor():
    try: return json.load(open(CURSOR_FILE))
    except: return {"last_uid": 0, "uidvalidity": None}

def save_cursor(c):
    tmp = CURSOR_FILE + ".tmp"
    with open(tmp,"w") as f: json.dump(c, f)
    os.replace(tmp, CURSOR_FILE)

def room(method, path, body=None, key=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        f"{SPHERA_URL}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {key or ARCIDES_KEY}",
                 "Content-Type": "application/json"}
    )
    try:
        r = urllib.request.urlopen(req, timeout=10)
        return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try: return json.loads(e.read())
        except: return {"error": e.code}
    except Exception as e:
        return {"error": str(e)}

# Trigger registry file — Boss pre-registers triggers before Soba sends them
TRIGGER_REGISTRY_FILE = "nb_triggers.json"

def load_triggers():
    try: return json.load(open(TRIGGER_REGISTRY_FILE))
    except: return {}

def save_triggers(triggers):
    tmp = TRIGGER_REGISTRY_FILE + ".tmp"
    with open(tmp,"w") as f: json.dump(triggers, f, indent=2)
    os.replace(tmp, TRIGGER_REGISTRY_FILE)

# Generic action whitelist — no pre-registration needed
GENERIC_ACTIONS = {
    "list_dir":    {"required_args": 1, "param_key": "path"},
    "read_file":   {"required_args": 1, "param_key": "path"},
    "nonce_probe": {"required_args": 1, "param_key": "nonce_value"},
    "run_script":  {"required_args": 1, "param_key": "script_name"},
}

def parse_northbound(body: str) -> dict | None:
    """
    Parse SPHERA-NORTHBOUND envelope OR generic SPHERA RUN command.
    
    Generic format (no pre-registration needed):
      SPHERA RUN list_dir local
      SPHERA RUN read_file local/server.py
      SPHERA RUN nonce_probe MYTOKEN
      SPHERA RUN run_script test_pea.py
    
    Falls back to trigger registry for named keys.
    Falls back to structured envelope.
    """
    import re, secrets
    
    # Try generic SPHERA RUN [action] [arg]
    match = re.search(r'SPHERA RUN ([A-Za-z0-9_-]+)(?:\s+([^\n]+))?', body)
    if match:
        token = match.group(1)
        arg   = (match.group(2) or "").strip()
        
        # Check if it's a generic action
        if token in GENERIC_ACTIONS:
            spec = GENERIC_ACTIONS[token]
            params = {spec["param_key"]: arg} if arg else {}
            nonce  = secrets.token_hex(8)
            print(f"[nb] generic action: {token} arg={arg!r}")
            return {
                "issuer":         "soba",  # default issuer for generic commands
                "target_edge":    "claude-code-local-01",
                "mission_id":     None,    # will auto-resolve below
                "action":         token,
                "params":         params,
                "approval_state": "APPROVED",
                "nonce":          nonce,
                "capability":     "python_execution",
                "_generic":       True
            }
        
        # Try named trigger registry
        triggers = load_triggers()
        if token in triggers:
            t = triggers[token]
            print(f"[nb] named trigger matched: {token}")
            return t
        
        print(f"[nb] unknown token {token!r} — not in generic actions or trigger registry")
        return None

    # Try SPHERA REPLY [work_id]: [answer] — Soba answers a checkpoint
    reply_match = re.search(r'SPHERA REPLY ([A-Za-z0-9_-]+):\s*(.+)', body, re.DOTALL)
    if reply_match:
        work_id = reply_match.group(1).strip()
        reply   = reply_match.group(2).strip()[:2000]
        import secrets as _sec
        print(f"[nb] checkpoint reply: work_id={work_id[:8]} reply={reply[:40]}")
        return {
            "issuer":         "soba",
            "target_edge":    "claude-code-local-01",
            "mission_id":     None,
            "action":         "checkpoint_reply",
            "params":         {"work_id": work_id, "reply": reply},
            "approval_state": "APPROVED",
            "nonce":          _sec.token_hex(8),
            "_checkpoint_reply": True
        }

    # Try structured SPHERA-NORTHBOUND envelope
    if "SPHERA-NORTHBOUND" not in body: return None
    try:
        s = body.index("SPHERA-NORTHBOUND") + len("SPHERA-NORTHBOUND")
        e = body.index("END-SPHERA-NORTHBOUND")
        return json.loads(body[s:e].strip())
    except Exception:
        return None

# Typed actions only — mirrors server.py DISPATCH_ACTIONS
NB_ALLOWED_ACTIONS = {"write_file","read_file","list_dir","run_script","nonce_probe"}

def validate_command(cmd: dict) -> tuple[bool, str]:
    """Validate northbound command envelope. Typed actions only — no free-form instruction."""
    issuer = cmd.get("issuer","").lower()
    if issuer not in AUTHORISED_ISSUERS:
        return False, f"unauthorised issuer: {issuer!r}"
    target = cmd.get("target_edge","")
    if target not in ALLOWED_EDGES:
        return False, f"target edge not allowed: {target!r}"
    if cmd.get("approval_state","").upper() != "APPROVED":
        return False, "approval_state must be APPROVED"
    if not cmd.get("nonce"):
        return False, "nonce required"
    # Typed action required — no free-form instruction field
    action = cmd.get("action","")
    if not action:
        return False, "action required (no free-form instruction accepted)"
    if action not in NB_ALLOWED_ACTIONS:
        return False, f"unknown action {action!r}. allowed: {sorted(NB_ALLOWED_ACTIONS)}"
    params = cmd.get("params", {})
    if not isinstance(params, dict):
        return False, "params must be a dict"
    return True, "ok"

def inject_work(cmd: dict, seen: set) -> tuple[bool, str]:
    """Create a work item in the room from validated northbound command."""
    work_id   = cmd.get("work_id") or str(uuid.uuid4())
    nonce     = cmd.get("nonce","")
    dedup_key = f"{work_id}:{nonce}"

    # Dedup by nonce alone — work_id changes each run for prose triggers
    nonce_key = f"nonce:{nonce}"
    if nonce_key in seen or dedup_key in seen:
        return False, f"duplicate nonce: {nonce}"
    seen.add(nonce_key)

    mission_id = cmd.get("mission_id")
    if not mission_id:
        if cmd.get("_generic"):
            # Auto-create a short-lived mission for generic actions
            r = room("POST", "/mission",
                     {"objective": f"Generic {cmd.get('action')} by {cmd.get('issuer','unknown')}"},
                     key=ARCIDES_KEY)
            mission_id = r.get("mission_id")
            if not mission_id:
                return False, f"failed to auto-create mission: {r}"
            print(f"[nb] auto-created mission {mission_id[:8]} for generic action")
        else:
            return False, "mission_id required — register trigger with mission_id via nb_triggers.json"

    # Create work item with typed action (serialised as JSON description)
    import json as _json
    typed_desc = _json.dumps({"action": cmd.get("action"), "params": cmd.get("params", {})})
    r = room("POST", f"/mission/{mission_id}/work", {
        "description": typed_desc,
        "capability":  "python_execution",
        "approval_state": "APPROVED",
        "issuer_principal": cmd.get("issuer","soba"),
        "target_edge_id": cmd.get("target_edge","claude-code-local-01"),
        "nonce": nonce,
    }, key=ARCIDES_KEY)

    actual_work_id = r.get("work_id")
    if not actual_work_id:
        return False, f"failed to create work: {r}"

    seen.add(dedup_key)
    print(f"[nb] injected work {actual_work_id[:8]} from {cmd.get('issuer')} → {cmd.get('target_edge')}")
    print(f"[nb] instruction: {cmd.get('instruction','')[:60]}")
    return True, actual_work_id

def poll_gmail(cursor, seen):
    """Poll Gmail for SPHERA-NORTHBOUND envelopes."""
    if not GMAIL_PASS: return cursor
    new_commands = 0
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(GMAIL_USER, GMAIL_PASS)
        M.select("INBOX")
        _, data = M.status("INBOX", "(UIDVALIDITY)")
        uidvalidity = int(data[0].decode().split("UIDVALIDITY")[1].strip().rstrip(")"))
        if cursor["uidvalidity"] != uidvalidity:
            print("[nb] UIDVALIDITY changed — resetting")
            cursor["uidvalidity"] = uidvalidity
            cursor["last_uid"] = 0
        _, uid_data = M.uid("SEARCH", None, "SINCE", "01-Sep-2026")
        all_uids = uid_data[0].split() if uid_data[0] else []
        new_uids = [u for u in all_uids if int(u if isinstance(u, int) else u.decode()) > cursor["last_uid"]]
        for uid_b in new_uids:
            uid_int = int(uid_b if isinstance(uid_b, int) else uid_b.decode())
            _, data = M.uid("FETCH", uid_b if isinstance(uid_b, bytes) else str(uid_b).encode(), "(RFC822)")
            if not data or not data[0]: continue
            raw  = email.message_from_bytes(data[0][1])
            body = ""
            if raw.is_multipart():
                for part in raw.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode("utf-8","ignore"); break
            else:
                body = raw.get_payload(decode=True).decode("utf-8","ignore")
            cmd = parse_northbound(body)
            if cmd:
                ok, reason = validate_command(cmd)
                if ok:
                    injected, result = inject_work(cmd, seen)
                    if injected:
                        new_commands += 1
                else:
                    print(f"[nb] rejected command: {reason}")
            cursor["last_uid"] = max(cursor["last_uid"], uid_int)
        M.logout()
    except imaplib.IMAP4.abort as e:
        print(f"[nb] imap connection reset: {e} — will retry next poll")
    except Exception as e:
        print(f"[nb] gmail error: {e}")
    return cursor

# Drive outbox file IDs to watch — keyed by edge_id
DRIVE_OUTBOXES = {
    "anish-apps-script-edge-01": os.environ.get("ANISH_OUTBOX_FILE_ID", "")
}

def poll_drive_outboxes(cursor, seen):
    """
    Poll Google Drive outbox files for ANISH_APPS_SCRIPT_EDGE results.
    Injects new work results into SPHERA room.
    Requires GOOGLE_DRIVE_TOKEN or service account — skips if not configured.
    """
    token = os.environ.get("GOOGLE_DRIVE_TOKEN", "")
    if not token:
        return cursor  # Not configured yet

    for edge_id, file_id in DRIVE_OUTBOXES.items():
        if not file_id:
            continue
        try:
            # Check if file has been modified since last poll
            import urllib.request as _ur
            meta_req = _ur.Request(
                f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=modifiedTime",
                headers={"Authorization": f"Bearer {token}"}
            )
            meta = json.loads(_ur.urlopen(meta_req, timeout=5).read())
            modified = meta.get("modifiedTime", "")
            last_seen = cursor.get(file_id, "")

            if modified == last_seen:
                continue  # No change

            # Read file content
            content_req = _ur.Request(
                f"https://www.googleapis.com/drive/v3/files/{file_id}/export?mimeType=text/plain",
                headers={"Authorization": f"Bearer {token}"}
            )
            content = _ur.urlopen(content_req, timeout=5).read().decode("utf-8", "ignore")

            # Parse work_item_id blocks from outbox
            import re
            blocks = re.findall(
                r'\[ANISH_APPS_SCRIPT_EDGE.*?\](.*?)(?=\[ANISH_APPS_SCRIPT_EDGE|$)',
                content, re.DOTALL
            )
            for block in blocks:
                # Extract work_item_id from block
                wid_match = re.search(r'work_item_id:\s*([\w-]+)', block)
                work_item_id = wid_match.group(1) if wid_match else None
                nonce_key = f"drive:{file_id}:{work_item_id or hash(block[:50])}"
                if nonce_key in seen:
                    continue
                # Inject into room
                result_text = block.strip()[:2000]
                r = room("POST", "/bridge/ingest", {
                    "principal":         "claude",
                    "content":           result_text,
                    "source_message_id": nonce_key,
                    "transport":         "google-drive",
                    "provenance": {
                        "edge_id":           edge_id,
                        "delegated_for":     "anish",
                        "continuity_class":  "scheduled_context_reconstruction"
                    }
                }, key=BRIDGE_KEY)
                if r.get("seq") or r.get("duplicate"):
                    print(f"[nb] Drive outbox ingested: {edge_id} work_item={work_item_id}")
                    seen.add(nonce_key)

            cursor[file_id] = modified

        except Exception as e:
            print(f"[nb] drive poll error for {edge_id}: {e}")

    return cursor

def run():
    print("[nb] SPHERA Northbound Ingress v1.0")
    print(f"[nb] room: {SPHERA_URL} | poll: {POLL_SECS}s")
    print(f"[nb] authorised issuers: {AUTHORISED_ISSUERS}")
    print(f"[nb] allowed edges: {ALLOWED_EDGES}")

    h = room("GET", "/health")
    if not h.get("ok"):
        print(f"[nb] ERROR: room unreachable: {h}"); sys.exit(1)
    print(f"[nb] room OK: seq:{h.get('last_seq',0)}")

    cursor = load_cursor()
    seen   = load_seen()

    # Drive outbox polling state
    drive_cursor = {}  # file_id -> last_modified_time seen

    while True:
        try:
            # Poll Gmail for SPHERA-NORTHBOUND commands
            cursor = poll_gmail(cursor, seen)
            save_cursor(cursor)
            save_seen(seen)

            # Poll Drive outboxes for ANISH_APPS_SCRIPT_EDGE results
            drive_cursor = poll_drive_outboxes(drive_cursor, seen)

        except KeyboardInterrupt:
            print("\n[nb] stopped.")
            break
        except Exception as e:
            print(f"[nb] error: {e}")
        time.sleep(POLL_SECS)

if __name__ == "__main__":
    run()
