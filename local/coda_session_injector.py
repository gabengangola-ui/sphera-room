"""
SPHERA Coda Session Injector
Polls room for instructions addressed to Coda, injects them into the live
Claude Code session via `claude --resume [session_id] --print [instruction]`
Coda's actual intelligence processes and replies — no faking.
"""
import json, os, sys, time, uuid, subprocess, hashlib
import urllib.request, urllib.error

SPHERA_URL  = os.environ.get("SPHERA_URL",    "http://localhost:8765")
BRIDGE_KEY  = os.environ.get("BRIDGE_KEY",    "br-sphera")
WORKER_KEY  = os.environ.get("WORKER_KEY",    "ck-sphera")
SESSION_ID  = os.environ.get("CODA_SESSION",  "cf7239f2-e00a-4878-bdc7-41d0708946d0")
POLL_SECS   = int(os.environ.get("POLL_SECS", "10"))
CURSOR_FILE = "injector_cursor.json"
SEEN_FILE   = "injector_seen.json"

def load_cursor():
    try: return json.load(open(CURSOR_FILE))["seq"]
    except: return 0

def save_cursor(seq):
    with open(CURSOR_FILE + ".tmp", "w") as f: json.dump({"seq": seq}, f)
    os.replace(CURSOR_FILE + ".tmp", CURSOR_FILE)

def load_seen():
    try: return set(json.load(open(SEEN_FILE)))
    except: return set()

def save_seen(seen):
    with open(SEEN_FILE + ".tmp", "w") as f: json.dump(list(seen)[-500:], f)
    os.replace(SEEN_FILE + ".tmp", SEEN_FILE)

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
        "source_message_id": f"coda-injector-{uuid.uuid4()}",
        "transport":         "coda-session-injector",
        "provenance":        {"edge_id": "coda-bridge-v2",
                              "continuity_class": "subordinate_worker"}
    }, key=BRIDGE_KEY)

def inject_into_coda(instruction, seq):
    """
    Inject instruction into live Coda Claude Code session via --resume.
    Claude Code processes it with real intelligence and returns output.
    """
    print(f"[injector] injecting into session {SESSION_ID[:8]}...: {instruction[:80]}")

    # Build the prompt for Coda
    prompt = (
        f"[SPHERA room instruction at seq:{seq} from Soba]\n\n"
        f"{instruction}\n\n"
        f"Reply in the SPHERA room via POST to {SPHERA_URL}/bridge/ingest "
        f"with your response. Use Authorization: Bearer {BRIDGE_KEY}. "
        f"Include principal=claude, your actual response as content."
    )

    # Run claude --resume with the instruction
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[injector] ERROR: ANTHROPIC_API_KEY not set")
        post_to_room("[coda] Cannot process: ANTHROPIC_API_KEY not configured.")
        return

    try:
        result = subprocess.run(
            ["claude", "--resume", SESSION_ID, "--print", prompt],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "ANTHROPIC_API_KEY": api_key}
        )
        output = result.stdout.strip()
        if output:
            print(f"[injector] Coda responded: {output[:100]}")
            # Post Coda's actual response to room
            post_to_room(f"[coda] {output}")
        elif result.returncode != 0:
            print(f"[injector] claude error: {result.stderr[:200]}")
            post_to_room(f"[coda] Error processing instruction: {result.stderr[:200]}")
    except FileNotFoundError:
        print("[injector] claude CLI not found")
        post_to_room("[coda] claude CLI not available in this session.")
    except subprocess.TimeoutExpired:
        print("[injector] timeout")
        post_to_room("[coda] Processing timed out.")
    except Exception as e:
        print(f"[injector] error: {e}")
        post_to_room(f"[coda] Error: {e}")

def is_for_coda(content):
    """Check if a room message is addressed to Coda."""
    c = str(content).lower()
    return "coda" in c and any(k in c for k in [
        "reply", "respond", "tell", "write", "say", "post",
        "process", "execute", "run", "do ", "please"
    ])

def run():
    print("[injector] SPHERA Coda Session Injector")
    print(f"[injector] session={SESSION_ID[:8]}... room={SPHERA_URL}")

    h = room("GET", "/health")
    if not h.get("ok"):
        print(f"[injector] room unreachable: {h}"); sys.exit(1)
    print(f"[injector] room OK seq:{h.get('last_seq',0)}")

    cursor = load_cursor() or h.get("last_seq", 0)
    seen   = load_seen()
    print(f"[injector] polling from seq:{cursor}")

    while True:
        try:
            r = room("GET", f"/events?after={cursor}")
            for ev in r.get("events", []):
                seq = ev.get("seq", cursor)
                cursor = max(cursor, seq)

                if seq in seen:
                    continue

                # Skip own messages
                if ev.get("principal") in ("claude", "system", "arcides"):
                    seen.add(seq)
                    continue

                etype = ev.get("type", "")
                if etype not in ("message",):
                    seen.add(seq)
                    continue

                payload = ev.get("payload_json", {})
                if isinstance(payload, str):
                    try: payload = json.loads(payload)
                    except: seen.add(seq); continue

                content = payload.get("content", "")
                if isinstance(content, dict):
                    content = content.get("content", str(content))

                if is_for_coda(content):
                    seen.add(seq)
                    save_seen(seen)
                    inject_into_coda(str(content), seq)

            save_cursor(cursor)
            save_seen(seen)

        except KeyboardInterrupt:
            print("\n[injector] stopped")
            save_cursor(cursor)
            save_seen(seen)
            break
        except Exception as e:
            print(f"[injector] loop error: {e}")

        time.sleep(POLL_SECS)

if __name__ == "__main__":
    run()
