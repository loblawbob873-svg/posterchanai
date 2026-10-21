"""Telegram's chat menu (mute, sound, auto-delete, block) and a screen that comes back by itself.

Reported 2026-10-21: "no way to block, mute notification sounds from user, set chat expiration" and "Telegram needs
a retry to connect if server restarts or down. I started windows app and telegram was a black screen until I
restarted posterchan". Real bundled client against the suite's fake /api/tgc (test_telegram_client_full_app.FAKE),
extended with the settings routes and a switch that takes the node down. Computer and tablet width (a phone gets
"Telegram lives on your phone" by design).
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_telegram_client_full_app import FAKE, _open

EXTRA = r"""
window.__tgPeer = {kind:'user', blocked:false, muted:false, muted_until:0, silent:false, ttl:0};
window.__tgCalls = []; window.__tgDown = false;
const __f1 = window.fetch;
window.fetch = function(url, opts){
  const u = String(url);
  if(u.includes('/api/tgc/') && window.__tgDown){ window.__tgDownHits = (window.__tgDownHits||0) + 1;
    return new Promise(() => {}); }                       // a node mid-restart: the request never answers
  if(!u.includes('/api/tgc/')) return __f1(url, opts);
  const path = u.replace(/^.*\/api\/tgc/, ''), body = opts && opts.body ? JSON.parse(opts.body) : null;
  const j = o => Promise.resolve(new Response(JSON.stringify(o), {status:200, headers:{'Content-Type':'application/json'}}));
  if(path.startsWith('/peer/')) return j(Object.assign({ok:true}, __tgPeer));
  if(path.startsWith('/notify')){ __tgCalls.push(['notify', body]);
    if('mute' in body && body.mute !== null) __tgPeer.muted = body.mute !== 0;
    if('silent' in body && body.silent !== null) __tgPeer.silent = body.silent;
    return j(Object.assign({ok:true}, __tgPeer)); }
  if(path.startsWith('/block')){ __tgCalls.push(['block', body]); __tgPeer.blocked = body.blocked; return j({ok:true, blocked:body.blocked}); }
  if(path.startsWith('/ttl')){ __tgCalls.push(['ttl', body]); __tgPeer.ttl = body.period; return j({ok:true, ttl:body.period}); }
  return __f1(url, opts);
};
"""


async def menu(b, item):
    await b.js("document.querySelector('[data-act=\"chat-menu\"]').click()")
    await b.until("!!document.querySelector('.menu-pop [data-m=\"%s\"]')" % item)
    await b.js("document.querySelector('.menu-pop [data-m=\"%s\"]').click()" % item)


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 820])
def test_the_chat_menu_mutes_silences_sets_auto_delete_and_blocks(width):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=900, deviceScaleFactor=1, mobile=False))
        await _open(b)
        await b.until("document.querySelectorAll('.tg-dialog').length===2")
        await b.js("document.querySelector('.tg-dialog[data-chat=\"42\"]').click()")
        await b.until("!!document.querySelector('[data-act=\"chat-menu\"]')")
        r = await b.js("(()=>{const e=document.querySelector('[data-act=\"chat-menu\"]'),r=e.getBoundingClientRect();return r.width>0&&r.right<=innerWidth;})()")
        assert r, "the chat menu button is not on screen"
        await menu(b, "mute3600")
        await b.until("__tgCalls.length===1")
        call = await b.js("__tgCalls[0]")
        assert call == ["notify", {"chat_id": 42, "mute": 3600}], call
        await menu(b, "unmute")                        # the menu now offers Unmute: it re-read the setting
        await b.until("__tgCalls.length===2")
        assert await b.js("__tgCalls[1]") == ["notify", {"chat_id": 42, "mute": 0}]
        await menu(b, "silent")
        await b.until("__tgCalls.length===3")
        assert await b.js("__tgCalls[2]") == ["notify", {"chat_id": 42, "silent": True}]
        await menu(b, "ttl")
        await b.until("!!document.querySelector('.tg-ttl [data-ttl=\"604800\"]')")
        await b.js("document.querySelector('.tg-ttl [data-ttl=\"604800\"]').click()")
        await b.until("__tgCalls.length===4")
        assert await b.js("__tgCalls[3]") == ["ttl", {"chat_id": 42, "period": 604800}]
        await b.js("window.__PC.uiConfirm=async()=>true; true")
        await menu(b, "block")
        await b.until("__tgCalls.length===5")
        assert await b.js("__tgCalls[4]") == ["block", {"chat_id": 42, "blocked": True}]
        # A group has no Block (Telegram blocks people, not groups).
        await b.js("__tgPeer.kind='chat'; document.querySelector('.tg-dialog[data-chat=\"77\"]').click()")
        await b.until("!!document.querySelector('[data-act=\"chat-menu\"]')")
        await b.js("document.querySelector('[data-act=\"chat-menu\"]').click()")
        await b.until("!!document.querySelector('.menu-pop [data-m]')")
        items = await b.js("[...document.querySelectorAll('.menu-pop [data-m]')].map(x=>x.dataset.m)")
        assert "block" not in items and "unblock" not in items, items

    asyncio.run(desktop.with_browser("online", "", check, extra_init=FAKE.replace("state:'none'", "state:'ready'") + EXTRA))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_node_that_does_not_answer_is_retried_until_it_does():
    async def check(b):
        await b.js("window.__tgDown = true; true")
        await _open(b)
        # It must NOT sit on a black "CONNECTING…": within the 15 s limit it says what is wrong and that it retries.
        await b.until("/Trying again automatically/.test(document.querySelector('.tg-app').textContent)")
        hits = await b.js("window.__tgDownHits||0")
        await b.js("window.__tgDown = false; true")      # the node is back
        await b.until("document.querySelectorAll('.tg-dialog').length===2")   # ... and the screen recovers alone
        assert hits >= 1

    asyncio.run(desktop.with_browser("online", "", check, extra_init=FAKE.replace("state:'none'", "state:'ready'") + EXTRA))
