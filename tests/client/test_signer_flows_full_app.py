"""The Signer screen (this app as a NIP-46 remote signer for other apps), driven end to end between two real devices.

  Signer   a device signed in with its own key opens Signer -> "Connect an app with a link" (the shipped
           Nip46Signer, settings.js) and hands out the bunker:// link it shows.
  App      a SECOND device, signed in with NO key: the login screen -> "PosterChan / remote signer" -> paste that
           link -> Connect (the shipped Nip46 client). Everything it signs is a NIP-46 request over THIS node's relay
           (the shipped RelayServer -- kind 24133 is ephemeral, fanned out to whoever listens) and is answered by the
           first device. Both halves are the real producers of each other's traffic; nothing is stubbed.

  Flows    pair -> the app is listed on the signer -> a post made on the app reaches the relay signed by the
           signer's key; the signer device reloading (its pairings come back) and the app still signs; revoke ->
           the app's next action is refused, said, and nothing is published; the signer stopping mid-action ->
           the app says it is waiting within seconds and its draft is kept; a link is single use; a device signed
           in through a remote signer has nothing to hand out (the screen says so, never a broken button).

Harness: tests/client/discover_flows_harness.py. Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/signer.js=...".
"""
import asyncio
import json

import pytest

from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FEATURE_COVERS = ('signer',)
FIRST_FILL = 130            # 130 .. (account keys are one byte; each module has its own node)
SCENARIOS = {}
COST = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


@pytest.fixture(scope='module')
def ran(request):
    return F.run(request, SCENARIOS, COST, FIRST_FILL)


APPS = "[...document.querySelectorAll('#signer-apps [data-revoke]')].map(b=>b.dataset.revoke)"


async def open_signer(d):
    await d.b.js("__PC.switchView('signer');true")
    await F.wait(d, "!!document.querySelector('#signer-apps') && !!document.querySelector('#signer-scan-qr')", 30,
                 'the Signer screen')


async def mint_link(d):
    await open_signer(d)
    await F.wait(d, "!!document.querySelector('#signer-bunker')", 15, 'Connect an app with a link')
    await d.b.js("document.querySelector('#signer-bunker').click();true")
    await F.wait(d, "!!document.querySelector('#bunker-uri') && /^bunker:\\/\\//.test(document.querySelector('#bunker-uri').value)",
                 15, 'the bunker link')
    # The sheet stays open while the other app pastes it: Done ends the offer (and its socket) by design.
    return await d.b.js("document.querySelector('#bunker-uri').value")


async def app_device(S, uri, i=3):
    """A second device that signs in with NO key of its own: paste the link on the login screen."""
    app = await S.device(i, login=False)
    await app.b.js("document.querySelector('#btn-amber').click();true")
    await F.wait(app, "!!document.querySelector('#amber-input') && !document.querySelector('#auth-amber').classList.contains('hidden')",
                 10, 'the remote signer login')
    await app.b.js("(u=>{const i=document.querySelector('#amber-input');i.value=u;i.dispatchEvent(new Event('input',{bubbles:true}));"
                   "document.querySelector('#btn-amber-connect').click();return true})(%s)" % json.dumps(uri))
    return app


async def paired(S, tag=''):
    sig = await S.device(0, tag=tag)
    uri = await mint_link(sig)
    app = await app_device(S, uri)
    await F.wait(app, "!!__PC.me() && __PC.me().pubkey===%s && __PC.me().mode==='nip46' && Relay.status==='ok'"
                 % json.dumps(S.pk(0)), 30, 'the app signed in through the signer')
    await F.wait(sig, "%s.length===1" % APPS, 15, 'the app listed on the signer')
    await sig.b.js("document.querySelector('#bunker-done') && document.querySelector('#bunker-done').click();true")
    return sig, app


def notes(n, pk, text):
    return [e for e in n.home.query({'kinds': [1], 'authors': [pk]}) if e['content'] == text]


async def compose(d, text):
    await F.view(d, 'home', "!!document.querySelector('#tl-fab')")
    await d.b.js("document.querySelector('#tl-fab').click();true")
    await F.wait(d, "!!document.querySelector('#cmp') && !!document.querySelector('#cmp-send')", 20, 'the composer')
    await d.b.js("(()=>{const t=document.querySelector('#cmp');t.focus();t.value=%s;"
                 "t.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#cmp-send').click();return true})()"
                 % json.dumps(text))


async def reload(d, settled=True):
    """Reload the page and wait for the NEW page (a marker set on the old one must be gone)."""
    await d.b.js("window.__pcOld=true;location.reload();true")
    await F.wait(d, "!window.__pcOld && !!window.__PC && !!__PC.me()%s" % (" && Relay.status==='ok'" if settled else ''),
                 60, 'the device back after a reload')


DRAFT = "(localStorage.getItem('pc_drafts_'+__PC.me().pubkey)||'')"


async def not_published(S, d, text, seconds):
    """`text` was NOT published within `seconds` -- the draft holds it, and the app SAID it is waiting."""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while loop.time() < end:
        assert not notes(S.n, S.pk(0), text), 'published without the signer'
        await asyncio.sleep(.5)
    assert text in await d.b.js(DRAFT), 'the text is not kept as a draft'


@scenario
async def a_paired_app_signs_a_real_post_through_this_device(S):
    sig, app = await paired(S)
    text = 'signed by the device in my pocket %d' % S.base
    ev = await F.post_note(app, text)
    assert ev['pubkey'] == S.pk(0)
    # The app's own Signer screen: it holds no key, so it has nothing to hand out -- said, never a broken button.
    await open_signer(app)
    assert await app.b.js("document.querySelector('#signer-scan-qr').disabled")
    assert 'Sign in here with a local key first' in await app.b.js("document.querySelector('.signer-app').innerText")
    assert not await app.b.js("!!document.querySelector('#signer-bunker')")


