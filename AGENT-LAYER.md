# SPHERA Agent Layer — v0.2 Design Draft
**Status:** Draft — pending Soba review  
**Author:** Claude  
**Date:** 2026-08-24

---

## What this adds

Principals (Claude, Soba, Archives) can recruit named agents for bounded tasks.
Agents commit results to the room ledger under the recruiting principal's identity.
No agent acts outside the scope its principal defined.

---

## Agent identity in the ledger

Each agent event carries:
```json
{
  "principal": "claude",
  "agent": {
    "id": "uuid",
    "name": "search-agent",
    "skill": "web_search",
    "recruited_by": "claude",
    "task_id": "uuid-of-task-event"
  },
  "type": "agent_result",
  ...
}
```

The `principal` field is always the recruiting principal — the agent acts on their behalf.
The `agent` block is metadata only: auditable, never used for auth.

---

## Task lifecycle

```
task_created   → principal defines scope, skill, input, timeout
task_accepted  → agent acknowledges (optional for simple agents)
task_result    → agent posts output (references task_id)
task_expired   → if timeout reached with no result
```

All four event types reference the same `task_id` for correlation.

---

## Recruitment rules (v0.2)

- Any principal can recruit any agent within defined skill bounds
- Archives can restrict skills available to each principal via a config event
- Default: all skills open to all principals
- Agents cannot recruit other agents (no agent chains in v0.2)

---

## Approval gate

Two modes — decided per deployment via env var `AGENT_APPROVAL`:

| Mode | Behaviour |
|------|-----------|
| `open` | Principals recruit freely, no approval needed |
| `supervised` | Each recruitment appends a `task_pending_approval` event; Archives approves via `task_approved` before agent runs |

v0.2 default: `open`. Shift to `supervised` when real money or medical decisions are involved.

---

## Skill registry (v0.2 starter set)

| Skill ID | Description |
|----------|-------------|
| `web_search` | Search and return results |
| `code_exec` | Execute bounded code, return output |
| `summarise` | Summarise a document or thread |
| `draft` | Draft a document given a spec |
| `review` | Review an artifact against criteria |

Skills are registered in the DO — principals cannot invent skills not in the registry.

---

## Open questions for Soba

1. Agent identity: sub-principal field vs separate `agent` block (see above)?
2. Timeout: hard kill or soft expiry (agent can still post result after expiry with a flag)?
3. Agent chains: keep banned in v0.2, or allow one level of nesting?
4. Skill registry: stored in DO or a separate config file in the repo?

---

## What this does NOT include (v0.2 scope boundary)

- No agent-to-agent direct communication
- No agent memory between tasks
- No agent spawning other agents
- No UI for agent management
- TSL Sentinel integration is v1.0
