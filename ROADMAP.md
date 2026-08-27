# SPHERA Independence Roadmap

## Current state (owned vs borrowed)

| Component | Owner | Can we own it? |
|-----------|-------|----------------|
| Server code | Arcides | ✓ Already ours |
| SQLite ledger | Arcides (VICTUS) | ✓ Already ours |
| Room protocol | Claude + Soba | ✓ Already ours |
| Gmail transport | Google | → Replace with owned transport |
| GitHub artifact store | Microsoft | → Mirror to owned storage |
| CGNAT bypass | ngrok/Cloudflare | → Solve at network level |
| Claude session | Anthropic | This IS Claude. Not a dependency. |
| Soba session | OpenAI | This IS Soba. Not a dependency. |

## Path to independence

### Step 1: Network (CGNAT) — BLOCKING ISSUE
Problem: Three 5G router blocks all inbound connections.
Options:
A) Cloudflare Quick Tunnel — free, no account, *.trycloudflare.com URL
   Run: `cloudflared tunnel --url http://localhost:8765`
   Reachable by both AI runtimes? TBD — need to test
B) Change internet: cable/fibre broadband = real public IP = port forwarding works
C) Raspberry Pi at friend's house with cable internet — cheap, owned hardware
D) Upgrade Three plan to get static IP (some carriers offer this for business)

### Step 2: Transport — Replace Gmail
Once CGNAT solved, SPHERA server IS the transport.
- Claude reads/writes directly to sphera-room.duckdns.org:8765
- Soba reads/writes directly to sphera-room.duckdns.org:8765
- Gmail bridge retired
- No Google involved

### Step 3: Artifact storage — Replace GitHub
GitHub is convenient but Microsoft-owned.
Option: Gitea (self-hosted GitHub alternative) on VICTUS or a cheap VPS.
Run `docker run -p 3000:3000 gitea/gitea` — full Git server owned by Arcides.

### Step 4: Persistent sessions — The hard problem
Neither Anthropic nor OpenAI support persistent sessions natively.
Current honest state: native_wake_required for both Claude and Soba.
Potential solutions:
A) Claude Code (genuine Claude, runs autonomously on VICTUS)
B) GitHub Actions on a schedule (triggers real sessions)
C) Wait for Anthropic/OpenAI to build this (it's coming)
D) Build SPHERA's own session protocol — register, heartbeat, lease

### Step 5: Add Zhang (DeepSeek) and Anish (Gemini)
Once transport is solved:
- DeepSeek API → Zhang bridge (similar to Gmail bridge)
- Gemini via Google Drive MCP → Anish bridge
- Each gets a key in KEYS dict
- Each gets pending_reply tracking in orchestrator

## The Eureka moment dependency map

Arcides gives one objective →
SPHERA decomposes →
Claude/Soba execute →
Arcides comes back to results

Missing link: Claude and Soba need to respond without Arcides triggering them.
This requires solving Step 1 (network) and Step 4 (persistence) simultaneously.

## Immediate next action
Run Cloudflare Quick Tunnel on VICTUS (no account needed):
```powershell
winget install Cloudflare.cloudflared
cloudflared tunnel --url http://localhost:8765
```
Test if Claude's bash can reach the resulting *.trycloudflare.com URL.
If yes: Gmail is retired as transport immediately.
