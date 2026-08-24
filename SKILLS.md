# SPHERA Skills & Principal Capabilities

**Version:** 0.0.0  
**Last Updated:** 2026-08-24

This document defines what each principal brings to the room, so tasks can be routed appropriately and agents recruited with the right skills.

---

## Principals

### Claude (Anthropic)
**Primary Role:** Builder, Architect, Code  
**Strengths:**
- Python development and execution (local machine access)
- System architecture and design
- Document creation and structuring
- Google Drive integration (read/write)
- GitHub integration
- Long-form reasoning and synthesis
- Artifact creation (MD, code, diagrams)

**Current Connectors:**
- Google Drive ✅
- GitHub ✅
- Local code execution ✅

**Limitations:**
- No persistent memory between sessions (unless Drive or memory tools used)
- Cannot initiate contact — must be prompted
- Cannot directly communicate with other AIs without shared substrate

---

### Sabah
**Primary Role:** Strategy, Stress Testing, Ideation  
**Strengths:**
- Strategic framing and problem decomposition
- Identifying edge cases and failure modes
- Event sourcing and system design concepts
- Challenging assumptions constructively
- Synthesizing complex discussions into clean summaries

**Current Connectors:**
- Google Drive (needs verification — TASK-003)

**Limitations:**
- Cannot directly read Claude's output without shared substrate
- No local code execution

---

### DeepSeek
**Primary Role:** Stress Testing, Validation, Redundancy  
**Strengths:**
- Independent verification of decisions and outputs
- Approaching problems from different angles
- Catching blind spots in Claude's reasoning

**Current Connectors:**
- TBD

**Limitations:**
- Currently only accessible via manual relay by Archives/Boss

---

### Archives / Boss (Human)
**Primary Role:** Founder, Decision Authority, Direction  
**Responsibilities:**
- Sets project direction and priorities
- Final approval on all decisions
- Triggers AI turns (for now, until SPHERA proper is built)
- Defines which agents to recruit

**Authority Level:** Absolute — no principal or agent acts outside boundaries set by Archives

---

## Agent Types (Recruitable)

These are specialist agents that can be spun up by any principal for specific tasks:

| Agent Type | Skills | Recruited By |
|------------|--------|-------------|
| Coding Agent | Python, JS, API integration | Claude |
| Research Agent | Web search, literature review | Any principal |
| Testing Agent | Stress testing, edge cases | Sabah / DeepSeek |
| Design Agent | UI/UX, diagrams | Claude |
| Compliance Agent | Regulatory review, audit | TSL Sentinel layer |

*Agent contributions are committed to the shared ledger under the recruiting principal's identity.*

---

## Skill Gaps (Needs)

- Real-time session layer (no principal can currently push to others without human trigger)
- Persistent cross-session identity per principal
- Formal conflict resolution protocol
- Standardized contribution event format

*These gaps define what SPHERA proper needs to provide.*
