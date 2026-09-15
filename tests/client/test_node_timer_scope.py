"""Scenario cleanup owns its timers and preserves real deadlines and failures."""
from pathlib import Path
import subprocess

SCOPE = Path(__file__).with_name('node_timer_scope.cjs')


def run(body):
    return subprocess.run(['node', '-e', "const scope=require(" + repr(str(SCOPE)) + ");" + body],
                          capture_output=True, text=True, timeout=5)


def test_cleanup_cancels_only_owned_timers_and_keeps_callback_arguments():
    result = run('''
const assert=require('node:assert/strict');
setTimeout(()=>process.stdout.write('outside;'),40);
const close=scope();
setTimeout(()=>{throw Error('completed scenario timer survived');},10000);
setTimeout((arg)=>{assert.equal(arg,'value');process.stdout.write('deadline;');close();close();},10,'value');
''')
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'deadline;outside;'


def test_cleanup_preserves_real_abort_deadline_until_scenario_finishes():
    result = run('''
const assert=require('node:assert/strict');const close=scope();
const controller=new AbortController();
controller.signal.addEventListener('abort',()=>{
  assert(controller.signal.aborted);process.stdout.write('ambiguous send observed');close();
});
setTimeout(()=>controller.abort(),20);
''')
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'ambiguous send observed'


def test_cleanup_does_not_force_exit_or_hide_post_completion_microtask_failure():
    result = run("const close=scope();setTimeout(()=>{},10000);close();Promise.resolve().then(()=>{throw Error('must remain visible');});")
    assert result.returncode != 0
    assert 'must remain visible' in result.stderr


def test_explicit_timer_cancellation_still_works():
    result = run("const close=scope();const t=setTimeout(()=>{throw Error('canceled timer ran');},0);clearTimeout(t);setTimeout(()=>{process.stdout.write('ok');close();},20);")
    assert result.returncode == 0, result.stderr
    assert result.stdout == 'ok'
