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


def test_concord_cannot_be_hidden_out_of_classic_and_desktop_launchers():
    locked = APP.split("const NAV_LOCKED = new Set(", 1)[1].split(");", 1)[0]
    assert "'concord'" in locked


def test_packaged_shell_loads_the_complete_concord_surface():
    for asset in ("concord.css", "cord-reader.js", "concord.js"):
        assert asset in HTML
    assert 'data-view="concord"' in HTML


def test_concord_never_repaints_another_app_shared_feed():
    render = CONCORD[CONCORD.index('function render(){'):CONCORD.index('const returning=', CONCORD.index('function render(){'))]
    assert "p.isView('concord')" in render
    assert render.index("p.isView('concord')") < render.index("p.$('#feed')")
    assert "document.addEventListener('click'" not in CONCORD, (
        "an out-of-band click handler can repaint Concord after Code owns the feed")
    assert "isView: view => VIEW === view" in APP


def test_concord_is_precached_and_old_bundles_self_heal_the_nav():
    sw = (ROOT / "static/js/client/sw.js").read_text()
    assert "'/static/js/client/concord.js'" in sw
    assert "{ view:'concord', into:'#disc-sub'" in APP


def test_classic_phone_concord_is_a_full_screen_drilldown_not_squeezed_desktop_columns():
    assert "mobileChatOpen=false" in CONCORD
    assert "mobileChatOpen||state.community==null?' show-chat'" in CONCORD
    assert "mobileChatOpen=true; render(); scrollChatBottom()" in CONCORD
    assert "mobileChatOpen=false; render()" in CONCORD
    assert 'body.concord-view .cc-app{position:fixed!important' in CONCORD_CSS
    assert 'flex-direction:row!important' in CONCORD_CSS
    assert 'body.concord-view .cc-app.show-chat{inset:0!important' in CONCORD_CSS
    assert 'body.concord-view .cc-app.home-view{inset:calc(58px + env(safe-area-inset-top)) 0 calc(61px + env(safe-area-inset-bottom)) 0!important' in CONCORD_CSS
    assert 'body.concord-view .cc-app.home-view{inset:0!important' not in CONCORD_CSS
    assert 'body.concord-view .cc-app.home-view #cc-back-channels{display:grid!important}' in CONCORD_CSS
    assert 'body.concord-view .cc-message-actions .cc-quick-react' in CONCORD_CSS


def test_tablet_concord_consumes_the_full_shell_width():
    tablet = CONCORD_CSS.split('@media(min-width:821px) and (max-width:1180px)', 1)[1].split('}', 5)
    tablet = '}'.join(tablet)
    assert 'width:100vw!important' in tablet
    assert 'body.concord-view .main,body.concord-view .feed.feed-dm' in tablet
    assert 'body.concord-view .cc-app{width:100%!important;max-width:none!important' in tablet
    assert 'grid-template-columns:62px 220px minmax(0,1fr)!important' in tablet


def test_web_concord_removes_the_timeline_shell_gutter_and_width_cap():
    assert 'body.concord-view .app{grid-template-columns:300px minmax(0,1fr)!important;width:100%!important;max-width:none!important;margin:0!important;padding:0!important;column-gap:0!important}' in CONCORD_CSS
    assert 'body.concord-view .main,body.concord-view .feed.feed-dm{width:100%!important;max-width:none!important' in CONCORD_CSS


def test_concord_owns_a_versioned_stylesheet_so_stale_shell_css_cannot_unstyle_it():
    assert 'static/css/concord.css' in HTML
    assert "data-concord-css" in CONCORD
    assert "concord.css?v=15" in CONCORD
    assert '.cc-compose textarea:focus' in CONCORD_CSS
    assert 'box-shadow:none!important' in CONCORD_CSS
    assert "'/static/css/concord.css'" in (ROOT / "static/js/client/sw.js").read_text()
    assert ".cc-app" in CONCORD_CSS and "grid-template-columns:68px 248px" in CONCORD_CSS
    assert CONCORD_CSS.count('{') == CONCORD_CSS.count('}'), "Concord CSS has an unclosed rule"


def test_native_bundles_package_the_concord_stylesheet():
    """Native shells load local /static assets, so a server copy cannot hide this omission."""
    for bundle in ("mobile/build-www.sh", "desktop/build-www.sh"):
        build = (ROOT / bundle).read_text()
        assert 'static/css/concord.css' in build, bundle


def test_desktop_release_audits_the_built_payload_not_only_source():
    workflow = (ROOT / ".github/workflows/desktop.yml").read_text()
    gate = workflow[workflow.index("name: Audit bundled Concord surface"):]
    for asset in ("concord.css", "concord.js", "cord-reader.js", "cord-protocol.js"):
        assert f"www/static/" in gate and asset in gate
    assert 'grep -q \'data-view="concord"\' www/index.html' in gate


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


def test_channels_can_be_starred_without_merging_concord_into_direct_messages():
    assert 'channelStarKey(room,name)' in CONCORD
    assert 'data-cc-star=' in CONCORD
    assert 'aria-pressed="${channelStarred(current,c.name)}"' in CONCORD
    assert 'orderedChannels(current)' in CONCORD
    assert '.cc-channel-star[aria-pressed="true"]' in CONCORD_CSS
    assert "VIEW==='messages' || VIEW==='concord'" in APP
    assert 'data-view="chat"' not in HTML


