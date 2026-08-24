# SPHERA Decisions Log

**Version:** 0.0.0  
**Format:** Every decision logged with who made it, when, why, and what alternatives were considered.

---

## DEC-001 — Use Google Drive as v0.0.0 Artifact Store

**Date:** 2026-08-24  
**Decision Authority:** Archives / Boss  
**Contributors:** Claude, Sabah  
**Status:** Accepted

**Decision:**  
Use Google Drive as the shared artifact store for SPHERA v0.0.0, not a live session bridge.

**Reasoning:**  
A live AI-to-AI bridge does not yet exist. Google Drive is already connected to both Claude and (potentially) Sabah. It provides a real shared substrate today with zero new infrastructure required.

**Alternatives Considered:**  
- Live conference call bridge → Failed in experiment; no shared state, only audio mixing
- Custom API endpoint → Requires build time; not available today

**Trade-offs:**  
- Drive is great for artifacts, not for dialogue or task state
- Human still needs to trigger each side's turn manually
- Acceptable for v0.0.0; replaced in v0.1.0

---

## DEC-002 — Artifacts Are Ledgers, Not Files

**Date:** 2026-08-24  
**Decision Authority:** Sabah (proposed) → Archives / Boss (accepted)  
**Contributors:** Sabah, Claude  
**Status:** Accepted

**Decision:**  
All SPHERA artifacts will be treated as event-sourced ledgers. The current document is a snapshot derived from an immutable event history. Every change is an event with: who, when, what, why.

**Reasoning:**  
Provenance cannot be bolted on after the fact. If the audit trail IS the data structure, traceability is native. Conflicts become explicit branches rather than silent overwrites. This maps directly to the TSL Sentinel chain-of-trust requirement.

**Alternatives Considered:**  
- Traditional file versioning (e.g. Git) → Good, but doesn't capture reasoning per change
- Simple timestamped overwrites → Loses history; not acceptable for trust layer

**Trade-offs:**  
- More complex than simple files
- Requires event replay to reconstruct current state
- Worth it: provenance is a core SPHERA requirement, not a nice-to-have

---

## DEC-003 — Pilot TSL Sentinel in Finance or Healthcare First

**Date:** 2026-08-24  
**Decision Authority:** Archives / Boss  
**Contributors:** Claude, Sabah  
**Status:** Accepted

**Decision:**  
TSL Sentinel (the AI decision trust and provenance layer) will be piloted in financial compliance or healthcare diagnostics before expanding to other sectors.

**Reasoning:**  
These sectors are already heavily regulated, highly scrutinized, and actively demanding better auditability. The pain is acute and the demand is already there — no need to create the market. A proven track record in high-stakes domains gives credibility for expansion.

**Quote:**  
"Start with what keeps them up at night." — Archives / Boss

---

## DEC-004 — Replace Human Relay, Not Human Authority

**Date:** 2026-08-24  
**Decision Authority:** Archives / Boss  
**Status:** Accepted — Founding Principle

**Decision:**  
SPHERA's goal is to remove Archives/Boss from the role of message carrier (copy-paste relay) while preserving and strengthening his role as decision authority and direction setter.

**Reasoning:**  
The human's cognitive bandwidth should be spent on decisions, not transport. The vision is: Archives steps away for six hours, returns to find work has progressed within the authority he set, with a clear record of what happened and what requires his decision.

**This is non-negotiable:** No AI principal acts outside the authority boundaries set by Archives/Boss.
