"""The browser must expose Concord, keep invite secrets client-side, and fit phones."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()
CONCORD_CSS = (ROOT / "static/css/concord.css").read_text()
CONCORD = (ROOT / "static/js/client/concord.js").read_text()
HTML = (ROOT / "templates/client.html").read_text()
PUSH = (ROOT / "app/services/nostr_push_service.py").read_text()


def test_concord_is_a_separate_discover_view_not_public_chat():
    assert 'data-view="concord"' in HTML
    discover = HTML.split('id="disc-sub"', 1)[1].split('</div>', 1)[0]
    assert 'data-view="concord"' in discover
    assert 'static/js/client/concord.js' in HTML
    assert "renderModuleView('concord','concord.js','PCConcord','render')" in APP
    chat = APP.split('async function renderChatrooms()', 1)[1].split('function channelCard', 1)[0]
    assert 'concord' not in chat.lower(), "Concord belongs in Discover, not the public Chat screen"


def test_stale_pwa_controller_cannot_restore_the_previous_screen_over_concord():
    assert "previous service worker" in CONCORD
    assert "[data-view=\"concord\"]" in CONCORD
    assert "setTimeout" in CONCORD
    assert "feed.classList.add('feed-dm'); render()" in CONCORD


def test_concord_is_precached_and_old_bundles_self_heal_the_nav():
    sw = (ROOT / "static/js/client/sw.js").read_text()
    assert "'/static/js/client/concord.js'" in sw
    assert "{ view:'concord', into:'#disc-sub'" in APP


def test_concord_owns_a_versioned_stylesheet_so_stale_shell_css_cannot_unstyle_it():
    assert 'static/css/concord.css' in HTML
    assert "data-concord-css" in CONCORD
    assert "concord.css?v=4" in CONCORD
    assert "'/static/css/concord.css'" in (ROOT / "static/js/client/sw.js").read_text()
    assert ".cc-app" in CONCORD_CSS and "grid-template-columns:68px 248px" in CONCORD_CSS
    assert CONCORD_CSS.count('{') == CONCORD_CSS.count('}'), "Concord CSS has an unclosed rule"


def test_concord_fills_workspace_and_identifies_the_signed_in_user():
    assert "classList.toggle('concord-view', v==='concord')" in APP
    assert 'body.concord-view .rightbar,body.concord-view .rb-toggle{display:none!important}' in CONCORD_CSS
    assert 'max-width:none!important' in CONCORD_CSS
    assert 'viewer: () =>' in APP
    assert 'Your Nostr identity' not in CONCORD
    assert '<small>You</small>' in CONCORD


def test_concord_has_discord_style_panes_and_dm_style_composer():
    for surface in ('cc-communities', 'cc-channels', 'cc-conversation', 'cc-messages', 'cc-compose'):
        assert surface in CONCORD
    assert 'Message #${state.channel' in CONCORD
    assert "['concord','users','Concord']" in APP, "mobile Discover must expose Concord"


def test_concord_has_honest_creation_and_public_discovery_empty_states():
    assert 'Create community' in CONCORD
    assert 'Create a test community' in CONCORD
    assert 'local:true' in CONCORD
    assert 'No public invites found' in CONCORD
    assert 'not published to relays' in CONCORD
    assert 'cc-public-room' in CONCORD_CSS


def test_concord_ctrl_or_cmd_enter_sends_without_breaking_plain_enter():
    assert 'bind(me);' in CONCORD and 'function bind(me)' in CONCORD
    assert "e.key==='Enter'&&(e.ctrlKey||e.metaKey)" in CONCORD
    assert 'e.preventDefault(); send.click()' in CONCORD
    assert "e.key==='Enter'&&!e.shiftKey" not in CONCORD


def test_invite_parser_requires_naddr_and_secret_fragment():
    assert "/\\/invite\\/(naddr1" in CONCORD
    assert "m&&u.hash.length>3" in CONCORD
    assert "secret:u.hash.slice(1)" in CONCORD
    assert "fetch(" not in CONCORD, "invite fragments must never be posted to PosterChan"


def test_concord_controls_are_phone_sized_and_single_column():
    phone = re.search(r"@media\(max-width:600px\)\{(.*?)\n\}", CSS, re.S)
    assert phone, "missing phone breakpoint"
    rules = phone.group(1)
    assert ".concord-hub" in rules
    assert "flex-wrap:wrap" in rules
    assert ".concord-hub .btn{width:100%;min-height:44px}" in rules
    assert ".concord-actions .btn{flex:1;min-height:46px}" in rules
    phone_all = CSS.split('@media(max-width:820px){', 1)[1]
    assert '.cc-app.show-chat .cc-conversation{display:flex}' in phone_all
    assert '.cc-channel{min-height:46px' in phone_all
    assert '.cc-head-btn,.cc-compose-btn,.cc-mobile-back{min-width:44px' in phone_all


def test_notification_copy_does_not_claim_server_can_read_room_messages():
    assert "Notification settings" in CONCORD
    assert "1059" in PUSH, "direct Concord invites must still use the giftwrap push path"
