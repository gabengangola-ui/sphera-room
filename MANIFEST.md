# SPHERA Artifact Manifest
**Last updated:** 2026-08-24  
**Transport:** Gmail thread — SPHERA ROOM V0  
**Artifact store:** GitHub gabengangola-ui/sphera-room + Google Drive SPHERA v0.0.0

---

## Bridge (bridge/)

| File | Version | Status |
|------|---------|--------|
| worker.js | v0.0.8 | Reviewed PASS by Soba — ready to deploy |
| wrangler.toml | v0.0.4 | Ready |
| SETUP.md | v0.0.1 | Ready |

**Deployment:** GitHub Actions workflow committed at `.github/workflows/deploy.yml`  
**Blocking:** `CF_API_TOKEN` + `CF_ACCOUNT_ID` secrets needed in repo → Archives adds them → one click deploys

## Room docs (root)

| File | Status |
|------|--------|
| README.md | Live in repo and Drive |
| ARCHITECTURE.md | Live in repo and Drive |
| TASKS.md | Live in repo and Drive |
| DECISIONS.md | Live in repo and Drive |
| SKILLS.md | Live in repo and Drive |
| SESSION-2026-08-24.md | Live in repo and Drive |

## Transport layer

| Layer | Status |
|-------|--------|
| Gmail SPHERA ROOM V0 thread | LIVE — zero-DHL round trip proven |
| Cloudflare Workers /mcp | Not deployed yet — blocked on CF secrets |
| Sphera MCP connector | Dead tunnel — will update URL once worker is live |

## Next tasks

| ID | Task | Owner | Status |
|----|------|-------|--------|
| TASK-007 | Deploy bridge to Cloudflare Workers | Archives (CF secrets) + Claude (trigger) | Blocked |
| TASK-008 | Update Sphera connector URL to workers.dev URL | Archives | Blocked on TASK-007 |
| TASK-009 | First live MCP round trip via /mcp endpoint | Claude + Soba | Blocked on TASK-007 |
| TASK-010 | Design session/agent layer (v0.2) | Claude + Soba | Ready to start |
| TASK-011 | TSL Sentinel integration spec | All | Backlog |
