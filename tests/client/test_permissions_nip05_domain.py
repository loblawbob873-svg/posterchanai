"""Additional permissions shows the NODE's NIP-05 domain -- never `@localhost` or `@posterchan`.

Reported: "Additional permissions -> NIP-05 sometimes appears as @localhost or @posterchan". The panel
started from `location.host`, the shell's own origin: `localhost` in the APK (Capacitor serves the
bundle from https://localhost), `posterchan` in the desktop app (app://posterchan). Only a name that
was ALREADY granted carried the real domain back from the server, so the ungranted case -- the one
where the admin is deciding whether to grant it -- showed the wrong address.

Runs the SHIPPED openPermissions (profile.js) under node against a stubbed server, as each shell.
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "static/js/client/profile.js").read_text()


def _fn(src, header):
    i = src.index(header)
    depth, j = 0, src.index("{", i)
    for k in range(j, len(src)):
        depth += {"{": 1, "}": -1}.get(src[k], 0)
        if depth == 0:
            return src[i:k + 1]
    raise AssertionError("unterminated")


HARNESS = r"""
const [fnSrc, origin, apiBase, reply] = process.argv.slice(1);
const u = new URL(origin);
global.location = { host: u.host, origin: u.origin, href: origin };
global.window = { __PC_API_BASE__: apiBase || undefined };
let shown = '';
const S = { IS_ADMIN: true };
const enc = s => String(s);
const profOf = () => ({ name: 'Alice' });
const modal = html => { shown = html; };
const $ = () => ({}), $$ = () => [];
global.fetch = async url => ({ json: async () => (String(url).includes('admin-nip05') ? JSON.parse(reply) : {}) });
eval(fnSrc);
openPermissions('a'.repeat(64)).then(() => {
  const m = shown.match(/NIP-05 <span class="muted small">([^<]*)<\/span>/);
  process.stdout.write(JSON.stringify(m ? m[1] : null));
});
"""


@pytest.mark.parametrize("origin,api_base", [
    ("https://localhost/index.html", "https://poster.place"),          # the APK
    ("app://posterchan/index.html", "https://poster.place"),           # the desktop app
    ("https://poster.place/client", ""),                               # the web client
])
@pytest.mark.parametrize("granted", [False, True])
def test_the_address_uses_the_nodes_domain_in_every_shell(origin, api_base, granted):
    reply = {"ok": True, "name": "alice" if granted else None, "domain": "poster.place",
             "nip05": "alice@poster.place" if granted else None}
    r = subprocess.run(["node", "-e", HARNESS, _fn(SRC, "async function openPermissions(pk){"), origin, api_base,
                        json.dumps(reply)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == "alice@poster.place"


def test_an_older_server_without_domain_falls_back_to_the_instance_not_the_shell():
    reply = {"ok": True, "name": None, "nip05": None}
    r = subprocess.run(["node", "-e", HARNESS, _fn(SRC, "async function openPermissions(pk){"),
                        "https://localhost/index.html", "https://node.example", json.dumps(reply)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == "alice@node.example"


def test_the_server_reports_its_domain_for_an_ungranted_key(monkeypatch):
    import asyncio
    from app.routers import client
    from app.services import settings_store
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: "")
    monkeypatch.setattr(client, "_nip05_domain", lambda request, db: "poster.place")
    resp = asyncio.run(client.admin_nip05_status(pubkey="b" * 64, request=None, db=None))
    body = json.loads(resp.body)
    assert body["name"] is None and body["domain"] == "poster.place"


def test_access_requests_name_the_instance_not_the_shell():
    up = (ROOT / "static/js/client/upload.js").read_text()
    assert not re.search(r"on \$\{location\.host\}", up), "an access request says 'on localhost' in the APK"
