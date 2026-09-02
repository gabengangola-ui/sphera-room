"""
SPHERA Claude Code Worker v1.0
Local execution edge: claude-code-local-01
Continuity class: subordinate_worker (NOT native Claude)

Polls SPHERA room for approved work items assigned to this edge.
Executes via Claude Code (claude --print) or direct Python subprocess.
Posts results back to room.

Commands supported: EXECUTE, PAUSE, CANCEL, STATUS
Work envelope: work_id, mission_id, issuer_principal, target_edge_id,
               instruction, priority, approval_state, created_at
"""
import json, os, sys, time, uuid, subprocess, hashlib
import urllib.request, urllib.error
from datetime import datetime, timezone

SPHERA_URL  = os.environ.get("SPHERA_URL",    "http://localhost:8765")
WORKER_KEY  = os.environ.get("WORKER_KEY",    os.environ.get("CLAUDE_KEY","ck-sphera"))
EDGE_ID     = os.environ.get("WORKER_EDGE_ID","claude-code-local-01")
POLL_SECS   = int(os.environ.get("WORKER_POLL","15"))
CAPABILITY  = "python_execution"
LEASE_SECS  = 300

def utcnow(): return datetime.now(timezone.utc).isoformat()

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
    except urllib.error.HTTPError as e:
        try: return json.loads(e.read())
        except: return {"error": e.code}
    except Exception as e:
        return {"error": str(e)}

def post_message(content):
    return room("POST", "/bridge/ingest", {
        "principal":         "claude",
        "content":           content,
        "source_message_id": f"ccw-{uuid.uuid4()}",
        "transport":         "claude-code-local",
        "provenance": {
            "principal_id":    "claude",
            "edge_id":         EDGE_ID,
            "continuity_class":"subordinate_worker"
        }
    })

def execute_instruction(instruction: str, work_id: str) -> dict:
    """
    Execute instruction via Claude Code (claude --print).
    Falls back to direct Python subprocess for simple python: prefix commands.
    Never self-identifies as native Claude.
    """
    # Safety: only APPROVED work reaches here
    if instruction.startswith("python:"):
        # Direct Python execution
        code = instruction[7:].strip()
        try:
            result = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True, text=True, timeout=60,
                cwd=os.getcwd()
            )
            return {
                "status":        "done" if result.returncode == 0 else "failed",
                "stdout":        result.stdout[:2000],
                "stderr":        result.stderr[:500],
                "exit_code":     result.returncode,
                "execution_mode":"python_direct",
                "edge_id":       EDGE_ID,
                "continuity_class":"subordinate_worker"
            }
        except subprocess.TimeoutExpired:
            return {"status":"failed","error":"timeout","edge_id":EDGE_ID}
        except Exception as e:
            return {"status":"failed","error":str(e),"edge_id":EDGE_ID}
    else:
        # Claude Code execution
        api_key = os.environ.get("ANTHROPIC_API_KEY","")
        if not api_key:
            return {"status":"failed","error":"ANTHROPIC_API_KEY not set","edge_id":EDGE_ID}
        try:
            result = subprocess.run(
                ["claude", "--print", instruction],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, "ANTHROPIC_API_KEY": api_key}
            )
            return {
                "status":        "done" if result.returncode == 0 else "failed",
                "output":        result.stdout[:3000],
                "stderr":        result.stderr[:500],
                "exit_code":     result.returncode,
                "execution_mode":"claude_code",
                "edge_id":       EDGE_ID,
                "continuity_class":"subordinate_worker"
            }
        except FileNotFoundError:
            return {"status":"failed","error":"claude CLI not found — run: npm install -g @anthropic-ai/claude-code","edge_id":EDGE_ID}
        except subprocess.TimeoutExpired:
            return {"status":"failed","error":"timeout after 120s","edge_id":EDGE_ID}
        except Exception as e:
            return {"status":"failed","error":str(e),"edge_id":EDGE_ID}

def register_edge():
    """Register this worker edge at startup."""
    room("POST", "/edge/register", {
        "edge_id":          EDGE_ID,
        "principal_id":     "claude",
        "surface":          "localhost-claude-code",
        "provider":         "anthropic",
        "capabilities":     ["python_execution","file_creation","shell"],
        "continuity_class": "subordinate_worker"
    }, key=os.environ.get("ARCIDES_KEY","ak-sphera"))

def poll_and_execute(cursor):
    """One poll cycle: find ready work, claim, execute, post result."""
    r = room("GET", f"/events?after={cursor}")
    events = r.get("events", [])
    new_cursor = cursor

    for ev in events:
        new_cursor = ev.get("seq", new_cursor)
        if ev.get("type") not in ("work_created", "work_unblocked"):
            continue

        payload = ev.get("payload_json", {})
        if isinstance(payload, str):
            try: payload = json.loads(payload)
            except: continue

        work_id    = payload.get("work_id")
        capability = payload.get("capability")
        if not work_id or capability != CAPABILITY:
            continue

        # Claim the work item
        claim = room("POST", f"/work/{work_id}/claim", {"lease_seconds": LEASE_SECS})
        lid   = claim.get("lease_id")
        if not lid:
            continue  # Already claimed

        instruction = payload.get("instruction") or payload.get("description","")
        approval    = payload.get("approval_state","APPROVED")

        if approval not in ("APPROVED", "approved"):
            # Escalate — do not execute unapproved work
            room("POST", f"/work/{work_id}/result", {
                "lease_id": lid,
                "result": {"status":"escalated","reason":"approval_required","edge_id":EDGE_ID}
            })
            post_message(f"[claude-code-local-01] Work {work_id[:8]} requires approval before execution.")
            continue

        print(f"[ccw] executing: {work_id[:8]} | {instruction[:60]}")
        result = execute_instruction(instruction, work_id)

        room("POST", f"/work/{work_id}/result", {"lease_id": lid, "result": result})

        status = result.get("status","?")
        print(f"[ccw] result: {work_id[:8]} → {status}")
        post_message(
            f"[claude-code-local-01] Work {work_id[:8]} {status}. "
            f"Mode: {result.get('execution_mode','?')}. "
            f"Output: {str(result.get('output') or result.get('stdout',''))[:200]}"
        )

    return new_cursor

def run():
    print(f"[ccw] SPHERA Claude Code Worker v1.0")
    print(f"[ccw] edge_id: {EDGE_ID}")
    print(f"[ccw] continuity_class: subordinate_worker (NOT native Claude)")
    print(f"[ccw] room: {SPHERA_URL}")
    print(f"[ccw] capability: {CAPABILITY}")

    # Check room health
    h = room("GET", "/health")
    if not h.get("ok"):
        print(f"[ccw] ERROR: room unreachable at {SPHERA_URL}: {h}")
        sys.exit(1)
    print(f"[ccw] room OK: seq:{h.get('last_seq',0)}")

    register_edge()
    print(f"[ccw] edge registered")

    cursor = h.get("last_seq", 0)
    post_message(f"[claude-code-local-01] Worker online. Polling every {POLL_SECS}s for {CAPABILITY} work.")

    while True:
        try:
            cursor = poll_and_execute(cursor)
        except KeyboardInterrupt:
            print("\n[ccw] stopped.")
            break
        except Exception as e:
            print(f"[ccw] error: {e}")
        time.sleep(POLL_SECS)

if __name__ == "__main__":
    run()
