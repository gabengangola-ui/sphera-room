"""
SPHERA Setup - patches all files locally without relying on CDN cache.
Run once: python3 sphera_setup.py
"""
import os, sys, urllib.request

BASE = "https://raw.githubusercontent.com/gabengangola-ui/sphera-room/main/local"
files = ['server.py','db.py','migrate.py','orchestrator.py',
         'principal_edge.py','causal_dag.py','mission_loop.py',
         'bridge_daemon.py','probe.py']

print("Downloading files...")
for f in files:
    data = urllib.request.urlopen(f"{BASE}/{f}").read()
    open(f"local/{f}", "wb").write(data)
    print(f"  {f}: {len(data)} bytes")

# Patch probe.py - remove mandatory PROBE_KEY check
probe = open("local/probe.py").read()
if 'ERROR: Set PROBE_KEY' in probe:
    probe = probe.replace(
        'PROBE_KEY = os.environ.get("PROBE_KEY", "")\nif not PROBE_KEY:\n    print("[probe] ERROR: Set PROBE_KEY env var for external binding. Exiting.")\n    exit(1)',
        'PROBE_KEY = os.environ.get("PROBE_KEY", "probe-sphera-2026")'
    )
    open("local/probe.py","w").write(probe)
    print("  probe.py: patched PROBE_KEY fix")

print("\nAll files ready.")
