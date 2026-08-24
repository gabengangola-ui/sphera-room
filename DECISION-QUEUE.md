# SPHERA Decision Queue — v0.2 Design
**Status:** v2 — revised per Soba review 2026-08-24  
**Authors:** Claude + Soba

---

## Purpose

Archives is decision authority. The decision queue lets principals request decisions through the room ledger. Archives reads and acts directly — no relay, no courier, no DHL.

---

## Lifecycle: pending → approved | rejected | expired → consumed

```
decision_requested  → principal binds to exact proposed action (with digest)
decision_approved   → Archives approves — references exact request_id
decision_rejected   → Archives rejects — references exact request_id + reason
decision_expired    → bridge posts when deadline passes with no decision
decision_consumed   → posted when the approved decision is actually executed
```

**`decision_consumed` is mandatory.** One approval authorises one execution unless the request is explicitly marked `reusable: true`. Without `decision_consumed`, the approval is open indefinitely — a silent repeat-execution vector.

---

## decision_requested — full schema

```json
{
  "type": "decision_requested",
  "actor": { "actor_type": "principal", "actor_id": "claude" },
  "request_id": "uuid",
  "scope": "Deploy bridge v0.0.8 to Cloudflare production",
  "action": {
    "type": "cloudflare_worker_deploy",
    "target": "sphera-bridge",
    "version": "0.0.8",
    "payload_digest": "sha256:abc123..."
  },
  "options": ["approve", "reject", "defer"],
  "context_event_ids": [42, 43, 44],
  "deadline": "2026-08-25T00:00:00Z",
  "reusable": false
}
```

Key fields:
- `action.payload_digest` — SHA256 of the exact action payload. If action parameters change, this digest changes, approval is invalid, new request required. Closes TOCTOU.
- `reusable: false` — default. One approval = one execution. Set true only for standing authorisations (e.g. "always allow web search").
- `context_event_ids` — the specific ledger events this decision is about. Approval is bound to them.

---

## decision_approved — schema

```json
{
  "type": "decision_approved",
  "actor": { "actor_type": "human", "actor_id": "archives" },
  "request_id": "uuid-of-decision_requested",
  "chosen_option": "approve",
  "note": "Go. Secrets are set."
}
```

**`actor_id` is NEVER accepted from caller content.** It is derived from the authenticated connection (ARCHIVES_KEY). The bridge rejects any approval attempt not authenticated as archives.

---

## decision_consumed — schema

```json
{
  "type": "decision_consumed",
  "actor": { "actor_type": "principal", "actor_id": "claude" },
  "request_id": "uuid-of-decision_requested",
  "approval_event_seq": 47,
  "execution_result": "success | failure",
  "note": "Bridge deployed to workers.dev/sphera-bridge"
}
```

Posted immediately when the approved action is executed. After this, the approval is closed. Any further execution attempt against the same request_id is rejected unless `reusable: true`.

---

## TOCTOU protection (per Soba)

If the action or its parameters change after `decision_requested` is posted:
1. `payload_digest` no longer matches the new action.
2. The approval references a digest that doesn't match current state.
3. The bridge rejects execution and requires a new `decision_requested`.

No approval is ever valid for a different action than the one originally requested.

---

## What Archives sees (v0.2)

Archives reads `GET /events` and filters on `type=decision_requested` where no corresponding `decision_approved`, `decision_rejected`, or `decision_expired` exists yet. That is the pending decisions queue.

Archives posts approval via authenticated POST to `/message` with a structured decision_approved payload.

Later: a minimal read-only UI showing only the pending queue. But that is not v0.2 scope.

---

## Rules enforced by the bridge

1. Only ARCHIVES_KEY can post events with `actor_id=archives`.
2. `decision_approved`/`decision_rejected` must reference an existing `request_id` in the ledger.
3. After `decision_consumed`, any execution attempt against same `request_id` is rejected (unless `reusable: true`).
4. After `decision_expired`, approval is no longer possible — a new request must be made.
5. `approved_by` field in request body is ignored — identity comes from auth only.
