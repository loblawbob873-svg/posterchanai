import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / 'static/js/client/app.js').read_text()


def test_signer_failure_rejects_and_theme_issues_no_protected_get():
    run = subprocess.run(
        ['node', str(Path(__file__).with_name('auth_signer_failure_runtime.mjs'))],
        text=True, capture_output=True, check=True,
    )
    got=json.loads(run.stdout)
    assert 'Firefox signer permission was denied' in got['surfaced']
    assert got['requests'] == []


def test_mail_and_settings_gate_fetch_on_successful_auth():
    mail=APP[APP.index('    async api(path, opts={})'):APP.index('    async render(root)', APP.index('    async api(path, opts={})'))]
    assert 'await ensureAiSession();' in mail
    assert 'catch' not in mail[:mail.index("fetch('/api/mail'+path")]

    settings_start=APP.index('async function renderUserSettings()')
    settings=APP[settings_start:settings_start+50000]
    auth=settings.index('try{ await ensureAiSession(); }')
    fetch=settings.index("fetch('/api/auth/settings')")
    assert auth < fetch and 'authError=e; break' in settings[auth:fetch]
    assert 'if(authError)' in settings and 'could not establish your app session' in settings
