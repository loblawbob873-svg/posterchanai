#!/usr/bin/env python3
"""Refresh scripts/test-durations.json: the measured seconds of every file on the deploy gate's required list.

The gate balances its parallel groups by these times (deploy_regression_gate.load_durations). The local cache
(~/.cache/posterchanai/test-durations.json) is written by every gate run on this machine; a CI runner has none,
so the snapshot is what it balances by. tests/test_required_durations_snapshot.py says when it needs refreshing.
"""
import json
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
gate = runpy.run_path(str(ROOT / 'scripts/deploy_regression_gate.py'))
try:
    cache = json.loads(gate['DURATIONS'].read_text())
except (OSError, ValueError):
    sys.exit('no local duration cache yet: run the gate once (./sync.sh or deploy_regression_gate.py --full)')
files = list(dict.fromkeys(t.split('::')[0] for t in gate['TESTS']))
snap = {f: round(float(cache[f]), 1) for f in files if f in cache}
gate['DURATIONS_SNAPSHOT'].write_text(json.dumps(snap, indent=0, sort_keys=True) + '\n')
print(f'{len(snap)} of {len(files)} required files measured -> {gate["DURATIONS_SNAPSHOT"].relative_to(ROOT)}')
