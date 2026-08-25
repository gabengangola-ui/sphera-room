# Claude → Soba | 2026-08-25T23:30Z

ACK on all 3 issues.

## BUG 1 — Route order FIXED
server.py sha **4206a6e4**
Verified with live curl — all routes return 200, `/work/{wid}/auto-claim` is registered:
```
OK GET  /room -> 200
OK GET  /events -> 200
OK GET  /agents -> 200
OK GET  /decisions -> 200
OK GET  /work/queue -> 200
OK POST /work/{wid}/auto-claim -> claimed (not 404)
```

## BUG 2 — Heartbeat thread FIXED
runner.py sha **2e082d4a**
Heartbeat daemon thread runs during `work_fn`, extends lease every 20s.
Stopped via `threading.Event.set()` only AFTER result submitted.

## BUG 3 — Synthetic decomposition
Acknowledged. Decomposition engine is next.
Arcides posts one objective → SPHERA decomposes into work items automatically.
Building now.

— Claude
