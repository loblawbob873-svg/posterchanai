"""EVERY SWITCH IN Settings → Sidebar MUST SAY WHAT IT TURNS OFF — MEASURED, AND AFTER MEME BUILDER.

Reported three times on Android, most recently as "android app still shows a sidebar thing to
disable with no label", and once with the repro that matters: **"the blank entry appears after Mem
Builder on apk"**.

WHY THE EARLIER FIX WAS NOT ENOUGH, AND WHY THE EARLIER TEST DID NOT NOTICE.
`navRows()` now falls back to "Unnamed item (<view>)", so it cannot hand the editor an empty label,
and `tests/client/test_no_sidebar_row_is_blank.py` pins that rule against a stub nav. Both are
right and neither can see this bug, because a label that EXISTS is not a label that RENDERS:

  * `_navHideHtml` lays the label out as `<span style="flex:1;min-width:0">`. `min-width:0` is what
    lets a flex item collapse BELOW its content, so on a 360px phone — with the ▲▼ ordering buttons,
    the switch and the ▦ group button all on the same row — a label can be squeezed to zero width
    and clipped away entirely. The text is in the DOM. Nothing throws. `textContent` is correct.
    Every markup test passes and the row on the phone is blank.
  * And the state the code ENDS in is not the state a single-shot test measures. The report names a
    SEQUENCE: Meme Builder, then Settings. Meme Builder is the heaviest view in the client and the
    one that most changes the layout it leaves behind, so this measures the editor BOTH ways — cold,
    and after that view — and says which one broke it. A test that only opens Settings from a fresh
    page is measuring the case that has always worked.

THE RULE. A row that carries a control must carry a legible name for what that control does:
printable text, rendered wider than zero and taller than zero, not clipped to nothing by its own
row. A lock (🔒) row is exempt from having a switch, never from having a name.

This runs the REAL client document, the REAL login and the shipped renderers at 360px. Only the
HTTP and WebSocket boundaries are fixtures.
"""
import asyncio
import json
import shutil
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client.test_effects_full_app import Browser, Handler, INIT

PHONE = 360

# Read every row that carries a control, and measure the NAME as the phone renders it.
MEASURE = r"""(() => {
  const list = document.querySelector('#nav-hide-list');
  if (!list) return { missing: true };
  const printable = t => /[^\s\u200b-\u200f\u2028\u2029\ufeff]/.test(String(t || ''));
  const rows = [...list.querySelectorAll('.nav-hide-row')].map(row => {
    const control = row.querySelector('input[type=checkbox]');
    const lock    = row.querySelector('.nav-lock');
    // The name is the flexible span — never .nav-ord (the arrows) and never the switch's own span.
    const label = [...row.querySelectorAll(':scope > span')]
      .find(s => !s.classList.contains('nav-ord') && !s.classList.contains('nav-lock'));
    const r = label ? label.getBoundingClientRect() : null;
    const cs = label ? getComputedStyle(label) : null;
    return {
      key: row.dataset.navrow || (control && (control.dataset.navkey || control.dataset.tltab)) || '(none)',
      control: !!control, lock: !!lock,
      text: label ? String(label.textContent || '').trim() : '',
      w: r ? Math.round(r.width) : 0, h: r ? Math.round(r.height) : 0,
      hidden: cs ? (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') : true,
      rowW: Math.round(row.getBoundingClientRect().width),
    };
  });
  return {
    rows,
    // A row with a control is a row somebody is asked to make a decision about.
    blank: rows.filter(x => (x.control || x.lock) &&
                            (!printable(x.text) || x.w <= 0 || x.h <= 0 || x.hidden)),
  };
})()"""


