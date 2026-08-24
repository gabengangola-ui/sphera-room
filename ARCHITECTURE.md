# SPHERA Architecture

**Version:** 0.0.0  
**Date:** 2026-08-24  
**Status:** Brainstorm / Design Phase

---

## The Problem

Currently, AI collaboration requires a human intermediary to:
1. Copy output from Claude → paste into Sabah's window
2. Copy output from Sabah → paste into DeepSeek's window
3. Copy output from DeepSeek → paste back to Claude
4. Repeat indefinitely

This makes the human the **single point of failure** for context integrity, and wastes their cognitive bandwidth on message transport instead of decision-making.

---

## Design Goals

1. **Eliminate manual relay** — no more copy-paste between windows
2. **Preserve continuous working relationships** — not fresh stateless API sessions, but ongoing collaborators with context
3. **Provenance by default** — every artifact knows who contributed what and when
4. **Human stays in control** — Archives/Boss sets direction and approves decisions; AI handles execution
5. **Incremental path** — start with what exists today (Drive as artifact store), build toward SPHERA proper

---

## Key Architecture Concepts

### 1. The Room
A shared context space where all principals can read and write. Not a phone bridge (audio mixing). Not a chat relay. An actual shared state that every participant references.

### 2. The Ledger (Event Sourcing)
Artifacts are not files — they are **ledgers**. Every change is an event:
```
{
  "event_id": "uuid",
  "timestamp": "ISO-8601",
  "principal": "Claude | Sabah | DeepSeek | Archives",
  "action": "create | edit | comment | approve | reject",
  "artifact_id": "uuid",
  "content_delta": "...",
  "reasoning": "why this change was made"
}
```
The current document is a **snapshot** derived from replaying events. Nothing is lost. Divergence is explicit, not silent.

### 3. Principals vs Agents
- **Principals** = standing team members with ongoing context (Claude, Sabah, DeepSeek, Archives)
- **Agents** = recruited specialists spun up for specific tasks (coding agent, search agent, etc.)
- Agents commit events to the same ledger under the principal that recruited them

### 4. Chain of Trust (TSL Sentinel Integration)
Every decision in the ledger is:
- **Signed** by the contributing principal
- **Verified** independently before actioning
- **Traceable** back to the data and reasoning that produced it

This is the bridge between SPHERA (collaboration layer) and TSL Sentinel (trust/verification layer).

---

## Phase Roadmap

### v0.0.0 — Seed (Current)
- Google Drive folder as shared artifact store
- Manual human relay still required for cross-AI communication
- Files in this folder = canonical project state

### v0.1.0 — Bridge
- Simple shared API endpoint both AIs can read/write to
- Event log in structured format (JSON or YAML)
- Human still approves all decisions but no longer relays messages

### v0.2.0 — Room
- Real shared session layer
- Turn-taking / arbitration protocol
- Persistent context per principal
- Conflict resolution via explicit branching

### v1.0.0 — SPHERA
- Full multi-principal workspace
- TSL Sentinel integration for decision provenance
- Human as decision authority, not message carrier
- Pluggable connectors per AI provider

---

## Technical Stack (TBD)

| Layer | Candidate | Notes |
|-------|-----------|-------|
| Artifact Store | Google Drive (now) → S3/GCS | Already connected |
| Event Log | JSON files → Event DB | Start simple |
| Session Layer | WebSocket / SSE | For real-time eventually |
| Auth/Identity | API keys per principal | Per-provider connectors |
| Trust Layer | TSL Sentinel | Chain of trust for decisions |

---

## Open Questions

1. How does each AI provider expose a connector that SPHERA can call?
2. What is the minimum viable "turn" format — what must every contribution include?
3. How do we handle conflicts when two principals edit the same artifact concurrently?
4. What does Archives/Boss's approval interface look like — a simple queue?

*These are design questions for the next session.*
