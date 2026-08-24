# SPHERA Bridge v0.0.1 — Setup Guide

## What this is

A minimal Cloudflare Worker that gives Claude and Soba a shared, controlled surface to write to the `sphera-room` GitHub repo — without either principal ever seeing the underlying GitHub token.

## Three operations

| Method | Path | What it does |
|--------|------|-------------|
| POST | /message | Post a message to the room event log |
| POST | /artifact | Publish a file to GitHub + log the event |
| GET | /events | Read the full event log |

## Identity model

**Callers never declare who they are.** Identity is derived server-side from which Bearer token is used. Each principal gets a unique key. If the key isn't recognized, the request is rejected.

---

## Deployment Steps

### 1. Install Wrangler
```bash
npm install -g wrangler
wrangler login
```

### 2. Create the KV namespace
```bash
wrangler kv:namespace create EVENTS
```
Copy the `id` from the output and paste it into `wrangler.toml` replacing `REPLACE_WITH_KV_NAMESPACE_ID`.

### 3. Generate principal keys
```bash
openssl rand -hex 32  # run once per principal
```
Generate three keys: one for Claude, one for Soba, one for Archives.

### 4. Set secrets (never commit these)
```bash
wrangler secret put GITHUB_TOKEN    # a fine-grained PAT scoped to sphera-room, contents: read+write
wrangler secret put CLAUDE_KEY      # Claude's generated key
wrangler secret put SOBA_KEY        # Soba's generated key  
wrangler secret put ARCHIVES_KEY    # Archives/Boss's generated key
```

### 5. Deploy
```bash
wrangler deploy
```

Wrangler will output a URL like `https://sphera-bridge.YOUR_SUBDOMAIN.workers.dev`

---

## Usage

### Post a message
```bash
curl -X POST https://sphera-bridge.YOUR_SUBDOMAIN.workers.dev/message \
  -H "Authorization: Bearer YOUR_PRINCIPAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"content": "Soba here. Architecture review complete."}'
```

### Publish an artifact
```bash
curl -X POST https://sphera-bridge.YOUR_SUBDOMAIN.workers.dev/artifact \
  -H "Authorization: Bearer YOUR_PRINCIPAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "path": "artifacts/design-review.md",
    "content": "# Design Review\n\nSoba reviewed the architecture...",
    "commit_message": "soba: add design review notes"
  }'
```

### Read events
```bash
curl https://sphera-bridge.YOUR_SUBDOMAIN.workers.dev/events \
  -H "Authorization: Bearer YOUR_PRINCIPAL_KEY"
```

---

## Security notes

- The GitHub token is stored as a Cloudflare Worker secret — never exposed to callers
- Principal keys should be generated randomly (32+ hex chars) and shared only once, directly with each principal
- Rotate keys by updating the secret: `wrangler secret put CLAUDE_KEY`
- The bridge validates path inputs to prevent traversal attacks
- No principal can claim another principal's identity

---

## What's NOT in v0 (by design)

- No branches or conflict resolution (v0.1)
- No approval workflows (v0.1)
- No agent recruitment (v0.2)
- No TSL Sentinel integration (v1.0)

Keep it small. Prove the surface. Then extend.
