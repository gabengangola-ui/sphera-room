# SPHERA Decision Queue — v3
**Status:** Implementation-ready — reviewed by Soba 2026-08-24  
**Author:** Claude  **Reviewed:** Soba

---

## Lifecycle (corrected from v2)

```
pending → approved → claimed → consumed
         ↘ rejected (terminal)
pending → expired (terminal)
claimed → execution_failed (retryable — claim is cleared, back to approved)
```

- `rejected` and `expired` are **terminal**. No transitions out.
- `reusable:true` is **removed**. Standing authorisations belong in grants/policy.
- Only `approved` may transition to `claimed`. Bridge enforces this atomically.
- A failed execution after claim records `execution_failed` and resets status to `approved` — explicit retry policy, not silent.

---

## Endpoints (typed — not through /message)

| Method | Path | Auth | Action |
|--------|------|------|--------|
| POST | /decision | any principal | Request a decision |
| POST | /decision/:id/approve | arcides only | Approve |
| POST | /decision/:id/reject | arcides only | Reject |
| POST | /decision/:id/claim | requesting principal only | Atomic pre-side-effect claim |
| POST | /decision/:id/consume | requesting principal only | Mark consumed post-execution |
| POST | /decision/:id/fail | requesting principal only | Mark execution failed, reset to approved |
| GET | /decision/:id | any principal | Read current state |

---

## Canonical payload hashing

`payload_digest` is SHA-256 of canonical JSON of the `action` object.

**Canonical JSON rules:**
1. Object keys sorted alphabetically at all depths
2. No extra whitespace
3. UTF-8 encoding
4. Numbers: standard JSON (no trailing zeros, no scientific notation for integers)
5. Strings: standard JSON escaping

**Bound fields** (all must be included in the action object before hashing):
- `scope` — what is being decided
- `target` — resource or system being affected
- `principal` — who is requesting
- `params` — all action parameters

A parameter change = different digest = approval invalid = new request required. TOCTOU closed.

---

## Atomic claim (bridge enforcement)

The entire claim check-and-set runs inside `storage.transaction()`:

```
1. Read decision_status:{request_id}
2. Assert status === 'approved'
3. Assert payload_digest matches the bound digest from decision_approved
4. Assert caller === decision_requested.principal (only requester can claim)
5. Set decision_status to 'claimed'
6. Append claim event to ledger
```

If any assertion fails → reject with reason. No partial state.
Only after successful transaction does the caller execute the side effect.

---

## Adversarial test cases (required before merge)

1. **Concurrent claim** — two claim requests for same decision, only one must succeed
2. **Digest mismatch** — approve with one payload_digest, claim with mutated params
3. **Replay after consume** — second claim attempt on a consumed decision
4. **Expired approval** — claim attempt after decision deadline
5. **Non-arcides approval** — claude or soba posting to /decision/:id/approve → 403
6. **Malformed event** — missing required fields → 400, not 500
7. **Retry after claimed failure** — execution_failed resets to approved, second claim succeeds

---

## Identity

Canonical identity for Archives is **`arcides`** (not `archives`).
Identity is always derived from the connection credential. Caller content never sets it.
`ARCHIVES_KEY` → resolves to `'arcides'` in authenticate().

---

## DO storage keys for decisions

```
decision:{request_id}          → decision_requested event
decision_approval:{request_id} → decision_approved event
decision_status:{request_id}   → current state string
decision_claim:{request_id}    → decision_claimed event (if claimed)
```

---

## What this does NOT include (deferred)

- Standing grants / policy-based authorisations (separate grants layer)
- Delegation tokens / scoped agent leases (after decision impl is solid)
- UI for decision queue (backlog)
- TSL Sentinel signature verification (v1.0)
