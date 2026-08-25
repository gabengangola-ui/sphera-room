"""
SPHERA Decomposition Engine v0.1

Takes a mission objective and breaks it into concrete work items with dependencies.
Uses rule-based decomposition for v0.1 (no LLM dependency — deterministic and auditable).
LLM-powered decomposition is v0.2.

Rules:
- Objective is analysed for known capability keywords
- A standard build pipeline is generated: design → build → test → review → deploy
- Only relevant stages are included based on objective keywords
- Dependencies chain automatically
"""

import re
from typing import List, Dict

# Keyword → capability mapping
CAPABILITY_SIGNALS = {
    'backend':   ['api', 'server', 'endpoint', 'database', 'db', 'service', 'backend', 'data'],
    'frontend':  ['ui', 'interface', 'console', 'dashboard', 'page', 'browser', 'html', 'frontend', 'client'],
    'testing':   ['test', 'spec', 'verify', 'validate', 'qa', 'quality'],
    'devops':    ['deploy', 'deployment', 'release', 'ship', 'production', 'infra', 'infrastructure', 'ci', 'cd'],
    'security':  ['security', 'auth', 'authentication', 'authorization', 'audit', 'pentest'],
    'docs':      ['document', 'documentation', 'readme', 'spec', 'design'],
    'data':      ['data', 'analytics', 'pipeline', 'etl', 'migration', 'schema'],
}

def detect_capabilities(objective: str) -> List[str]:
    """Detect which capabilities are relevant based on objective keywords."""
    obj_lower = objective.lower()
    detected = []
    for cap, signals in CAPABILITY_SIGNALS.items():
        if any(sig in obj_lower for sig in signals):
            detected.append(cap)
    # Always include testing for any build work
    if any(c in detected for c in ('backend','frontend','security','data')) and 'testing' not in detected:
        detected.append('testing')
    # Always end with deploy if not purely docs
    if detected and 'devops' not in detected and 'docs' not in detected:
        detected.append('devops')
    return detected if detected else ['backend', 'testing', 'devops']  # sensible default

def decompose(objective: str, mission_id: str = None) -> List[Dict]:
    """
    Decompose an objective into ordered work items.
    Returns list of dicts with: description, capability, dependencies (by index position).
    """
    caps = detect_capabilities(objective)
    
    # Build ordered work items with dependency chain
    items = []
    
    # Stage 1: Design/planning (if objective mentions design/architecture)
    obj_lower = objective.lower()
    if any(w in obj_lower for w in ['design', 'architect', 'plan', 'spec', 'platform', 'system']):
        items.append({
            'description': f'Design architecture and specs for: {_short(objective)}',
            'capability': 'docs',
            'dep_indices': []
        })

    # Stage 2: Core build stages in detected capability order
    build_caps = [c for c in caps if c not in ('testing', 'devops', 'docs')]
    for cap in build_caps:
        prev = [len(items) - 1] if items else []
        items.append({
            'description': f'Implement {cap} layer: {_short(objective)}',
            'capability': cap,
            'dep_indices': prev
        })

    # Stage 3: Testing (depends on all build stages)
    if 'testing' in caps:
        build_indices = [i for i,item in enumerate(items) if item['capability'] not in ('docs', 'testing', 'devops')]
        prev = [build_indices[-1]] if build_indices else ([len(items)-1] if items else [])
        items.append({
            'description': f'Write and run tests: {_short(objective)}',
            'capability': 'testing',
            'dep_indices': prev
        })

    # Stage 4: Security review (if needed, after testing)
    if 'security' in caps:
        prev = [len(items) - 1] if items else []
        items.append({
            'description': f'Security review: {_short(objective)}',
            'capability': 'security',
            'dep_indices': prev
        })

    # Stage 5: Deploy (last, depends on everything before)
    if 'devops' in caps:
        prev = [len(items) - 1] if items else []
        items.append({
            'description': f'Deploy to production: {_short(objective)}',
            'capability': 'devops',
            'dep_indices': prev
        })

    return items

def _short(s: str, n: int = 40) -> str:
    return s[:n] + '...' if len(s) > n else s

def decompose_and_create(objective: str, mission_id: str, call_fn) -> List[str]:
    """
    Decompose objective and create work items via the SPHERA API.
    call_fn(method, path, body) → response dict
    Returns list of created work_ids.
    """
    plan = decompose(objective, mission_id)
    work_ids = []
    
    for item in plan:
        deps = [work_ids[i] for i in item['dep_indices'] if i < len(work_ids)]
        r = call_fn('POST', f'/mission/{mission_id}/work', {
            'description': item['description'],
            'capability': item['capability'],
            'dependencies': deps
        })
        if r.get('work_id'):
            work_ids.append(r['work_id'])
            status = r.get('status', '?')
            print(f'  [{status:7s}] {item["description"][:50]} [{item["capability"]}]')
        else:
            print(f'  [ERROR] Failed to create work item: {r}')
    
    return work_ids


# ── Self-test ─────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    tests = [
        'Deploy SPHERA backend API to production',
        'Build room UI console for operator access',
        'Ship full SPHERA platform with auth, backend, frontend, and deploy',
        'Write integration tests for the event ledger',
        'Design and implement the security audit system',
    ]
    
    p = f = 0
    def ok(l): global p; print(f'  OK   {l}'); p += 1
    def fail(l, d=''): global f; print(f'  FAIL {l}' + (f': {d}' if d else '')); f += 1
    
    print('=== DECOMPOSER TESTS ===\n')
    
    for obj in tests:
        plan = decompose(obj)
        caps = [item['capability'] for item in plan]
        deps_ok = all(all(d < i for d in item['dep_indices']) for i, item in enumerate(plan))
        print(f'\n  "{obj[:55]}"')
        for item in plan:
            dep_str = f' ← {item["dep_indices"]}' if item['dep_indices'] else ''
            print(f'    [{item["capability"]:10s}] {item["description"][:50]}{dep_str}')
        ok(f'deps are backward-only ({len(plan)} items)') if deps_ok else fail('dep ordering', str(plan))
        ok(f'testing included') if 'testing' in caps else fail('testing missing')
    
    # Edge: empty-ish objective
    plan = decompose('do something')
    ok('default pipeline for unknown objective') if len(plan) >= 2 else fail('default too short')
    
    # Dependency chain: each item only depends on items before it
    for obj in tests:
        plan = decompose(obj)
        for i, item in enumerate(plan):
            for dep_idx in item['dep_indices']:
                if dep_idx >= i:
                    fail(f'forward dependency in: {obj}')
                    break
        else:
            ok(f'no forward deps: {obj[:40]}')
    
    print(f'\n=== {p} passed, {f} failed ===')
