"""Pressing Publish again on an unconfirmed git issue re-sends THAT issue; it never files a second.

Reported (git issue d2376b43): "I get the message 'timeout' but the issue was published already" --
and the report before it sat on the repo four times in 51 seconds, one copy per press. Each press
signed a NEW event (a new created_at is a new id), so every retry after an unconfirmed publish was a
duplicate the moment the first one landed. Runs the shipped form in the bundled client; only
`Relay.publish`'s answer is a fixture.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

REPO = {"id": "c" * 64, "kind": 30617, "pubkey": "d" * 64, "created_at": 1, "sig": "e" * 128, "content": "",
        "tags": [["d", "demo"], ["name", "demo"]]}


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_retried_issue_is_the_same_issue():
    async def check(b):
        await desktop.login(b)
        await b.js("""window.__sent=[]; Relay.publish=async (ev)=>{ __sent.push(ev.id);
            return __sent.length===1 ? {ok:false, msg:'timeout'} : {ok:true, msg:''}; }""")
        await b.js("window.__PC.newRepoIssue(" + json.dumps(REPO) + ")")
        await b.until("!!document.querySelector('#ri-subj')")
        await b.js("""document.querySelector('#ri-subj').value='Cover picker broken';
            document.querySelector('#ri-body').value='It says my connection is down.';
            document.querySelector('#ri-pub').click()""")
        await b.until("window.__sent.length===1 && /confirmed/.test(document.querySelector('#ri-status').textContent)")
        status = await b.js("document.querySelector('#ri-status').textContent")
        assert "copy" in status, f"the timeout message does not say a retry is safe: {status!r}"
        await b.until("!document.querySelector('#ri-pub').disabled")
        await b.js("document.querySelector('#ri-pub').click()")
        await b.until("window.__sent.length===2")
        await asyncio.sleep(0.3)
        sent = await b.js("window.__sent")
        assert sent[0] == sent[1], f"the retry signed a NEW issue -- a duplicate once the first lands: {sent}"
        assert await b.js("!document.querySelector('#ri-subj')"), "the form did not close after the confirmed send"

    asyncio.run(desktop.with_browser("online", "?pcShell=1", check))
