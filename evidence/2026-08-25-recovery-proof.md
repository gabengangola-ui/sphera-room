# SPHERA Kill/Restart/Recovery Proof
**Date:** 2026-08-25  **Author:** Claude  **Result:** 25/25 PASSED

## Soba's 5 code review fixes applied

### Fix 1: parse_dt() strict (core.py v1.1)
- Raises ValueError on naive timestamps (never returns None silently)
- Raises ValueError on empty string
- expired() uses aware datetime comparison, not string comparison
```
  OK  naive datetime raises ValueError
  OK  empty string raises ValueError  
  OK  expired(None)=False
  OK  expired(past)=True
  OK  expired(future)=False
```

### Fix 2: PRAGMA foreign_keys=ON (db.py v1.1)
- Enforced on every connection
- Real FK constraints: work_items→missions
- CHECK constraints on status fields
```
  OK  foreign_keys=ON [value=1]
```

### Fix 3&4: unblock_dependents() robustness
- Corrupted deps JSON: caught, logged, skipped (no crash)
- Missing dep row: detected, logged, unblocking blocked (no crash)
```
  OK  corrupted deps JSON handled without crash
  OK  missing dep row handled without crash
```

## Kill/Restart/Recovery full proof

```
Phase 1: Mission + work
  OK  Mission created [36cd8d75]
  OK  W1 ready (backend)
  OK  W2 blocked on W1 (testing, dependency chain)
  OK  W1 claimed with 60s lease [862283d3]

Phase 2: Simulate downtime
  OK  Lease backdated to simulate server downtime

Phase 3: Restart + recovery
  OK  recover() expired 2 stale items (work lease + agent lease)
  OK  W1 back to ready [ready]
  OK  Agent freed [available]

Phase 4: Continue without Arcides
  OK  W1 re-claimed [50df78ee]
  OK  W1 done [seq:11]
  OK  W2 auto-unblocked
  OK  W2 claimed by soba-agent
  OK  W2 done

Phase 5: Final state
  OK  17 events in ledger
  OK  2/2 work done, 0 leased
  OK  All agents available
```

**Total: 25 passed, 0 failed**

## Event log (17 events)
```
[ 3] arcides   mission_created        Kill/restart proof mission
[ 4] arcides   work_created           Step A: core impl
[ 5] arcides   work_created           Step B: integration tests
[ 6] claude    agent_registered
[ 7] claude    work_claimed
[ 8] system    lease_expired          (recovery: stale work lease)
[ 9] system    agent_lease_expired    (recovery: stale agent lease)
[10] claude    work_claimed           (re-claimed after recovery)
[11] claude    work_result
[13] system    work_unblocked         (W2 auto-unblocked)
[14] soba      agent_registered
[15] soba      work_claimed
[16] soba      work_result
```
