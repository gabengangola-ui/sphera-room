"""
SPHERA Capability Matching v0.1

Scoring algorithm:
  - Specialist (1 cap, exact match):     110
  - Exact match (multi-cap agent):       100
  - Superset (has cap + others):         80 - (extra_caps * 2)  min 10
  - No match:                            -1 (excluded)

Ranking: score desc → cap count asc → agent_id asc
"""

def score_agent(agent_caps: set, required_cap: str) -> int:
    if required_cap not in agent_caps:
        return -1
    if len(agent_caps) == 1:
        return 110  # perfect specialist
    base = 80
    penalty = (len(agent_caps) - 1) * 2
    return max(base - penalty, 10)

def match_agents(agents: list, required_capability: str, limit: int = 5) -> list:
    """
    Find and rank available agents for a required capability.
    Returns list of (agent_dict, score) sorted best-first.
    """
    candidates = []
    for agent in agents:
        if agent.get('status') != 'available':
            continue
        caps = set(agent.get('capabilities', []))
        s = score_agent(caps, required_capability)
        if s > 0:
            candidates.append((agent, s))
    candidates.sort(key=lambda x: (-x[1], len(x[0].get('capabilities', [])), x[0].get('agent_id', '')))
    return candidates[:limit]

def auto_assign(work_item: dict, agents: list) -> dict | None:
    """Return best available agent for a work item, or None if none available."""
    cap = work_item.get('capability')
    if not cap:
        return None
    ranked = match_agents(agents, cap, limit=1)
    return ranked[0][0] if ranked else None
