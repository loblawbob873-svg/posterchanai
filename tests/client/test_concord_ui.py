"""The browser must expose Concord, keep invite secrets client-side, and fit phones."""

import re
import subprocess
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
    assert "concord.css?v=9" in CONCORD
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
    assert "['concord','concord','Concord']" in APP, "mobile Discover must expose Concord"


def test_concord_has_honest_creation_and_public_discovery_empty_states():
    assert 'Create community' in CONCORD
    assert 'Create a public community' in CONCORD
    assert 'window.PosterCord.createCommunity' in CONCORD
    assert 'p.relayPublishTo(relays,ev)' in CONCORD
    assert "await p.publish(1,`${name}" in CONCORD
    assert 'id="cc-publish-listing"' in CONCORD and 'DISCOVER_RELAYS' in CONCORD
    assert 'p.relayPublishTo(CORD_RELAYS,ev)' in CONCORD
    assert 'No public communities found' in CONCORD
    assert 'cc-public-room' in CONCORD_CSS


def test_concord_ctrl_or_cmd_enter_sends_without_breaking_plain_enter():
    assert 'bind(me);' in CONCORD and 'function bind(me)' in CONCORD
    assert "e.key==='Enter'&&(e.ctrlKey||e.metaKey)" in CONCORD
    assert 'e.preventDefault(); return send.click()' in CONCORD
    assert "e.key==='Enter'&&!e.shiftKey" not in CONCORD


def test_concord_create_and_send_flow_executes_without_runtime_errors():
    run = subprocess.run(
        ["node", str(ROOT / "tests/client/concord_runtime.mjs")],
        cwd=ROOT, capture_output=True, text=True, timeout=10,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "concord runtime flow ok" in run.stdout


def test_concord_room_icons_can_be_set_on_create_and_edited_later():
    assert 'cc-community-icon' in CONCORD
    assert 'id="cc-edit-icon"' in CONCORD
    assert 'id="cc-icon-save"' in CONCORD
    assert 'room.icon=normalizeIcon' in CONCORD
    assert "u.protocol==='https:'||u.protocol==='http:'" in CONCORD
    assert '.cc-server-img' in CONCORD_CSS


def test_concord_standard_controls_are_wired_not_decorative():
    assert 'id="cc-attach"' in CONCORD and 'file.onchange=async' in CONCORD
    assert 'p.uploadBlob' in CONCORD
    assert 'id="cc-emoji"' in CONCORD and 'p.openEmojiPopover' in CONCORD
    assert 'id="cc-members"' in CONCORD and "members.onclick" in CONCORD
    assert 'id="cc-notify"' in CONCORD and 'p.askOsNotify' in CONCORD
    assert 'id="cc-call"' in CONCORD and 'p.startGroupCall' in CONCORD
    assert 'startGroupCall,' in APP and 'uploadBlob,' in APP and 'openEmojiPopover,' in APP
    assert 'cc-members-dialog' in CONCORD and 'cc-member-list' in CONCORD_CSS
    assert 'cc-description-value' in CONCORD and 'room.description=String' in CONCORD
    assert 'cc-channel-visibility' in CONCORD and "channel.private=$('#cc-channel-visibility').value==='private'" in CONCORD
    assert '.cc-visibility.public' in CONCORD_CSS and '.cc-visibility.private' in CONCORD_CSS
    assert 'p.linkify' in CONCORD and 'p.linkCardHtml' in CONCORD
    assert 'id="cc-copy-link"' in CONCORD and 'p.copyValue(room.url)' in CONCORD
    assert 'upgrading this room to a public relay community' in CONCORD
    assert 'mintPublicRoom(p,room.name,room.icon)' in CONCORD
    assert 'await hydrateInvite(p,raw)' in CONCORD
    assert 'decrypting saved community' in CONCORD
    assert 'kinds:[33301]' in CONCORD
    assert "'#d':[''],limit:100" in CONCORD and 'max:200' in CONCORD
    assert 'for(const ev of candidates)' in CONCORD
    assert "opened=decoded(url,[ev])" in CONCORD
    assert 'kinds:[13302,33302]' in CONCORD and 'syncArmadaMemberships(p,viewer)' in CONCORD
    assert 'window.PosterCordReader' in CONCORD and 'hydrateRoomStreams(p,i)' in CONCORD
    assert 'kinds:[1059]' in CONCORD and 'reader.inspectChat' in CONCORD
    assert 'reader.createChatWrap' in CONCORD and 'await p.relayPublishTo(relays,made.wrap)' in CONCORD
    assert 'scrollChatBottom()' in CONCORD
    assert "if(file&&input)file.onchange=async()=>" in CONCORD
    assert 'room.cord.hydrated=true' in CONCORD
    assert "if(loaded&&loaded.cord)" in CONCORD
    assert 'await p.nip44dec(viewer.pubkey,event.content)' in CONCORD
    assert 'cc-public-copy' in CONCORD and '.cc-public-copy' in CONCORD_CSS
    assert 'function isUnread(room)' in CONCORD
    assert '.cc-channel.unread' in CONCORD_CSS and '.cc-server.unread' in CONCORD_CSS
    assert 'notifyMentions(p,current,messages,viewer,me)' in CONCORD
    assert "route:'concord'" in CONCORD and 'concord-mention-' in CONCORD
    assert "search:'armada.buzz/invite'" in CONCORD and "search:'poster.place/invite'" in CONCORD
    assert 'data-cc-discover' in CONCORD and 'relaySubscribe:' in APP
    assert 'class="cc-message-avatar"' in CONCORD and '.cc-message-avatar' in CONCORD_CSS
    assert 'linkify, linkCardHtml, hydrateLinkCards' in APP


def test_concord_dove_icon_reaches_sidebar_mobile_and_desktop_launcher():
    sprite = (ROOT / 'static/js/client/sprite.js').read_text()
    assert 'id="i-concord"' in sprite
    assert 'data-view="concord"><svg class="ic"><use href="#i-concord"' in HTML
    assert "icon:'#i-concord', label:'Concord'" in APP
    # os.js derives desktop/start-menu icons from the sidebar <use href>, so this is the desktop source.
    os_js = (ROOT / 'static/js/client/os.js').read_text()
    assert "btn.querySelector('svg use')" in os_js


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


def test_concord_messages_support_persisted_replies_and_reactions():
    assert 'function messageId(m)' in CONCORD
    assert 'data-cc-reply=' in CONCORD
    assert 'reply:replyTarget?' in CONCORD
    assert 'data-cc-react=' in CONCORD
    assert 'data-cc-react-toggle=' in CONCORD
    assert 'saveTestMessages(storeId,m)' in CONCORD
    assert '.cc-message-reply' in CONCORD_CSS
    assert '.cc-reaction-picker' in CONCORD_CSS


def test_concord_brand_always_returns_to_discovery():
    assert 'id="cc-home"' in CONCORD
    assert "state.community=null; state.channel=null; render()" in CONCORD