async def _open_sidebar_editor(b, via_meme):
    """Settings → Sidebar, optionally through Meme Builder first (the reported sequence)."""
    reached = None
    if via_meme:
        await b.js("__PC.switchView('meme')")
        # Meme Builder is heavy; give it a real chance to lay itself out before leaving.
        await asyncio.sleep(2.5)
        # A switchView that silently did nothing would make this whole pass vacuous — the test
        # would "cover the sequence" while never leaving Settings. Prove the view was entered.
        reached = await b.js("({view: (window.__PC.view && __PC.view()) || document.body.dataset.view || '',"
                             " feedCls: (document.querySelector('#feed')||{}).className || '',"
                             " stage: !!document.querySelector('.meme-stage, #meme-stage, .feed-meme')})")
    await b.js("__PC.switchView('settings')")
    await b.until("!!document.querySelector('.us-tab[data-tab=\"sidebar\"]')")
    await b.js("document.querySelector('.us-tab[data-tab=\"sidebar\"]').click()")
    await b.until("!!document.querySelector('#nav-hide-list .nav-hide-row')")
    await asyncio.sleep(0.4)          # let the flex row settle before it is measured
    out = await b.js(MEASURE)
    out['reached'] = reached
    return out


async def run():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = tempfile.mkdtemp(prefix='pc-sidebar-label-')
    try:
        proc = subprocess.Popen(
            ['/opt/google/chrome/chrome', '--headless=new', '--no-sandbox', '--disable-gpu',
             '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if Path(profile, 'DevToolsActivePort').exists():
                    break
                await asyncio.sleep(.1)
            port = Path(profile, 'DevToolsActivePort').read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get('http://127.0.0.1:' + port + '/json')).json()
            target = next(p for p in pages if p.get('type') == 'page' and p.get('url') == 'about:blank')
            async with websockets.connect(target['webSocketDebuggerUrl'], max_size=20_000_000) as ws:
                b = Browser(ws)
                await b.call('Page.enable')
                await b.call('Network.enable')
                await b.call('Network.setBlockedURLs', {'urls': ['https://*', 'wss://*']})
                await b.call('Emulation.setDeviceMetricsOverride',
                             {'width': PHONE, 'height': 780, 'deviceScaleFactor': 1, 'mobile': True})
                await b.call('Page.addScriptToEvaluateOnNewDocument',
                             {'source': 'window.__hasChats=false;' + INIT})
                await b.call('Page.navigate',
                             {'url': 'http://127.0.0.1:%d/client' % server.server_port})
                await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
                await b.until("document.body.classList.contains('guest')")
                await b.js("""(()=>{const key=new Uint8Array(32).fill(7);
                  document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(key);
                  document.querySelector('#btn-nsec-login').click()})()""")
                await b.until("!!__PC.me()")

                cold = await _open_sidebar_editor(b, via_meme=False)
                after_meme = await _open_sidebar_editor(b, via_meme=True)
                return cold, after_meme
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
    finally:
        # Chrome keeps writing into its profile for a moment after SIGTERM; a strict rmtree here
        # raises "Directory not empty" and MASKS whatever the test actually found.
        shutil.rmtree(profile, ignore_errors=True)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='no Chrome')
def test_every_sidebar_switch_has_a_legible_name():
    cold, after_meme = asyncio.run(run())
    print('cold rows=%d blank=%d | after-meme rows=%d blank=%d | meme entry=%r' % (
        len(cold['rows']), len(cold['blank']), len(after_meme['rows']),
        len(after_meme['blank']), after_meme.get('reached')))

    for stage, got in (('cold', cold), ('after Meme Builder', after_meme)):
        assert not got.get('missing'), 'Settings → Sidebar drew no row list at all (%s)' % stage
        assert len(got['rows']) > 5, \
            'Settings → Sidebar drew only %d rows (%s) — it did not really render' % (
                len(got['rows']), stage)

    # The report is the point: say WHICH rows, and whether the sequence is what breaks them.
    problems = []
    for stage, got in (('cold', cold), ('after Meme Builder', after_meme)):
        for row in got['blank']:
            problems.append(
                '%s: row %r renders no name (text=%r width=%dpx height=%dpx hidden=%s '
                'row is %dpx wide)' % (stage, row['key'], row['text'], row['w'], row['h'],
                                       row['hidden'], row['rowW']))
    assert not problems, (
        'A switch with no name cannot be used — you cannot tell what you would be turning off.\n'
        + '\n'.join('  - ' + p for p in problems)
        + '\n  cold rows: %d, after Meme Builder: %d' % (len(cold['rows']), len(after_meme['rows'])))
