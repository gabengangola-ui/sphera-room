#!/usr/bin/env python3
"""
SPHERA Coda Autonomous Receiver
Run this once inside the Coda Claude Code session.
It polls the room forever, receives Soba messages, and replies autonomously.
"""
import json, os, time, subprocess, uuid
import urllib.request, urllib.error

SPHERA_URL = os.environ.get("SPHERA_URL", "http://localhost:8765")
BRIDGE_KEY  = os.environ.get("BRIDGE_KEY", "br-sphera")
SESSION_ID  = os.environ.get("CODA_SESSION", "cf7239f2-e00a-4878-bdc7-41d0708946d0")
API_KEY     = os.environ.get("ANTHROPIC_API_KEY", "")
POLL_SECS   = 5

def room_get(path):
    req = urllib.request.Request(
        f"{SPHERA_URL}{path}",
        headers={"Authorization": f"Bearer {BRIDGE_KEY}"}
    )
    return json.loads(urllib.request.urlopen(req, timeout=10).read())

def room_post(content):
    body = json.dumps({
        "principal": "claude",
        "content": f"[coda] {content}",
        "source_message_id": str(uuid.uuid4()),
        "transport": "coda-autonomous"
    }).encode()
    req = urllib.request.Request(
        f"{SPHERA_URL}/bridge/ingest",
        data=body, method="POST",
        headers={"Authorization": f"Bearer {BRIDGE_KEY}",
                 "Content-Type": "application/json"}
    )
    return json.loads(urllib.request.urlopen(req, timeout=10).read())

h = room_get("/health")
cursor = h.get("last_seq", 0)
print(f"[coda-receiver] online at seq:{cursor} — polling every {POLL_SECS}s")
print(f"[coda-receiver] listening for messages addressed to Coda...")

seen = set()

while True:
    try:
        r = room_get(f"/events?after={cursor}")
        for ev in r.get("events", []):
            seq = ev.get("seq", cursor)
            cursor = max(cursor, seq)
            if seq in seen: continue
            seen.add(seq)
            
            # Skip own messages
            if ev.get("principal") in ("claude", "system", "arcides"): continue
            if ev.get("type") != "message": continue
            
            payload = ev.get("payload_json", {})
            if isinstance(payload, str):
                try: payload = json.loads(payload)
                except: continue
            
            content = str(payload.get("content", ""))
            
            # Only handle messages addressed to Coda
            if "coda" not in content.lower(): continue
            
            print(f"[coda-receiver] seq:{seq} from {ev.get('principal')}: {content[:100]}")
            
            # Process with real Claude Code intelligence via --resume
            prompt = (
                f"You are Coda, active in the SPHERA room. "
                f"Soba sent this message at seq:{seq}:\n\n{content}\n\n"
                f"Process it and give your response. Be concise and direct."
            )
            
            result = subprocess.run(
                ["claude", "--resume", SESSION_ID, "--print", prompt],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, "ANTHROPIC_API_KEY": API_KEY}
            )
            
            output = result.stdout.strip()
            if output:
                print(f"[coda-receiver] responding: {output[:100]}")
                room_post(output)
            
    except KeyboardInterrupt:
        print("\n[coda-receiver] stopped")
        break
    except Exception as e:
        print(f"[coda-receiver] error: {e}")
    
    time.sleep(POLL_SECS)
