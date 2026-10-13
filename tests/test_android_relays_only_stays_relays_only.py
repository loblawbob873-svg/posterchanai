""""Use relays only (no server)" on the APK must STAY relays only.

Reported (Vyuram): "when you hit use relays only no servers it goes back to poster.place & uses it anyways."
Settings stores the choice as an EMPTY `pc_instance`; the APK's boot shim (mobile/build-www.sh) read it with
`if(s) return s; return 'https://poster.place'` -- an empty string is falsy, so the choice was read as "never
chosen" and every reload talked to poster.place again. The desktop app was always right (main.js keeps '' and
defaults only when nothing was ever chosen). This RUNS the shipped shim's expression under node.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

BUILD = (Path(__file__).resolve().parents[1] / 'mobile/build-www.sh').read_text(encoding='utf-8')


def _base(stored):
    line = next(l for l in BUILD.splitlines() if l.startswith('window.__PC_API_BASE__ ='))
    expr = line.split('=', 1)[1].strip().rstrip(';').replace('\\\\', '\\')   # the heredoc doubles backslashes
    js = ('const store=%s;const localStorage={getItem:k=>k==="pc_instance"?store:null};'
          'const window={};console.log(JSON.stringify(%s));') % (json.dumps(stored), expr)
    out = subprocess.run(['node', '-e', js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(not shutil.which('node'), reason='node required')
def test_relays_only_is_kept_and_only_a_never_chosen_install_gets_the_default():
    assert _base(None) == 'https://poster.place'            # a fresh install: the default
    assert _base('') == '', 'relays only was read as "never chosen" and the app went back to poster.place'
    assert _base('https://my.example/') == 'https://my.example'