def test_thread_replies_tag_every_participant_once_but_never_the_sender():
    assert 'function threadParticipants(messages,target,viewerPubkey)' in CONCORD
    assert 'node.pubkey!==viewerPubkey' in CONCORD
    assert 'seen.has(messageId(node))' in CONCORD
    assert "replyTags.push(['P',pk],['p',pk])" in CONCORD
    assert "filter(t=>['K','E'].includes(t[0]))" in CONCORD


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


def test_mobile_reopens_the_last_server_then_drills_into_a_channel_like_discord():
    assert "localStorage.getItem('pc.concord.active')" in CONCORD
    assert "localStorage.setItem('pc.concord.active',String(i))" in CONCORD
    assert 'mobileChatOpen=false, discoveryOpen=false' in CONCORD
    assert "discoveryOpen=true; state.community=null" in CONCORD
    assert "state.channel=b.dataset.ccChannel; mobileChatOpen=true" in CONCORD
    assert "mobileChatOpen=false; render()" in CONCORD
    assert 'id="cc-home" title="Your rooms"' in CONCORD
    assert 'id="cc-discovery" title="Discover public communities"' in CONCORD
    assert "discoveryOpen=!rooms.length" in CONCORD
    assert "return channels.length?channels:[{name:'general',private:false}]" in CONCORD
    assert 'visibleChannels.map(c=>' in CONCORD
    assert 'room.channels=hydratedChannels' in CONCORD
    assert "if(room&&room.cord&&!room.cord.hydrated)" in CONCORD
    assert "await hydrateRoomStreams(p,state.community)" in CONCORD
    assert "if(state.community==null){ const rooms=saved(),wanted=Number(localStorage.getItem('pc.concord.active')" in CONCORD
    assert "state.community==null?'Back to rooms':'Channels'" in CONCORD


def test_created_and_joined_communities_survive_browser_storage_loss():
    assert 'async function persistArmadaMembership(p,room)' in CONCORD
    assert 'await p.nip44enc(viewer.pubkey,JSON.stringify(list))' in CONCORD
    assert 'await p.publish(13302,content,[])' in CONCORD
    assert 'await persistArmadaMembership(p,room)' in CONCORD
    assert 'recoverOwnedInvite(p,item)' in CONCORD
    assert "item.source.pubkey!==viewer.pubkey" in CONCORD


def test_send_is_optimistic_and_does_not_wait_for_relays_to_paint():
    handler = CONCORD.split("send.onclick=async()=>", 1)[1].split("input.onkeydown", 1)[0]
    assert "pending:!room.local" in handler
    assert handler.index("saveTestMessages(storeId,m)") < handler.index("await publishCordMessage")
    assert "failed.pending=false;failed.failed=true" in handler


def test_desktop_recovery_merges_armada_list_shards_and_retries_early_empty_queries():
    assert "function membershipEvents(p,pubkey)" in CONCORD
    assert "{kinds:[13302],authors:[pubkey],limit:1}" in CONCORD
    assert "{kinds:[33302],authors:[pubkey],'#d':[''],limit:20}" in CONCORD
    assert "kinds:[13302,33302]" not in CONCORD
    assert "for(const event of candidates)" in CONCORD
    assert "const entries=new Map(),tombs=new Map()" in CONCORD
    assert "Math.max(Number(tombs.get(t.community_id))" in CONCORD
    assert "membershipRetryTimer=setTimeout" in CONCORD
    assert "recovered?60000:5000" in CONCORD
    assert "window.PosterCordReader.inspectControl(m,[])" in CONCORD


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
    assert "target.value=normalizeIcon($('#cc-icon-value').value)" in CONCORD
    assert 'return saveButton.click()' in CONCORD
    assert 'reader.createMetadataWrap' in CONCORD
    assert 'community relays rejected the profile update' in CONCORD
    assert 'createMetadataWrap: () => createMetadataWrap' in CORD_READER
    assert '[TAG_SUBKIND, VSK_METADATA]' in CORD_READER


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
    assert 'cc-description-value' in CONCORD and 'room.description=description' in CONCORD
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
    assert 'kinds:[13302]' in CONCORD and 'kinds:[33302]' in CONCORD and 'syncArmadaMemberships(p,viewer)' in CONCORD
    assert 'window.PosterCordReader' in CONCORD and 'hydrateRoomStreams(p,i)' in CONCORD
    assert 'kinds:[1059]' in CONCORD and 'reader.inspectChat' in CONCORD
    assert 'reader.createChatWrap' in CONCORD and 'await p.relayPublishTo(relays,made.wrap)' in CONCORD
    assert 'scrollChatBottom()' in CONCORD
    assert "if(file&&input)file.onchange=async()=>" in CONCORD
    assert 'room.cord.hydrated=true' in CONCORD
    assert "if(loaded&&loaded.cord)" in CONCORD
    assert 'let loaded=room;' in CONCORD and 'state.community=i' in CONCORD
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
    assert 'body.concord-view .cc-messages{width:100%!important;max-width:100%!important' in CONCORD_CSS
    assert 'overflow-x:hidden!important;overscroll-behavior:contain!important' in CONCORD_CSS
    assert 'html:has(body.concord-view),body.concord-view' in CONCORD_CSS
    assert 'overflow-x:clip!important' in CONCORD_CSS
    assert 'overscroll-behavior-x:none!important;touch-action:pan-y!important' in CONCORD_CSS
    assert 'body.concord-view .cc-message-body a' in CONCORD_CSS and 'overflow-wrap:anywhere!important' in CONCORD_CSS
    assert 'body.concord-view .cc-message-body .xdc-card{max-width:100%!important}' in CONCORD_CSS
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
