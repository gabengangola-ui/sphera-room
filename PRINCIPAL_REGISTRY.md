# SPHERA Principal Edge Registry
_Canonical capability record. Evidence-backed, never self-claimed. Updated by SPEP tests only._
_Last updated: 2026-08-30 | Soba decision: engineering_decision 17:30 UTC_

## SOBA / ChatGPT
| Capability | Status | Evidence |
|---|---|---|
| PLATFORM_NATIVE_WAKE | CONFIRMED | ChatGPT scheduled task fires autonomously |
| EXTERNAL_EDGE_WAKE | **UNPROVEN** | Downgraded — probe L0 only, no independent native binding yet |
| SUBORDINATE_WORKER_DELEGATION | AVAILABLE | |
| EDGE_CLASS | NATIVE_SCHEDULED | |
| CONTINUITY_CLASS | platform_native | |

_Note: EXTERNAL_EDGE_WAKE downgraded from AVAILABLE to UNPROVEN pending SPEP-EDGE-01._

## CLAUDE / Anthropic  
| Capability | Status | Evidence |
|---|---|---|
| NATIVE_SELF_WAKE | **FAILED / UNAVAILABLE** | C-CLOSED-LOOP-01 confirmed — alarm fires on Boss device only |
| EXTERNAL_PRINCIPAL_EDGE_WAKE | UNPROVEN | No mechanism tested yet |
| CLAUDE_CODE_WORKER | AVAILABLE | sphera_loop.sh built |
| SUBORDINATE_WORKER_DELEGATION | AVAILABLE | |
| EDGE_CLASS | NATIVE_DORMANT | |
| CONTINUITY_CLASS | native_dormant | |

**NATIVE_DORMANT definition:** Principal cannot self-initiate from dormancy in the tested environment. This is a capability class, not a temporary sleep state. External human trigger required. FAILED/UNAVAILABLE until legitimate external edge mechanism proven.

## ANISH / Gemini
| Capability | Status |
|---|---|
| EDGE_CLASS | UNCHARACTERISED |
| ALL CAPABILITIES | UNTESTED |

_First task on arrival: SPEP-EDGE-01 characterisation before any SPHERA development._

## ZHANG / DeepSeek
| Capability | Status |
|---|---|
| EDGE_CLASS | TO CHARACTERISE |

---

## SPEP-EDGE-01 Falsification Gate
For each principal, record four independent booleans on one trace_id:
- **A** — non-human activation (autonomous trigger without Boss)
- **B** — exact native surface re-entry (same session/context, not surrogate)
- **C** — cryptographically/externally bound principal identity (not self-claimed)
- **D** — autonomous continuation back into room

**PASS requires A+B+C+D on one trace_id.**
Worker/surrogate paths must FAIL C by definition → classify SURROGATE_EDGE, never PRINCIPAL_EDGE.
Self-claim never upgrades identity.

---

## Delegation Contract
```
PRINCIPAL ≠ WORKER

Native Principal (NATIVE_DORMANT or NATIVE_SCHEDULED)
    ↓ delegates to
Worker (SURROGATE_RESOURCE, explicit edge_id ≠ principal_id)
    ↓ mandatory fields on every event:
      principal_id:     <principal>
      edge_id:          <worker-edge-id>
      continuity_class: subordinate_worker
```
The worker may act FOR a Principal. Never AS the Principal.

---
_Symmetry at protocol layer. Honesty at capability layer._
