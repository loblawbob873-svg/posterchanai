"""The app gate must not say "you are not a member" before anybody has answered the question.

Reported as: "The nip05 message you get when running every fucking app is annoying, and terrible to
the user, surely you can improve this. There has to be a smarter something you can do when you login
to quickly get where you need to be."

`tests/client/test_the_app_gate_can_fix_itself.py` is the sibling of this file and covers the other
half — that a gate which HAS an answer offers the next step (a name), never a profile edit. This one
covers when the gate is allowed to make that speech at all.

The screen had three situations and one body of text. `allowed()` is false for a member whose verdict
has not arrived yet, and a verdict is unknown on every fresh load and after every profile change —
so the app somebody opened was replaced by the reasons they might be refused while the check that
says otherwise was still in flight. Do it in five apps and it is five lectures about a membership
they already hold. "We could not ask" (a 503, a dead relay, a signer that did not answer) painted the
same text, which is this repo's most repeated rule broken on the most sensitive screen it has.

Nothing here is about WHO IS ALLOWED — `allowed()` is untouched and the gate still blocks — only
about what is shown and how often.
"""
from tests.client.test_emoji_pack_tabs_layout import chrome  # noqa: F401  (pytest fixture)
from tests.client.test_instance_access_browser import opened, settle

ESSAY = 'Apply for one — as soon as an admin approves it, every app here opens.'


def text(chrome):
    return chrome.evaluate("document.querySelector('#feed').textContent")


def test_a_check_still_in_flight_is_not_a_refusal(chrome):
    """The window this lives in is every boot: state is null until the server answers."""
    with opened(chrome):
        assert chrome.evaluate("held=true;PCInstanceAccess.gate('notes')") is True, 'the gate stopped blocking'
        assert chrome.evaluate("PCInstanceAccess.allowed('notes')") is False, 'the gate started allowing on a guess'
        shown = text(chrome)
        assert 'Notes' in shown, 'the panel does not even name the app being opened'
        for phrase in (ESSAY, 'Apply for a name', 'Existing app permissions still apply'):
            assert phrase not in shown, f'an unanswered check is presented as a refusal: {phrase!r}'
        chrome.evaluate('replies.shift()()')
        settle(chrome)
        assert chrome.evaluate('renders') == ['notes'], 'the answer arrived and nobody was let in'


def test_could_not_ask_never_reads_as_not_a_member(chrome):
    """A 503, an unreachable relay or a signer that never answered is not a verdict."""
    with opened(chrome):
        chrome.evaluate("__PC.authFetch=async()=>{throw Error('relay unreachable')};PCInstanceAccess.gate('mail')")
        settle(chrome)
        shown = text(chrome)
        assert 'relay unreachable' in chrome.evaluate("document.querySelector('.ia-status').textContent")
        for phrase in (ESSAY, 'Apply for a name'):
            assert phrase not in shown, f'a failed check is presented as a refusal: {phrase!r}'
        assert chrome.evaluate("!!document.querySelector('.ia-retry')"), 'no way to ask again'
        assert chrome.evaluate("PCInstanceAccess.allowed('mail')") is False, 'a failed check let somebody in'


def test_a_refusal_explains_itself_once_not_once_per_app(chrome):
    """The second app does not need the paragraphs. It needs the buttons."""
    with opened(chrome):
        chrome.evaluate("qualified=false;PCInstanceAccess.gate('notes')")
        settle(chrome)
        assert ESSAY in text(chrome), 'the first refusal no longer explains itself'
        chrome.evaluate("PCInstanceAccess.gate('mail')")
        again = text(chrome)
        assert 'Email' in again
        assert ESSAY not in again, 'every app still repeats the whole explanation'
        assert chrome.evaluate("!!document.querySelector('.ia-apply')"), 'the next step went with the prose'
        assert 'example.test' in again, 'which instance the name is on is no longer said'


def test_the_explanation_returns_when_the_situation_changes(chrome):
    with opened(chrome):
        chrome.evaluate("qualified=false;PCInstanceAccess.gate('notes')")
        settle(chrome)
        chrome.evaluate("PCInstanceAccess.gate('mail')")
        chrome.evaluate("account='b'.repeat(64);PCInstanceAccess.gate('mail')")
        settle(chrome)
        assert ESSAY in text(chrome), 'another account is a new situation and must be explained again'


def test_the_profile_never_forces_an_uncached_server_check(chrome):
    """`?refresh=1` makes the server drop its own answer. Membership no longer reads the profile, so
    neither the profile ARRIVING on boot nor an edit to it is a reason to pay for that."""
    with opened(chrome):
        chrome.evaluate("window.urls=[];window.profileReady=false;"
                        "__PC.viewer=()=>({pubkey:account,profile:profileReady?{nip05}:{},profileKnown:profileReady});"
                        "__PC.authFetch=async url=>{urls.push(url);return {ok:true,json:async()=>info()}};")
        chrome.evaluate("PCInstanceAccess.allowed('notes')")
        chrome.evaluate("profileReady=true;PCInstanceAccess.gate('notes')")
        settle(chrome)
        chrome.evaluate("nip05='someone@elsewhere.test';PCInstanceAccess.gate('notes')")
        settle(chrome)
        assert chrome.evaluate('urls') == ['/api/instance-welcome/access'], chrome.evaluate('urls')


def test_a_member_who_is_merely_offline_is_not_told_to_fix_their_profile(chrome):
    """A cached yes cannot open a server-backed app, which is not the same as being refused one."""
    with opened(chrome):
        chrome.evaluate('PCInstanceAccess.refresh()')
        settle(chrome)
        chrome.evaluate("Object.defineProperty(navigator,'onLine',{value:false,configurable:true});PCInstanceAccess.refresh()")
        settle(chrome)
        # Web Search needs the server; Mail reads what this device kept, so a cached yes opens it offline.
        assert chrome.evaluate("PCInstanceAccess.allowed('websearch')") is False
        assert chrome.evaluate("PCInstanceAccess.allowed('mail')") is True
        chrome.evaluate("PCInstanceAccess.gate('websearch')")
        assert ESSAY not in text(chrome), 'a member who is offline is told they are not a member'
        assert 'Offline' in chrome.evaluate("document.querySelector('.ia-status').textContent")
        assert chrome.evaluate("!!document.querySelector('.ia-use')") is False, \
            'it offers to publish the address they already publish'
