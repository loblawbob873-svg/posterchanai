"""PosterChanOS hands PosterFox's Passwords add-on its pairing, with nothing to type (vault.js _syncOS + the desktop's
desktop/browserpairing.js). "we need to make sure that the passwords extension automatically connects to posterchanOS
without any config" (2026-10-29)."""
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which('node') is None, reason='Node is required')
def test_the_vault_hands_the_desktop_this_accounts_pairing():
    r = subprocess.run(['node', str(Path(__file__).with_name('vault_os_pairing_runtime.mjs'))],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(shutil.which('node') is None, reason='Node is required')
def test_the_desktop_writes_firefox_managed_storage_for_this_user_only(tmp_path):
    import base64
    code = base64.b64encode(json.dumps({"v": 1, "t": "pcvault", "pubkey": "a1" * 32, "key": "k"}).encode()).decode()
    js = """
const bp = require(%s), home = %s;
const out = [bp.set(home, %s), bp.set(home, %s)];
let refused = ''; try { bp.set(home, 'bm90IGEgY29kZQ=='); } catch (e) { refused = e.message; }
out.push(refused, bp.file(home));
console.log(JSON.stringify(out));
""" % (json.dumps(str(ROOT / 'desktop/browserpairing.js')), json.dumps(str(tmp_path)), json.dumps(code), json.dumps(code))
    r = subprocess.run(['node', '-e', js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    first, second, refused, path = json.loads(r.stdout)
    assert (first, second) == ('written', 'unchanged') and refused
    f = Path(path)
    assert f == tmp_path / '.mozilla/managed-storage/passwords@poster.place.json'
    doc = json.loads(f.read_text())
    assert doc['name'] == 'passwords@poster.place' and doc['type'] == 'storage' and doc['data']['pairing'] == code
    assert stat.S_IMODE(f.stat().st_mode) == 0o600 and stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    r = subprocess.run(['node', '-e', 'console.log(require(%s).set(%s, null))' % (
        json.dumps(str(ROOT / 'desktop/browserpairing.js')), json.dumps(str(tmp_path)))], capture_output=True, text=True)
    assert r.stdout.strip() == 'removed' and not f.exists()


def test_the_bridge_is_wired_shell_only_and_cleared_at_sign_out():
    main = (ROOT / 'desktop/main.js').read_text()
    h = main[main.index("ipcMain.handle('pc:browser-pairing'"):][:300]
    assert 'SHELL_MODE' in h and 'fromOurPage(e)' in h
    assert "setBrowserPairing: (code) => ipcRenderer.invoke('pc:browser-pairing'" in (ROOT / 'desktop/preload.js').read_text()
    app = (ROOT / 'static/js/client/app.js').read_text()
    assert app.count('window.pcShell?.setBrowserPairing?.(null)') == 2      # logout and account switch
