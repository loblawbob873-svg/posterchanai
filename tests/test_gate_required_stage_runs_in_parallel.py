"""The gate's REQUIRED stage runs in parallel groups. Serially it outgrew every time budget it was given:
deploy 210's took 2006-2034s of 2100s, and deploy 211 was killed at 2100s with ~930 of 2,246 cases run, no
test named and nothing deployed. Driven through the SHIPPED gate with a stand-in pytest whose run time grows
with the tests it is handed -- the one property that made the serial stage fail."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / 'scripts/deploy_regression_gate.py'

STANDIN = '''import os,sys,time
from pathlib import Path
report=next(a.split('=',1)[1] for a in sys.argv if a.startswith('--junitxml='))
ids=[a for a in sys.argv[1:] if '.py' in a and not a.startswith('-')]
time.sleep(len(ids)*float(os.environ['PER_TEST_S']))
cases=''.join('<testcase classname="x" name="%s" time="0"/>' % i.replace('"','') for i in ids)
Path(report).write_text('<testsuites><testsuite>'+cases+'</testsuite></testsuites>')
'''


def _gate(tmp_path, per_test, jobs, budget):
    (tmp_path / 'pytest.py').write_text(STANDIN)
    env = dict(os.environ, PYTHONPATH=str(tmp_path), XDG_CACHE_HOME=str(tmp_path / 'cache'),
               PER_TEST_S=str(per_test), PC_GATE_JOBS=str(jobs), PC_REQUIRED_TIMEOUT=str(budget))
    return subprocess.run([sys.executable, str(GATE)], env=env, capture_output=True, text=True, timeout=120)


def test_a_required_list_longer_than_one_budget_still_finishes_in_parallel(tmp_path):
    import runpy
    n = len(runpy.run_path(str(GATE))['TESTS'])
    per = 12.0 / n                       # serially: 12s against a 7s budget -- the deploy-211 shape
    result = _gate(tmp_path, per, jobs=4, budget=7)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
    assert 'parallel group' in result.stdout and '[regressions] PASS:' in result.stdout
    assert ('%d required cases' % n) in result.stdout, 'a required entry was dropped or run twice'


def test_groups_keep_a_file_together_and_cover_every_entry():
    import runpy
    gate = runpy.run_path(str(GATE))
    entries = list(gate['TESTS'])
    groups = gate['required_shards'](entries, 4, {}, ROOT)
    flat = [e for g in groups for e in g]
    assert sorted(flat) == sorted(entries)
    owner = {}
    for i, g in enumerate(groups):
        for e in g:
            assert owner.setdefault(e.split('::')[0], i) == i, e
    assert len(groups) > 1
