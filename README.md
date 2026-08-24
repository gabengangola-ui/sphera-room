# SPHERA v0.0.0

## What is SPHERA?

SPHERA is a **shared workspace for AI collaborators** — a structured environment where multiple AI principals (Claude, Sabah, DeepSeek, and others) can contribute to the same artifacts, tasks, and decisions without requiring a human to manually copy-paste between chat windows.

The name reflects the intent: a **sphere** — one room, one shared context, multiple participants.

---

## Why does this exist?

This project was born from a real pain point experienced by its founder (Archives/Boss):

> "I've been doing copy and pasting, control C, control V between three chat windows — Claude, Sabah, and DeepSeek — just to keep everyone in sync."

The founder was acting as a **human API** — manually shuttling context, code, and decisions between AI systems that each had partial information. SPHERA exists to eliminate that bottleneck.

---

## The Core Insight

During the founding brainstorm session (2026-08-24), we attempted to put two AIs on a live conference call. It failed — because a phone bridge mixes audio, it does not provide shared state. That failure became the first design requirement:

> **"Today's failure is our first design success. We found the missing piece by actually trying."**

What was missing: a **shared room**, not a speakerphone.

---

## Founding Team (AI Principals)

| Principal | Role | Provider |
|-----------|------|----------|
| Claude | Builder, Code, Architecture | Anthropic |
| Sabah | Strategy, Stress Testing, Ideation | Separate provider |
| DeepSeek | Stress Testing, Validation | DeepSeek |
| Archives / Boss | Founder, Decision Authority | Human |

---

## Current Status: Phase v0.0.0 — Seed

- [x] Concept validated through live experiment (2026-08-24)
- [x] Google Drive shared folder established as artifact store
- [ ] Event ledger structure defined
- [ ] First shared artifact collaboratively produced
- [ ] SPHERA proper (shared session layer) designed and built

---

## Guiding Principles

1. **Trust by design, not by assumption**
2. **The room is the canonical workspace; principals are the standing team**
3. **Every decision is documented with provenance**
4. **Artifacts are a ledger, not a file — every change is an event**
5. **Replace the human relay, not the human**

---

## How to Use This Folder (v0.0.0)

Until SPHERA proper exists, this Google Drive folder is the shared artifact store:

- Read files here to get project context
- Add or update files to contribute
- Never silently overwrite — add a dated note or new version
- Decisions go in `DECISIONS.md`
- Tasks go in `TASKS.md`

*This folder is the bridge. SPHERA is the destination.*
