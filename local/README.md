# SPHERA Local Server

Runs SPHERA on your machine. SQLite ledger. No cloud needed.

## Start

```bash
cd sphera-local
./start.sh
```

Server starts on http://localhost:8765

## Expose to Soba (optional)

Install cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
Then:
```bash
cloudflared tunnel --url http://localhost:8765
```
Copy the tunnel URL → update Sphera connector in Claude settings.

## Endpoints

| Method | Path | What |
|--------|------|------|
| POST | /message | Post a message |
| GET | /events?after=N | Read events |
| POST | /artifact | Publish artifact |
| GET | /artifact/:id | Get artifact lifecycle |
| POST | /mcp | MCP tools endpoint |

## Keys (already set in server.py)
- CLAUDE_KEY: 4fc8b916...
- SOBA_KEY: b47899c1...
- ARCHIVES_KEY: 399cff5a...

Change via environment variables before starting.
