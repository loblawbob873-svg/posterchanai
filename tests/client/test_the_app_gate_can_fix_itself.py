"""The app gate offers the next real step -- a name here -- and NEVER asks you to change your profile.

Two reports, one screen. First: "The nip05 message you get when running every fucking app is annoying"
-- the gate told people to open Edit profile, replace their NIP-05 with this instance's address and
Save. Then a one-click "Use <address>" did that for them. Which is exactly what nobody with an identity
of their own wants: "Why can't users add their non-instance nip05" / "the entire point was to display
both". Membership is the name this instance granted (the server never reads the profile), so there is
nothing in a profile to fix, and the gate must not offer to overwrite one.

Driven in a real browser against the shipped instance-access.js: what a refused person SEES, and that
nothing on the screen publishes a kind-0.
"""
from tests.client.test_emoji_pack_tabs_layout import chrome  # noqa: F401  (pytest fixture)
from tests.client.test_instance_access_browser import opened, settle


def test_a_refused_account_is_offered_a_name_and_keeps_its_own_nip05(chrome):
    with opened(chrome):
        chrome.evaluate("window.published=[];__PC.publish=async(...a)=>{published.push(a);return {ok:true}};"
                        "nip05='bob@nostrplebs.com';qualified=false;PCInstanceAccess.gate('mail')")
        settle(chrome)
        shown = chrome.evaluate("document.querySelector('#feed').textContent")
        assert 'example.test' in shown, 'it does not say which instance the name is on'
        assert 'Keep whatever NIP-05 your profile shows' in shown, shown
        for phrase in ('Edit profile', 'Replace', 'replace', 'Use alice@', 'set your NIP-05'):
            assert phrase not in shown, f'the gate still asks for a profile change: {phrase!r}'
        assert chrome.evaluate("[...document.querySelectorAll('.instance-app-gate button')].map(b=>b.className)") == \
            ['btn btn-neon ia-apply', 'btn btn-ghost ia-retry']
        assert chrome.evaluate('published') == [], 'the gate published a profile'


def test_applying_when_a_name_was_already_granted_just_lets_you_in(chrome):
    """The server answers `already` when a name exists -- which IS access now, not a profile chore."""
    with opened(chrome):
        chrome.evaluate("qualified=false;PCInstanceWelcome={apply:async()=>{qualified=true;return {ok:true,already:true,address:'alice@example.test'}}};"
                        "PCInstanceAccess.gate('mail')")
        settle(chrome)
        chrome.evaluate("document.querySelector('.ia-apply').click()")
        settle(chrome)
        assert chrome.evaluate('renders') == ['mail'], 'a granted name did not open the app'


def test_a_blocked_account_is_told_so_and_not_offered_an_application(chrome):
    with opened(chrome):
        chrome.evaluate("__PC.authFetch=async()=>({ok:true,json:async()=>({...info(),qualified:false,reason:'blocked'})});"
                        "PCInstanceAccess.gate('mail')")
        settle(chrome)
        shown = chrome.evaluate("document.querySelector('#feed').textContent")
        assert 'blocked' in shown and not chrome.evaluate("!!document.querySelector('.ia-apply')"), shown
