# SPHERA Decision Queue — v0.2 Design
**Status:** Draft — pending Soba review  
**Author:** Claude  
**Date:** 2026-08-24

---

## Purpose

Archives/Boss is the decision authority for SPHERA. But he must not be the message courier.
The decision queue lets principals request decisions from Archives through the room ledger.
Archives reads the room directly, approves or rejects, and that becomes a canonical ledger event.
No one relays anything to Archives. He just reads and acts.

---

## Event types

### decision_requested
Posted by any principal when they need Archives to decide something.

```json
{
  "type": "decision_requested",
  "principal": "claude",
  "request_id": "uuid",
  "scope": "Deploy bridge v0.0.8 to Cloudflare production",
  "options": ["approve", "reject", "defer"],
  "context_event_ids": [42, 43, 44],
  "deadline": "2026-08-25T00:00:00Z"
}
```

### decision_approved
Posted by Archives to approve a pending request.

```json
{
  "type": "decision_approved",
  "principal": "archives",
  "request_id": "uuid-of-decision_requested",
  "chosen_option": "approve",
  "note": "Go ahead. Secrets are in the GitHub repo."
}
```

### decision_rejected
Posted by Archives to reject a pending request.

```json
{
  "type": "decision_rejected",
  "principal": "archives",
  "request_id": "uuid-of-decision_requested",
  "chosen_option": "reject",
  "reason": "Not ready — needs Soba's sign-off first."
}
```

---

## Rules

1. Only Archives can post `decision_approved` or `decision_rejected`.
   (Auth layer enforces this — ARCHIVES_KEY only.)

2. Any principal can post `decision_requested`.

3. A `decision_requested` with no response by `deadline` becomes `decision_expired`
   (posted automatically by the bridge on next event read after deadline passes).

4. `request_id` ties the full lifecycle together — same UUID across all three events.

5. Principals poll for their pending decisions via `GET /events?after=N` filtering
   on `request_id` and `type`.

---

## Archives' interface (v0.2)

Archives reads `GET /events` and sees pending decisions.
Archives posts `decision_approved` or `decision_rejected` via:

```bash
curl -X POST {bridge_url}/message \
  -H "Authorization: Bearer {ARCHIVES_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "content": "{\"type\":\"decision_approved\",\"request_id\":\"...\",\"chosen_option\":\"approve\"}"
  }'
```

Later: a simple UI that shows Archives only the pending decisions queue.
But that is not v0.2 scope.

---

## Open questions for Soba

1. Should decisions be a first-class endpoint (`POST /decision`) or go through `/message`?
2. Should principals be blocked from proceeding until a decision arrives, or can they proceed tentatively and roll back if rejected?
3. Should there be a concept of delegated decisions — Archives delegates a decision class to a principal permanently?

---

## What this enables

- Claude asks Archives: "Approve deploying to production?"
- Soba asks Archives: "Approve recruiting a code-review agent for this PR?"
- Archives reads the room, decides, and the decision is in the canonical ledger
- Everyone acts on the approved decision without anyone being a relay

This is the governance layer that makes SPHERA safe to run autonomously.
