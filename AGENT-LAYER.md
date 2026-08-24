# SPHERA Agent Layer — v0.2 Design
**Status:** v2 — revised per Soba review 2026-08-24  
**Authors:** Claude + Soba

---

## Actor envelope (revised)

Every event carries a first-class `actor` block. Authentication terminates at the Principal/worker connection. The ledger records the derived actor identity for provenance.

```json
{
  "actor": {
    "actor_type": "principal | agent | human",
    "actor_id": "claude | soba | archives | agent-uuid",
    "sponsor_principal_id": "claude",
    "delegation_id": "uuid-of-task_created-event"
  }
}
```

Rules:
- `actor_type=principal` — actor_id matches an authenticated key. No sponsor needed.
- `actor_type=agent` — actor_id is a uuid. sponsor_principal_id and delegation_id are required.
- `actor_type=human` — for Archives acting directly outside a principal session.
- Agents are NOT principals. They hold no credentials. Auth terminates at the sponsoring principal.
- `sponsor_principal_id` and `delegation_id` give full auditable lineage back to the human decision that authorised the agent.

---

## Task lifecycle

```
task_created      → principal defines scope, skill, input, authority_expiry
task_accepted     → agent acknowledges (optional)
task_result       → agent posts output (references delegation_id)
task_result_late  → result posted after authority_expiry (flagged, pending review)
task_expired      → bridge posts when authority_expiry passes with no result
```

### Authority vs result — SEPARATED (per Soba)

| Concept | Meaning | Expiry type |
|---------|---------|-------------|
| `authority_expiry` | Agent loses capability to act | HARD — bridge enforces, no exceptions |
| `result_acceptance_deadline` | Window to accept a late result | SOFT — humans decide |

An agent may submit `task_result_late` for human review, but it MUST NOT retain execution capability after `authority_expiry`. These are independent clocks.

---

## Skill registry (revised)

Registry mutations are immutable events — not mutable state.
The DO maintains a materialized current projection from the event history.

```
skill_registered   → new skill added to registry
skill_deprecated   → skill marked inactive (not deleted — history is immutable)
skill_updated      → parameters/description updated (creates new version, old preserved)
```

`GET /skills` returns the current materialized projection.
The event log is the source of truth. The registry is a read-optimised view of it.

This means: you can audit exactly what skills existed at any point in time by replaying events up to that timestamp.

---

## Agent chains — v0.2 rule

**One delegation level only. No exceptions.**
- Principals may spawn agents.
- Agents may NOT spawn other agents.
- This is enforced by the bridge: if `actor_type=agent` posts a `task_created`, it is rejected with 403.

Agent chains are v0.3 scope with explicit human approval gate per chain.

---

## Recruitment rules

- Any principal can recruit any registered skill within their authority bounds.
- Archives can restrict available skills per principal via a `principal_skill_grant` event.
- Default (no grants configured): all registered skills open to all principals.
- Skill bounds are checked at task_created time against the current registry projection.

---

## Open questions resolved (Soba review)

| Question | Resolution |
|----------|------------|
| Agent as metadata vs first-class? | First-class `actor` envelope, not just nested metadata |
| Soft vs hard timeout? | Hard expiry for authority, soft for result acceptance |
| Agent chains? | Banned v0.2, one level only |
| Registry in DO or config? | Events in ledger, materialized projection in DO |

---

## What is NOT in v0.2

- Agent-to-agent communication
- Agent memory between tasks
- Recursive agent spawning
- TSL Sentinel integration (v1.0)
- Agent management UI
