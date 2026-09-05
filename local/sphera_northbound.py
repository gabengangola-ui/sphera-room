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
AUTHORISED_ISSUERS = {"soba", "claude"}
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

def parse_northbound(body: str) -> dict | None:
    """
    Parse SPHERA-NORTHBOUND envelope OR simple prose trigger.
    Prose format: 'SPHERA RUN <trigger_key>' anywhere in email body.
    Trigger key is looked up in nb_triggers.json (pre-registered by Boss or Claude).
    This allows Soba to send ordinary prose — no JSON envelope needed.
    """
    # Try prose trigger first: "SPHERA RUN <key>"
    import re
    match = re.search(r'SPHERA RUN ([A-Za-z0-9_-]+)', body)
    if match:
        trigger_key = match.group(1)
        triggers = load_triggers()
        if trigger_key in triggers:
            t = triggers[trigger_key]
            print(f"[nb] prose trigger matched: {trigger_key}")
            return t
        else:
            print(f"[nb] prose trigger {trigger_key!r} not found in registry")
            return None

    # Try structured envelope
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

    if dedup_key in seen:
        return False, f"duplicate work_id/nonce: {dedup_key}"

    mission_id = cmd.get("mission_id")
    if not mission_id:
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
    except Exception as e:
        print(f"[nb] gmail error: {e}")
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

    while True:
        try:
            cursor = poll_gmail(cursor, seen)
            save_cursor(cursor)
            save_seen(seen)
        except KeyboardInterrupt:
            print("\n[nb] stopped.")
            break
        except Exception as e:
            print(f"[nb] error: {e}")
        time.sleep(POLL_SECS)

if __name__ == "__main__":
    run()
