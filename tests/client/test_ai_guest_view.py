import json, subprocess
from pathlib import Path


ROOT=Path(__file__).resolve().parents[2]
APP=(ROOT/'static/js/client/app.js').read_text()


def test_guest_ai_view_paints_login_state_without_signing_or_fetching():
    p=subprocess.run(['node','--unhandled-rejections=strict',str(Path(__file__).with_name('ai_guest_runtime.mjs'))],
                     text=True,capture_output=True,check=True)
    got=json.loads(p.stdout)
    assert 'Sign in with a Nostr account' in got['html']
    assert 'id="ai-signin"' in got['html']
    assert got['requests']==[]


def test_auth_boundary_checks_identity_before_pubkey_or_login_request():
    fn=APP[APP.index('async function ensureAiSession(){'):APP.index('// In-app Admin:',APP.index('async function ensureAiSession(){'))]
    guard=fn.index("if(!ME || !ME.pubkey) throw")
    assert guard < fn.index("[['p', ME.pubkey]]")
