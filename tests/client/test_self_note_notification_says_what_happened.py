"""A server notification (a note-to-self DM) says WHAT happened, not "saved to your notes to self".

Reported: the notification an admin gets when a user applies for access "is nothing about a user
requesting permission -- it's about something being saved". The server delivers every system message
(access applications, agent runs, uptime alerts) as a DM from the operator key, which on a
single-admin node is the admin's own key -- a note to self -- and `_dmNotify` described only that:
"New notification — Saved to your notes to self". This RUNS the shipped `_dmNotify` under node.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from tests.client_source import client_source

SRC = client_source()
FN = SRC[SRC.index("  function _dmNotify("):SRC.index("  // Index DMs WITHOUT decrypting")]

RUN = r"""
const out = [];
const ctx = { settings: {} };
function notificationAllowed(){ return true; }
function notifToast(html){ out.push(['toast', html]); }
function osNotify(title, body){ out.push(['os', title, body]); }
function enc(s){ return String(s).replace(/[&<>"']/g, c => '&#' + c.charCodeAt(0) + ';'); }
const ClientSettings = { get: (k, d) => (k in ctx.settings ? ctx.settings[k] : d) };
const LOGO = 'logo.png';
%s
const APPLY = 'A user applied for an instance NIP-05 name on poster.place.\n\nnostr:npub1syayev4rfepca743vw64dcdqdlqrgkyumpk7mkun2qhqa9xwyhsqse2t5r\n\nOpen their profile → Permissions → NIP-05 to approve a name.';
_dmNotify(null, true, APPLY);
ctx.settings.hideDmPreview = true; _dmNotify(null, true, APPLY);
ctx.settings.hideDmPreview = false; _dmNotify(null, true, '<img src=x onerror=alert(1)> uptime down');
_dmNotify(null, true, '');
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node is required")
def test_a_server_notification_names_the_event():
    r = subprocess.run(["node", "-e", RUN % FN], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    os_ = [o for o in out if o[0] == "os"]
    toasts = [o[1] for o in out if o[0] == "toast"]
    assert all("saved to your notes" not in (o[2] or "").lower() for o in os_), os_
    assert os_[0][2] == "A user applied for an instance NIP-05 name on poster.place.", os_[0]
    assert os_[1][2] == "Open Messages to read it", "hidden previews must not show the text"
    assert "<img" not in toasts[2] and "&#60;img" in toasts[2], "message text must be escaped in the toast"
    assert os_[3][2] == "Open Messages to read it"
