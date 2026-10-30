import re
import json
import subprocess
from pathlib import Path
from tests.client_source import client_source


ROOT = Path(__file__).resolve().parents[2]
APP = client_source()


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
    # awaited before the request -- directly, or inside the time limit that keeps a node that never answers from being
    # a spinner for ever (test_mail_never_waits_for_ever_full_app); the rule, not the spelling
    assert re.search(r"await (?:limit\([^;]*?)?ensureAiSession\(\)", mail[:mail.index("fetch('/api/mail'+path")]), mail[:600]
    assert 'catch' not in mail[:mail.index("fetch('/api/mail'+path")]

    settings_start=APP.index('async function renderUserSettings()')
    settings=APP[settings_start:settings_start+50000]
    auth=settings.index('const sessionP=ensureAiSession();')
    fetch=settings.index("fetch('/api/auth/settings')")
    # no protected GET without a session: a failed one AND one still pending after the cap both leave first
    assert auth < fetch and 'authError=e; break' in settings[auth:fetch] and 'lateSession=sessionP; break' in settings[auth:fetch]
    # A failed session no longer REPLACES Settings (relays and every on-device pane still work): it marks the
    # server half unavailable, which hides the server tabs and keeps Save on this device.
    assert '_settingsServerDown = authError ?' in settings and 'could not establish your app session' in settings
