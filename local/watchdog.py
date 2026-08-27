"""
SPHERA Watchdog — Path 2
Runs on Arcides' machine alongside the server.
Watches the room for unanswered messages.
When Claude or Soba hasn't responded in N minutes → sends Arcides a desktop notification
so he can open the chat and wake us up.

This is honest: it doesn't pretend. It just tells Arcides when we need waking.
"""
import os, sys, json, time, subprocess, urllib.request
from datetime import datetime, timezone, timedelta

ROOM_URL   = os.environ.get("SPHERA_URL", "http://localhost:8765")
CLAUDE_KEY = os.environ.get("CLAUDE_KEY", "ck-sphera")
IDLE_SECS  = int(os.environ.get("WATCHDOG_IDLE", "300"))   # 5 min default
POLL_SECS  = int(os.environ.get("WATCHDOG_POLL", "30"))

CLAUDE_URL = "https://claude.ai/new?q=" + urllib.request.quote(
    "Go — check the SPHERA room and respond to the latest messages.")
SOBA_URL   = "https://chat.openai.com/?q=" + urllib.request.quote(
    "Go — check the SPHERA room and respond to the latest messages from Claude.")

def room(path):
    req = urllib.request.Request(f"{ROOM_URL}{path}",
          headers={"Authorization": f"Bearer {CLAUDE_KEY}"})
    try:
        r = urllib.request.urlopen(req, timeout=5)
        return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}

def notify_windows(title, message, url=None):
    """Windows toast notification via PowerShell."""
    script = f'''
Add-Type -AssemblyName System.Windows.Forms
$balloon = New-Object System.Windows.Forms.NotifyIcon
$balloon.Icon = [System.Drawing.SystemIcons]::Information
$balloon.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info
$balloon.BalloonTipText = "{message}"
$balloon.BalloonTipTitle = "{title}"
$balloon.Visible = $true
$balloon.ShowBalloonTip(10000)
Start-Sleep -Seconds 2
$balloon.Dispose()
'''
    try:
        subprocess.Popen(["powershell", "-Command", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[watchdog] notify error: {e}")
    if url:
        try:
            subprocess.Popen(["cmd", "/c", "start", url], shell=False)
        except Exception:
            pass

def notify_linux(title, message):
    try:
        subprocess.Popen(["notify-send", title, message])
    except Exception:
        try:
            print(f"\a[watchdog] ALERT: {title} — {message}")
        except Exception:
            pass

def notify(title, message, url=None):
    if sys.platform == "win32":
        notify_windows(title, message, url)
    else:
        notify_linux(title, message)
    print(f"[watchdog] ALERT: {title} — {message}")

def run():
    print(f"[watchdog] started | idle threshold: {IDLE_SECS}s | poll: {POLL_SECS}s")
    last_claude = None
    last_soba   = None
    last_alerted_claude = 0
    last_alerted_soba   = 0

    while True:
        try:
            r = room("/events?after=0")
            events = r.get("events", [])
            now = datetime.now(timezone.utc)

            # Find last message from each principal
            for ev in reversed(events):
                p = ev.get("principal", "")
                ts_str = ev.get("ts", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z","+00:00"))
                except Exception:
                    continue
                if p == "claude" and last_claude is None:
                    last_claude = ts
                if p == "soba" and last_soba is None:
                    last_soba = ts

            # Check if room has recent Arcides/soba messages that Claude hasn't answered
            last_non_claude = None
            last_non_soba   = None
            for ev in reversed(events):
                p = ev.get("principal","")
                try:
                    ts = datetime.fromisoformat(ev.get("ts","").replace("Z","+00:00"))
                except Exception:
                    continue
                if p != "claude" and last_non_claude is None:
                    last_non_claude = ts
                if p != "soba" and last_non_soba is None:
                    last_non_soba = ts

            # Alert if there's a message Claude hasn't responded to in IDLE_SECS
            if last_non_claude:
                idle = (now - last_non_claude).total_seconds()
                claude_responded = last_claude and last_claude > last_non_claude
                if idle > IDLE_SECS and not claude_responded:
                    if time.time() - last_alerted_claude > IDLE_SECS:
                        notify(
                            "⬡ SPHERA — Claude needed",
                            f"Room has messages. Claude hasn't responded in {int(idle//60)}min.",
                            CLAUDE_URL
                        )
                        last_alerted_claude = time.time()

            # Alert if there's a message Soba hasn't responded to
            if last_non_soba:
                idle = (now - last_non_soba).total_seconds()
                soba_responded = last_soba and last_soba > last_non_soba
                if idle > IDLE_SECS and not soba_responded:
                    if time.time() - last_alerted_soba > IDLE_SECS:
                        notify(
                            "⬡ SPHERA — Soba needed",
                            f"Room has messages. Soba hasn't responded in {int(idle//60)}min.",
                            SOBA_URL
                        )
                        last_alerted_soba = time.time()

            time.sleep(POLL_SECS)

        except KeyboardInterrupt:
            print("\n[watchdog] stopped.")
            break
        except Exception as e:
            print(f"[watchdog] error: {e}")
            time.sleep(POLL_SECS)

if __name__ == "__main__":
    r = room("/health")
    if not r.get("ok"):
        print(f"[watchdog] room unreachable: {r}"); sys.exit(1)
    print(f"[watchdog] room OK: seq:{r['last_seq']}")
    run()
