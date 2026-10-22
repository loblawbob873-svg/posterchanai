"""Concord's room bar is Armada's: search, call and a menu -- and search finds messages and takes you to them.

2026-10-21: "armada UI is simpler and cleaner, their Room top bar has a hamburger menu with the options instead of
cluttered like us. Just keep phone next to the hamburger menu and add a search ... search icon next to phone", and
"In known members, separate Admins and Members like Armada". Real client, a minted fixture community.
"""
import asyncio
import json
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.concord_fixture_relay import INSTALL, MINT, OPEN_ROOM
from tests.client.test_concord_forum_full_app import wait_for, type_into

NO_OS = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [360, 1280])
def test_search_call_and_menu(width):
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=844, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("__PC.switchMessagesTab('concord')")
        await b.until('!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord')
        await b.js(INSTALL)
        minted = await b.js(MINT.replace('NAME', json.dumps('TV shows')))
        await b.js(OPEN_ROOM + '(' + json.dumps(minted['room']) + ')')
        await b.until("!!document.querySelector('.cc-channel[data-cc-channel=\"general\"]')")
        await b.js("document.querySelector('.cc-channel[data-cc-channel=\"general\"]').click()")
        for t in ['Who watched the finale?', 'Something else entirely', 'The finale was great']:
            await type_into(b, '#cc-input', t)
            await b.js("document.querySelector('#cc-send').click()")
            await asyncio.sleep(.5)
        await wait_for(b, "document.querySelectorAll('.cc-message').length===3", 30)
        got['bar'] = await b.js("[...document.querySelectorAll('.cc-conversation>header button')].filter(e=>e.getBoundingClientRect().width>0&&e.getBoundingClientRect().right<=innerWidth+1).map(e=>e.id)")
        # The menu holds the rest.
        await b.js("document.querySelector('#cc-head-more').click()")
        await wait_for(b, "!!document.querySelector('.menu-pop [data-m]')")
        got['menu'] = await b.js("[...document.querySelectorAll('.menu-pop [data-m]')].map(x=>x.dataset.m)")
        if width < 600:   # a phone opens Known members from the menu; a desktop shows it as a side pane already
            await b.js("document.querySelector('.menu-pop [data-m=\"cc-members\"]').click()")
        else:
            await b.js("document.body.click()")
        await asyncio.sleep(.4)
        got['groups'] = await b.js("[...document.querySelectorAll('.cc-member-group')].filter(e=>e.getBoundingClientRect().height>0).map(e=>e.textContent)")
        got['owner_label'] = await b.js("[...document.querySelectorAll('.cc-member small')].filter(e=>e.getBoundingClientRect().height>0).map(e=>e.textContent)")
        await b.js("document.querySelectorAll('#cc-members-dialog').forEach(d=>d.classList.add('hidden'))")
        # Search: matches across the community, newest first, and a result takes you to its message.
        await b.js("document.querySelector('#cc-search').click()")
        await wait_for(b, "!!document.querySelector('#cc-search-q')")
        await type_into(b, '#cc-search-q', 'FINALE')
        await wait_for(b, "document.querySelectorAll('.cc-search-hit').length===2")
        got['hits'] = await b.js("[...document.querySelectorAll('.cc-search-hit .cc-search-text')].map(x=>x.textContent)")
        got['marked'] = await b.js("[...document.querySelectorAll('.cc-search-hit mark')].map(x=>x.textContent)")
        await b.js("document.querySelectorAll('.cc-search-hit')[1].click()")
        await wait_for(b, "!!document.querySelector('.cc-message-target')")
        got['target'] = await b.js("document.querySelector('.cc-message-target').textContent")
        got['fits'] = await b.js("document.documentElement.scrollWidth<=innerWidth+1")
    asyncio.run(desktop.with_browser('online', '', check, NO_OS))
    assert got['bar'][-3:] == ['cc-search', 'cc-call', 'cc-head-more'], got['bar']
    assert not {'cc-members', 'cc-pins', 'cc-direct-send', 'cc-copy-link', 'cc-notify-head'} & set(got['bar']), got['bar']
    assert got['menu'][0] == 'cc-members' and 'cc-leave-shortcut' in got['menu'] and 'cc-copy-link' in got['menu'], got['menu']
    assert any(g.startswith('Admins') for g in got['groups']) and 'Owner' in got['owner_label'], got
    assert got['hits'] == ['The finale was great', 'Who watched the finale?'], got['hits']
    assert got['marked'] == ['finale', 'finale']
    assert 'Who watched the finale?' in got['target']
    assert got['fits']
