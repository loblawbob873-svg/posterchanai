"""The read-state sync keeps ITS document and nothing else -- and so cannot break the desktop.

Found as a regression in deploy 205's gate: from the commit that added `pcai:seen` (76d4a45dc),
test_desktop_native_program_icons_full_app failed every run. The desktop drew its icons, and the
next change to them ("Add a program…" → Steam) was refused with "still loading your desktop
layout". Measured cause, not a guess:

  * the seen sync's live subscription asks for `#d: pcai:seen`, but a relay that does not honour
    `#d` (the fixture relay; real relays exist that do the same) answers with EVERY kind-30078 the
    account has -- including `pcai:desktop`;
  * the subscription saved whatever arrived into the Store, so after the next reload the desktop
    painted its layout FROM CACHE (~300ms) while its own relay read had not answered yet, and
    os.js refuses every write until that read answers (the replaceable-doc rule). Icons on screen,
    every change refused. Before the seen sync nothing put `pcai:desktop` in the Store, the icons
    appeared only once the relay read had answered, and the same click worked.

So the rule asserted here: what that subscription stores is the account's own `pcai:seen`, never
another document a careless relay handed it. The SHIPPED bundle, a relay that ignores `#d`.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# The account's OWN documents on the relay: a real (decryptable) pcai:seen and its desktop layout.
# The fixture relay (test_effects_full_app.INIT) matches kinds/ids/authors and ignores `#d`.
SEED = r'''(()=>{const me=new Uint8Array(32).fill(1),pk=NostrTools.getPublicKey(me),now=Math.floor(Date.now()/1000);
 const ck=NostrTools.nip44.getConversationKey(me,pk);
 const seen=NostrTools.finalizeEvent({kind:30078,created_at:now-60,tags:[['d','pcai:seen']],
   content:NostrTools.nip44.encrypt(JSON.stringify({v:1,notif:now-120,dmAll:0,dm:{}}),ck)},me);
 const desk=NostrTools.finalizeEvent({kind:30078,created_at:now-30,tags:[['d','pcai:desktop']],
   content:NostrTools.nip44.encrypt(JSON.stringify({v:1,order:[],folders:[],hidden:[]}),ck)},me);
 window.__events=[seen,desk];return pk;})()'''

STORED = ("(d=>(window.Store&&Store.query([{authors:[NostrTools.getPublicKey(new Uint8Array(32).fill(1))],"
          "kinds:[30078],'#d':[d]}])||[]).length)")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_seen_subscription_stores_only_pcai_seen():
    async def check(b):
        await b.until("document.body.classList.contains('guest')")
        await b.js(SEED)
        await desktop.login(b)
        # The subscription ran and delivered: its own document is in the Store (positive control,
        # so the assertion below cannot pass merely because nothing was ever delivered).
        await b.until(STORED + "('pcai:seen')>=1")
        await asyncio.sleep(1.5)            # every other answer to the same REQ has landed by now
        assert await b.js(STORED + "('pcai:desktop')") == 0, \
            'the read-state subscription cached the desktop layout a relay handed it'
        assert not await b.js('__errors'), await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check))
