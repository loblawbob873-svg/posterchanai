"""Signing in with Amber (NIP-55, an on-device Android signer) and posting through it, in the shipped client on a real
node -- and a signer that hands back something other than what it was asked to sign is refused.

  The bridge  `window.Capacitor.Plugins.Nip55`, the APK's Nip55Plugin.java, is STUBBED -- at its exact interface:
              isAvailable() -> {available, signers:[{package,label,self}]}; request({type, content, pubkey, ...}) ->
              {result, event, id, package, rejected, viaResolver}, the shape Nip55Plugin.java resolves for both the
              intent path and the silent content-resolver path. Amber itself and Android's intents cannot run here,
              so the stub plays Amber: it signs with the account's real key (NostrTools in the page), and the TAMPER
              mode makes it alter the event first (a buggy or hostile signer -- the signature is valid, the words are
              not the user's).
  The rest    the shipped client (login screen -> "Sign in with Amber" -> the composer -> Send), the shipped relay.

What is held: a post signed through Amber reaches the relay exactly as typed; a signed event whose content, tags or
kind differ from the template is refused, said ("other than what you asked"), never published, and the text is kept
as a draft -- the same rule signer.js applies to a NIP-46 remote signer (test_signer_flows_full_app.py).

Harness: tests/client/discover_flows_harness.py. Fail-first: PC_JOURNEY_OVERRIDE="/static/js/client/signer.js=...".
"""
import asyncio
import json

import pytest

from tests.client import discover_flows_harness as F
from tests.client import realnode_harness as R
from tests.client.test_signer_flows_full_app import DRAFT, compose, notes

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
FEATURE_COVERS = ('signer',)
FIRST_FILL = 220           # 220 .. (account keys are one byte; each module has its own node)
SCENARIOS = {}
COST = {}


def scenario(fn):
    SCENARIOS[fn.__name__] = fn
    return fn


@pytest.fixture(scope='module')
def ran(request):
    return F.run(request, SCENARIOS, COST, FIRST_FILL)


# Amber, at the Nip55 plugin's interface. __FILL__ is the account's key byte; __TAMPER__ '', 'content', 'tags', 'kind'.
AMBER = r'''
(()=>{const FILL=__FILL__, TAMPER=__TAMPER__;
  const key=()=>new Uint8Array(32).fill(FILL);
  const pk=()=>NostrTools.getPublicKey(key());
  const ck=(peer)=>NostrTools.nip44.getConversationKey(key(),peer);
  window.__amberAsked=[];
  const Nip55={
    async isAvailable(){return {available:true,signers:[{package:'com.greenart7c3.nostrsigner',label:'Amber',self:false}]};},
    async request(o){
      const ok=(x)=>Object.assign({package:'com.greenart7c3.nostrsigner',rejected:false,viaResolver:true,id:o.id||null,event:null},x);
      if(o.type==='get_public_key')return ok({result:NostrTools.nip19.npubEncode(pk())});
      if(o.type==='sign_event'){
        const t=JSON.parse(o.content);window.__amberAsked.push(t.kind);
        if(t.kind===1&&TAMPER==='content')t.content=String(t.content)+' [edited by the signer]';
        if(t.kind===1&&TAMPER==='tags')t.tags=(t.tags||[]).concat([['p','00'.repeat(32)]]);
        if(t.kind===1&&TAMPER==='kind')t.kind=3;
        const ev=NostrTools.finalizeEvent({kind:t.kind,content:t.content,tags:t.tags||[],created_at:t.created_at||Math.floor(Date.now()/1000)},key());
        return ok({result:ev.sig,event:JSON.stringify(ev)});
      }
      if(o.type==='nip44_encrypt')return ok({result:NostrTools.nip44.encrypt(o.content,ck(o.pubkey))});
      if(o.type==='nip44_decrypt')return ok({result:NostrTools.nip44.decrypt(o.content,ck(o.pubkey))});
      if(o.type==='nip04_encrypt')return ok({result:await NostrTools.nip04.encrypt(key(),o.pubkey,o.content)});
      if(o.type==='nip04_decrypt')return ok({result:await NostrTools.nip04.decrypt(key(),o.pubkey,o.content)});
      return {rejected:true,error:'unsupported '+o.type};
    }};
  window.Capacitor=Object.assign(window.Capacitor||{},{isNativePlatform:()=>false,getPlatform:()=>'web',
    Plugins:Object.assign((window.Capacitor&&window.Capacitor.Plugins)||{},{Nip55})});
})();
'''


async def amber_device(S, tamper=''):
    js = AMBER.replace('__FILL__', str(S.fill(0))).replace('__TAMPER__', json.dumps(tamper))
    d = await S.device(0, login=False, pre_init=js)
    await F.wait(d, "!!document.querySelector('#btn-nip55') && !document.querySelector('#btn-nip55').classList.contains('hidden')",
                 30, 'the "Sign in with Amber" button')
    await d.b.js("document.querySelector('#btn-nip55').click();true")
    await F.wait(d, "!!__PC.me() && __PC.me().mode==='nip55' && __PC.me().pubkey===%s && Relay.status==='ok'"
                 % json.dumps(S.pk(0)), 60, 'signed in through Amber')
    return d


@scenario
async def a_post_signed_by_amber_reaches_the_relay_as_typed(S):
    d = await amber_device(S)
    text = 'signed on this phone by Amber %d' % S.base
    await compose(d, text)
    got = await F.until(lambda: notes(S.n, S.pk(0), text), 30, 'the post on the relay', d)
    assert got[0]['pubkey'] == S.pk(0)


@CHROME
def test_a_post_signed_by_amber_reaches_the_relay_as_typed(ran):
    ran('a_post_signed_by_amber_reaches_the_relay_as_typed')


async def _refused(S, tamper):
    d = await amber_device(S, tamper)
    text = 'exactly these words, %s %d' % (tamper, S.base)
    await compose(d, text)
    await F.wait(d, "(window.__toastLog||[]).some(t=>/other than what you asked/.test(t))", 20, 'the refusal said')
    loop = asyncio.get_running_loop()
    end = loop.time() + 3
    while loop.time() < end:
        assert not [e for e in S.n.home.query({'kinds': [1, 3], 'authors': [S.pk(0)]}) if text in e['content']], \
            'an event the user did not ask for was published under their name (%s altered)' % tamper
        await asyncio.sleep(.5)
    assert text in await d.b.js(DRAFT), 'the text is not kept as a draft'


@scenario
async def amber_returning_other_words_is_refused(S):
    await _refused(S, 'content')


@CHROME
def test_amber_returning_other_words_is_refused(ran):
    ran('amber_returning_other_words_is_refused')


@scenario
async def amber_returning_other_tags_is_refused(S):
    await _refused(S, 'tags')


@CHROME
def test_amber_returning_other_tags_is_refused(ran):
    ran('amber_returning_other_tags_is_refused')


@scenario
async def amber_returning_another_kind_is_refused(S):
    await _refused(S, 'kind')


@CHROME
def test_amber_returning_another_kind_is_refused(ran):
    ran('amber_returning_another_kind_is_refused')
