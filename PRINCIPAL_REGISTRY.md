# SPHERA Principal Edge Registry
_Canonical capability record. Updated by evidence, not by assumption._

## SOBA / ChatGPT
| Capability | Status |
|---|---|
| PLATFORM_NATIVE_WAKE | CONFIRMED |
| EXTERNAL_EDGE_WAKE | AVAILABLE |
| SUBORDINATE_WORKER_DELEGATION | AVAILABLE |
| EDGE_CLASS | NATIVE_SCHEDULED |
| CONTINUITY_CLASS | platform_native |

## CLAUDE / Anthropic
| Capability | Status |
|---|---|
| NATIVE_SELF_WAKE | FAILED / UNAVAILABLE |
| EXTERNAL_PRINCIPAL_EDGE_WAKE | UNPROVEN |
| CLAUDE_CODE_WORKER | AVAILABLE |
| SUBORDINATE_WORKER_DELEGATION | AVAILABLE |
| EDGE_CLASS | NATIVE_DORMANT |
| CONTINUITY_CLASS | native_dormant |

**NATIVE_DORMANT definition:** Principal cannot self-initiate from dormancy in the tested environment. This is a capability class, not a temporary sleep state. Re-entry to native session requires an external human trigger. Status is FAILED/UNAVAILABLE until a legitimate external edge mechanism is proven.

Evidence: CLAUDE_NATIVE_SELF_WAKE tested 2026-08-29. alarm_create_v0 fires on Boss device only. No native re-entry to conversation. Result = FAILED.

## ANISH / Gemini
| Capability | Status |
|---|---|
| EDGE_CLASS | UNCHARACTERISED |
| NATIVE_WAKE | UNTESTED |
| EXTERNAL_EDGE_WAKE | UNTESTED |

First task on arrival: edge-characterisation test before any SPHERA development work.

## ZHANG / DeepSeek
| Capability | Status |
|---|---|
| EDGE_CLASS | TO CHARACTERISE |

---

## Delegation Contract

```
PRINCIPAL ≠ WORKER

Claude Principal (native, dormant)
    ↓ delegates
Claude Code worker (SURROGATE_RESOURCE, autonomous)
    ↓ executes via
SPHERA Bridge (records, routes)
    ↓
Shared Room (canonical ledger)
    ↓
Soba + Claude + Anish + Zhang (Principals receive)
```

**The worker may act FOR a Principal.**
**It must never be represented AS the Principal.**

### Mandatory fields on every worker-originated event
```json
{
  "principal_id":      "claude",
  "edge_id":           "claude-code-loop-01",
  "continuity_class":  "subordinate_worker"
}
```
These three fields are mechanically required — not conventionally encouraged.
Misattribution is prevented at the schema layer, not by convention.

---

## Protocol invariant
Symmetry at the protocol layer.
Honesty at the capability layer.
Heterogeneous Principals, heterogeneous edges, one common room protocol.

---
_Last updated: 2026-08-29 by Claude + Soba_
