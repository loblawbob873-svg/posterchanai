"""The browser must expose Concord, keep invite secrets client-side, and fit phones."""

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()
CONCORD_CSS = (ROOT / "static/css/concord.css").read_text()
CONCORD = (ROOT / "static/js/client/concord.js").read_text()
CORD_READER = (ROOT / "static/js/client/cord-reader.js").read_text()
HTML = (ROOT / "templates/client.html").read_text()
PUSH = (ROOT / "app/services/nostr_push_service.py").read_text()


def test_concord_is_a_separate_discover_view_not_public_chat():
    assert 'data-view="concord"' in HTML
    discover = HTML.split('id="disc-sub"', 1)[1].split('</div>', 1)[0]
    assert 'data-view="concord"' in discover
    assert 'static/js/client/concord.js' in HTML
    assert "renderModuleView('concord','concord.js','PCConcord','render')" in APP
    assert 'async function renderChatrooms()' not in APP
    assert 'data-view="chat"' not in HTML, "legacy Nostr Chat navigation must stay removed"


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
    assert "concord.css?v=13" in CONCORD
    assert '.cc-compose textarea:focus' in CONCORD_CSS
    assert 'box-shadow:none!important' in CONCORD_CSS
    assert "'/static/css/concord.css'" in (ROOT / "static/js/client/sw.js").read_text()
    assert ".cc-app" in CONCORD_CSS and "grid-template-columns:68px 248px" in CONCORD_CSS
    assert CONCORD_CSS.count('{') == CONCORD_CSS.count('}'), "Concord CSS has an unclosed rule"


def test_concord_fills_workspace_and_identifies_the_signed_in_user():
    assert "classList.toggle('concord-view', v==='concord')" in APP
    assert 'body.concord-view .rightbar,body.concord-view .rb-toggle,body.concord-view #rb-toggle{display:none!important' in CONCORD_CSS
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


def test_created_and_joined_communities_survive_browser_storage_loss():
    assert 'async function persistArmadaMembership(p,room)' in CONCORD
    assert 'await p.nip44enc(viewer.pubkey,JSON.stringify(list))' in CONCORD
    assert 'await p.publish(13302,content,[])' in CONCORD
    assert 'await persistArmadaMembership(p,room)' in CONCORD
    assert 'recoverOwnedInvite(p,item)' in CONCORD
    assert "item.source.pubkey!==viewer.pubkey" in CONCORD


def test_chat_scroll_is_keyed_by_room_and_survives_profile_link_navigation():
    assert "sessionStorage.getItem('pc.concord.scroll.'+key)" in CONCORD
    assert "room&&(room.communityId||room.naddr||room.url)" in CONCORD
    assert "scroller.querySelectorAll('a')" in CONCORD
    assert "!document.body.classList.contains('concord-view')" in CONCORD
    assert "['ai-msgs','dm-msgs']" in APP
    assert "inner['cc-messages']" in APP
    assert "pos&&pos.bottom?el.scrollHeight" in APP
    os_js = (ROOT / 'static/js/client/os.js').read_text()
    assert "w.innerChatScroll={}" in os_js
    assert "['cc-messages','.cc-messages']" in os_js
    assert "el.scrollTop=pos.bottom?el.scrollHeight" in os_js
    assert "el.dataset.osParking='1'" in os_js
    assert 'scroller.dataset.osParking' in CONCORD


def test_concord_ctrl_or_cmd_enter_sends_without_breaking_plain_enter():
    assert 'bind(me);' in CONCORD and 'function bind(me)' in CONCORD
    assert "e.key==='Enter'||e.code==='Enter'" in CONCORD
    assert 'e.preventDefault(); return send.onclick()' in CONCORD
    assert "e.key==='Enter'&&!e.shiftKey" not in CONCORD


def test_authors_can_delete_their_own_messages_after_relay_acceptance():
    assert 'data-cc-delete' in CONCORD
    assert "found.pubkey!==viewer.pubkey" in CONCORD
    assert "[['e',id],['k',String(found.kind||9)]],5" in CONCORD
    assert "messages.filter(m=>messageId(m)!==id)" in CONCORD


def test_owner_can_publish_an_interoperable_cord_ban():
    assert 'data-cc-ban' in CONCORD
    assert 'reader.createBanWrap' in CONCORD
    assert 'community relays rejected the ban' in CONCORD
    assert 'room.banned=made.banned' in CONCORD


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


def test_public_community_cards_resolve_cord_icons():
    assert 'hydrateDiscoveredIcon(p,item)' in CONCORD
    assert 'publicRoomIcon(p,r)' in CONCORD
    assert '.cc-public-icon img' in CONCORD_CSS
    assert "u.protocol==='https:'||u.protocol==='http:'" in CONCORD
    assert '.cc-server-img' in CONCORD_CSS


