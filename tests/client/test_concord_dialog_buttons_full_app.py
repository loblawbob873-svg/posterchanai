"""Communities' dialog buttons do what they say, pressed in the SHIPPED bundle in a fixture room:

  * Theme → Done closes the theme picker;
  * Create a community → Cancel closes the create dialog;
  * on a phone, Members → Invite swaps the member list for the join/invite sheet;
  * on a phone, ‹ from a room's channel list goes back to the communities list;
  * ⋯ → Invitation links opens the links manager with its "new link" button.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_call_button_full_app import ROOM, EXTRA


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def room(b, width):
    await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=850, deviceScaleFactor=1, mobile=width < 600))
    await desktop.login(b)
    await b.js(ROOM + "('cord','ok')")
    await b.until("!!document.querySelector('.cc-channel-list')")


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_theme_create_and_invitation_links_on_desktop():
    async def check(b):
        await room(b, 1280)
        await b.until("!!document.querySelector('#cc-theme')")
        await b.js("document.querySelector('#cc-theme').click(); 1")
        await b.until("!!document.querySelector('#cc-theme-done')")
        await b.js("document.querySelector('#cc-theme-done').click(); 1")
        await b.until("!document.querySelector('#cc-theme-done')")

        await b.js("document.querySelector('#cc-create, #cc-join-create').click(); 1")
        await b.until("!document.querySelector('#cc-create-dialog').classList.contains('hidden')")
        await b.js("document.querySelector('#cc-create-cancel').click(); 1")
        await b.until("document.querySelector('#cc-create-dialog').classList.contains('hidden')")

        await b.js("document.getElementById('cc-head-more').click(); 1")
        await b.until("!!document.querySelector('.menu-pop [data-m=\"cc-manage-links\"]')")
        await b.js("document.querySelector('.menu-pop [data-m=\"cc-manage-links\"]').click(); 1")
        await b.until("!!document.querySelector('#cc-mint-link') && !!document.querySelector('#cc-owned-links')")
        assert not await b.js("__errors"), await b.js("__errors")

    asyncio.run(desktop.with_browser("online", "", check, EXTRA))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_members_invite_and_back_to_communities_on_a_phone():
    async def check(b):
        await room(b, 390)
        await b.until("!!document.querySelector('#cc-members')")
        await b.js("document.querySelector('#cc-members').click(); 1")
        await b.until("!document.querySelector('#cc-members-dialog').classList.contains('hidden')")
        await b.js("document.querySelector('#cc-members-invite').click(); 1")
        await b.until("document.querySelector('#cc-members-dialog').classList.contains('hidden') && !document.querySelector('#cc-join').classList.contains('hidden')")
        await b.js("document.querySelector('#cc-join').classList.add('hidden'); 1")

        await b.until("!!document.querySelector('#cc-back-communities')")
        assert not await b.js("!!document.querySelector('.cc-discover')")
        await b.js("document.querySelector('#cc-back-communities').click(); 1")
        await b.until("!!document.querySelector('.cc-discover')")
        assert not await b.js("__errors"), await b.js("__errors")

    asyncio.run(desktop.with_browser("online", "", check, EXTRA))