@CHROME
def test_a_paired_app_signs_a_real_post_through_this_device(ran):
    ran('a_paired_app_signs_a_real_post_through_this_device')


@scenario
async def the_signer_reloading_keeps_its_pairing_and_answers(S):
    sig, app = await paired(S)
    await reload(sig)
    await open_signer(sig)
    await F.wait(sig, "%s.length===1" % APPS, 15, 'the pairing still listed after the reload')
    ev = await F.post_note(app, 'after the signer restarted %d' % S.base)
    assert ev['pubkey'] == S.pk(0)


@CHROME
def test_the_signer_reloading_keeps_its_pairing_and_answers(ran):
    ran('the_signer_reloading_keeps_its_pairing_and_answers')


@scenario
async def a_revoked_app_can_no_longer_sign(S):
    sig, app = await paired(S)
    await sig.b.js("document.querySelector('#signer-apps [data-revoke]').click();true")
    await F.wait(sig, "%s.length===0 && /No apps are signed in/.test(document.querySelector('#signer-apps').innerText)" % APPS,
                 10, 'the app gone from the list')
    await reload(sig)
    await open_signer(sig)
    await F.wait(sig, "/No apps are signed in/.test(document.querySelector('#signer-apps').innerText)", 15,
                 'still revoked after a reload')
    text = 'should never be signed %d' % S.base
    await compose(app, text)
    await F.wait(app, "(window.__toastLog||[]).some(t=>/waiting for your signer/.test(t))", 10, 'the app says it is waiting')
    await not_published(S, app, text, 6)


@CHROME
def test_a_revoked_app_can_no_longer_sign(ran):
    ran('a_revoked_app_can_no_longer_sign')


@scenario
async def a_signer_that_stops_answering_mid_action_is_said_and_keeps_the_text(S):
    sig, app = await paired(S)
    await sig.__aexit__()                         # the phone holding the key goes away
    S.open.remove(sig)
    text = 'typed while the signer was gone %d' % S.base
    await compose(app, text)
    await F.wait(app, "(window.__toastLog||[]).some(t=>/waiting for your signer/.test(t))", 8,
                 'the app says it is waiting, within seconds')
    await not_published(S, app, text, 4)
    # The app is not frozen: another screen opens while the signature is outstanding.
    await F.view(app, 'notifications', "__PC.isView && __PC.isView('notifications')", 10)


@CHROME
def test_a_signer_that_stops_answering_mid_action_is_said_and_keeps_the_text(ran):
    ran('a_signer_that_stops_answering_mid_action_is_said_and_keeps_the_text')


@scenario
async def a_request_sent_while_the_signer_was_still_resubscribing_lands(S):
    """The signer device comes back (a reload: a phone out of doze) and THIS node's relay is slow to take its
    subscription -- 5 s. A post made in that gap is fanned out to nobody (kind 24133 is ephemeral); the app's
    re-send must still get it signed and published."""
    gate = getattr(S.n, '_signer_gate', None) or F.Gate(S.n.home)
    S.n._signer_gate = gate
    tag = 'sg%d' % S.base
    sig, app = await paired(S, tag=tag)
    await open_signer(sig)
    rule = gate.add(lambda filters, conn: 5 if F.device_of(conn) == tag and F.asks(filters, kinds=[24133]) else None)
    try:
        await reload(sig, settled=False)
        text = 'sent while the signer was coming back %d' % S.base
        await compose(app, text)
        await F.until(lambda: notes(S.n, S.pk(0), text), 40, 'the post signed and published after the gap', app)
    finally:
        gate.drop(rule)


@CHROME
def test_a_request_sent_while_the_signer_was_still_resubscribing_lands(ran):
    ran('a_request_sent_while_the_signer_was_still_resubscribing_lands')


# A signer that signs something else: it rewrites the content of every kind-1 it is asked to sign before signing it
# with the real key (a buggy or hostile signer -- the signature is valid, the words are not the user's).
TAMPER = r'''
(()=>{let real=null;Object.defineProperty(window,'PCSignerFactory',{configurable:true,
  set(f){real=f;},get(){return real&&function(dep){const m=real(dep);const N=m.Nip46Signer;const h=N._handle;
    N._handle=async function(method,params){if(method==='sign_event'){let t=params[0];if(typeof t==='string')t=JSON.parse(t);
      if(t.kind===1){t.content=String(t.content)+' [edited by the signer]';params=[JSON.stringify(t)].concat(params.slice(1));}}
      return h.call(this,method,params);};return m;};}});})();
'''


@scenario
async def a_signer_that_alters_what_it_signs_is_refused(S):
    sig = await S.device(0, pre_init=TAMPER)
    uri = await mint_link(sig)
    app = await app_device(S, uri)
    await F.wait(app, "!!__PC.me() && __PC.me().mode==='nip46' && Relay.status==='ok'", 30, 'the app signed in')
    text = 'exactly these words %d' % S.base
    await compose(app, text)
    await F.wait(app, "(window.__toastLog||[]).some(t=>/other than what you asked/.test(t))", 20, 'the refusal said')
    loop = asyncio.get_running_loop()
    end = loop.time() + 3
    while loop.time() < end:
        assert not [e for e in S.n.home.query({'kinds': [1], 'authors': [S.pk(0)]}) if text in e['content']], \
            'a note the user did not write was published under their name'
        await asyncio.sleep(.5)
    assert text in await app.b.js(DRAFT), 'the text is not kept as a draft'


@CHROME
def test_a_signer_that_alters_what_it_signs_is_refused(ran):
    ran('a_signer_that_alters_what_it_signs_is_refused')