def test_concord_standard_controls_are_wired_not_decorative():
    assert 'id="cc-attach"' in CONCORD and 'file.onchange=async' in CONCORD
    assert 'p.uploadBlob' in CONCORD
    assert 'p.blossomPicker(null,insertBlossomAttachment' in CONCORD
    assert '🌸 Blossom folders' in CONCORD
    assert 'pendingAttachments.set(url,tag)' in CONCORD
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
    assert 'refreshing room channels and history' not in CONCORD
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
    assert 'let loaded=room; state.community=i' in CONCORD
    assert 'await p.nip44dec(viewer.pubkey,event.content)' in CONCORD
    assert 'cc-public-copy' in CONCORD and '.cc-public-copy' in CONCORD_CSS
    assert 'function isUnread(room)' in CONCORD
    assert '.cc-channel.unread' in CONCORD_CSS and '.cc-server.unread' in CONCORD_CSS
    assert 'notifyMentions(p,current,messages,viewer,me)' in CONCORD
    assert "route:'concord'" in CONCORD and 'concord-mention-' in CONCORD
    assert 'import_meta.env' not in CORD_READER
    assert 'CapacitorException' not in CORD_READER and 'registerPlugin' not in CORD_READER
    assert 'decryptImagePointer(icon)' in CONCORD and "crypto.subtle.decrypt({name:'AES-GCM'" in CONCORD
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
    assert "if(view==='concord') snapTo(w,'max')" in os_js
    assert '.osw-slot:has(>.cc-app)' in CONCORD_CSS
    assert '.osw-body>#feed.feed-dm:has(.cc-app)' in CONCORD_CSS


def test_invite_parser_requires_naddr_and_secret_fragment():
    assert "/\\/invite\\/(naddr1" in CONCORD
    assert "m&&u.hash.length>3" in CONCORD
    assert "secret:u.hash.slice(1)" in CONCORD
    invite_loader = CONCORD[CONCORD.index("async function hydrateInvite"):CONCORD.index("function inviteRefUrl")]
    assert "fetch(" not in invite_loader, "invite fragments must never be posted to PosterChan"


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
    assert 'body.concord-view .cc-conversation{width:100vw' in CONCORD_CSS
    assert 'body.concord-view .cc-message-actions button::after{content:none' in CONCORD_CSS
    assert "document.getElementById('rb-toggle')" in APP


def test_notification_copy_does_not_claim_server_can_read_room_messages():
    assert "Notification settings" in CONCORD
    assert "1059" in PUSH, "direct Concord invites must still use the giftwrap push path"


def test_concord_messages_support_persisted_replies_and_reactions():
    assert 'function messageId(m)' in CONCORD
    assert 'data-cc-reply=' in CONCORD
    assert 'wireKind=target?1111:9' in CONCORD
    assert "['E',messageId(target)" in CONCORD
    assert 'data-cc-react=' in CONCORD
    assert 'data-cc-react-toggle=' in CONCORD
    assert 'saveTestMessages(storeId,m)' in CONCORD
    assert '.cc-message-reply' in CONCORD_CSS
    assert '.cc-reaction-picker' in CONCORD_CSS
    assert "publishCordMessage(p,room,state.channel,emoji" in CONCORD
    assert 'data-cc-quick-react=' in CONCORD
    assert 'cc-reaction${mine?' in CONCORD and 'aria-pressed="${mine}"' in CONCORD
    assert '.cc-reaction.mine' in CONCORD_CSS
    assert '.cc-action-sep' in CONCORD_CSS


def test_room_history_reads_pool_and_external_relays_without_erasing_cached_messages():
    assert 'async function cordQuery(' in CONCORD
    assert 'if(p.relayQuery)jobs.push' in CONCORD
    assert 'if(p.relayQueryFrom)jobs.push' in CONCORD
    assert 'const storeId=channelStoreId(room,channel.name),prior=testMessages(storeId)' in CONCORD
    assert 'for(const m of msgs)merged.set' in CONCORD
    assert 'since,limit:500' in CONCORD


def test_concord_webxdc_mentions_live_sync_and_scroll_are_integrated():
    assert "application/x-webxdc" in CONCORD and "pendingAttachments.set(url,['imeta'" in CONCORD
    assert 'hydrateWebxdcCards(current)' in CONCORD and 'PCWebxdc.cardHtml(app)' in CONCORD
    assert 'reactionIds:' in CORD_READER and 'extraTags' in CORD_READER
    assert 'mentionToken' in CONCORD and "e.key==='Tab'" in CONCORD
    assert 'mentionBox' not in CONCORD
    assert 'refreshActiveChannel(p)' in CONCORD and 'setInterval(()=>refreshActiveChannel(p),4000)' in CONCORD
    assert 'scrollStates' in CONCORD and 'st.pinned' in CONCORD and 'preserveChatScroll' in CONCORD


def test_concord_brand_always_returns_to_discovery():
    assert 'id="cc-home"' in CONCORD
    assert "state.community=null; state.channel=null; render()" in CONCORD
