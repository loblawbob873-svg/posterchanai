"""Real owned subprocess trees must stop when the release gate finishes or aborts."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


def running(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1][0] != 'Z'
    except FileNotFoundError:
        return False


def wait_for(predicate, message):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline, message
        time.sleep(.02)


@pytest.mark.parametrize('ending', ['timeout', 'interrupt', 'terminate', 'success'])
def test_gate_cleans_its_children_and_leaves_unrelated_processes(tmp_path, ending):
    pids = tmp_path / 'owned'
    child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"
    workload = ("import os,pathlib,subprocess,sys,time\n"
                f"p=subprocess.Popen([sys.executable,'-c',{child!r}])\n"
                f"pathlib.Path({str(pids)!r}).write_text(str(os.getpid())+' '+str(p.pid))\n"
                + ("print('finished')\n" if ending == 'success' else "time.sleep(30)\n"))
    code = f"""
import importlib.util, os, pathlib, sys
spec=importlib.util.spec_from_file_location('gate', {str(ROOT/'scripts/deploy_regression_gate.py')!r})
gate=importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
rc,out=gate._run_required_tests([sys.executable,'-c',{workload!r}],pathlib.Path({str(tmp_path)!r}),os.environ,pathlib.Path({str(tmp_path/'run.log')!r}),timeout={.5 if ending == 'timeout' else 10})
print(out,flush=True)
raise SystemExit(rc)
"""
    sentinel = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    runner = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, start_new_session=True)
    owned = []
    try:
        wait_for(lambda: pids.exists() and len(pids.read_text().split()) == 2, 'workload did not start')
        owned = list(map(int, pids.read_text().split()))
        if ending in ('interrupt', 'terminate'):
            runner.send_signal(signal.SIGINT if ending == 'interrupt' else signal.SIGTERM)
        stdout, stderr = runner.communicate(timeout=5)
        expected = {'timeout': 124, 'interrupt': 130, 'terminate': 130, 'success': 0}[ending]
        assert runner.returncode == expected, stdout + stderr
        wait_for(lambda: not any(running(pid) for pid in owned), 'gate leaked an owned child')
        assert sentinel.poll() is None, 'cleanup signalled an unrelated process'
    finally:
        # Only fixture-owned groups/PIDs, including the intentionally broken control.
        for pgid in [runner.pid] + owned[:1]:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for pid in owned:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        runner.communicate(timeout=5)
        sentinel.terminate(); sentinel.wait(timeout=5)


def test_all_native_launch_sites_inherit_the_managed_gate_group(tmp_path):
    """Execute each shipped launch call with a harmless child replacing its binary."""
    native = ROOT / 'tests/test_native_window_reload_electron.py'
    code = f"""
import ast,importlib.util,os,subprocess,sys
spec=importlib.util.spec_from_file_location('native_fixture',{str(native)!r})
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
os.environ['PC_GATE_MANAGED_PROCESSES']='1'
assert os.getpgrp()==os.getpid()
tree=ast.parse(open({str(native)!r}).read())
seen=[]
for node in ast.walk(tree):
    if not isinstance(node,ast.Assign) or len(node.targets)!=1 or not isinstance(node.targets[0],ast.Name):continue
    name=node.targets[0].id
    if name not in ('compositor','xserver','native') or not isinstance(node.value,ast.Call):continue
    call=node.value
    # Replace executable and IO boundaries only; preserve actual factory and session option.
    call.args=[ast.parse(repr([sys.executable,'-c','import time;time.sleep(30)']),mode='eval').body]
    call.keywords=[kw for kw in call.keywords if kw.arg=='start_new_session']
    proc=eval(compile(ast.fix_missing_locations(ast.Expression(call)),'native-launch','eval'),vars(fixture))
    try:
        assert os.getpgid(proc.pid)==os.getpgrp(),name+' escaped the gate process group'
    finally:
        fixture._stop_native(proc)
    seen.append(name)
assert sorted(seen)==['compositor','native','xserver'],seen
print('all native launch sites stayed owned')
"""
    result = subprocess.run([sys.executable, '-c', code], start_new_session=True,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
