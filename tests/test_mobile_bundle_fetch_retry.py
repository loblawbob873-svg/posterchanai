"""Android must build its shell from the same commit as its JavaScript, even offline."""
from pathlib import Path
import os
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def test_mobile_shell_builds_offline_and_loads_current_membership_module(tmp_path):
    curl=tmp_path/'curl'
    curl.write_text('#!/bin/sh\nexit 99\n')
    curl.chmod(0o755)
    env={**os.environ,'PATH':str(tmp_path)+os.pathsep+os.environ['PATH']}
    result=subprocess.run(['bash',str(ROOT/'mobile/build-www.sh')],env=env,capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stdout+result.stderr
    html=(ROOT/'mobile/www/index.html').read_text()
    assert html.index('static/js/client/instance-access.js')<html.index('static/js/client/app.js')
    assert '{{' not in html and '{%' not in html
    assert (ROOT/'mobile/www/static/js/client/instance-access.js').read_bytes()==(ROOT/'static/js/client/instance-access.js').read_bytes()


def test_shared_renderer_changes_trigger_every_native_build():
    for name in ('android.yml','desktop.yml','android-emulator.yml'):
        assert "'scripts/client_shell.py'" in (ROOT/'.github/workflows'/name).read_text()
