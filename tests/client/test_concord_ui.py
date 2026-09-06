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
WEBXDC = (ROOT / "static/js/client/webxdc.js").read_text()
HTML = (ROOT / "templates/client.html").read_text()
PUSH = (ROOT / "app/services/nostr_push_service.py").read_text()


def test_concord_is_a_communities_section_of_messages_not_public_chat():
    assert 'data-view="messages"' in HTML
    discover = HTML.split('id="disc-sub"', 1)[1].split('</div>', 1)[0]
    assert 'data-view="concord"' not in discover
    assert 'static/js/client/concord.js' in HTML
    assert "renderModuleView('concord','concord.js','PCConcord','render')" in APP
    assert 'async function renderChatrooms()' not in APP
    assert 'data-view="chat"' not in HTML, "legacy Nostr Chat navigation must stay removed"


def test_messages_remains_the_single_room_and_dm_launcher():
    locked = APP.split("const NAV_LOCKED = new Set(", 1)[1].split(");", 1)[0]
    assert 'data-view="messages"' in HTML
    assert 'data-view="concord"' not in HTML


def test_packaged_shell_loads_the_complete_concord_surface():
    for asset in ("concord.css", "cord-reader.js", "concord.js"):
        assert asset in HTML
    assert 'id="messages-communities"' in APP


def test_concord_never_repaints_another_app_shared_feed():
    render = CONCORD[CONCORD.index('function render(){'):CONCORD.index('startDiscovery(p)', CONCORD.index('function render(){'))]
    assert "p.isView('concord')" in render
    assert render.index("p.isView('concord')") < render.index("p.$('#feed')")
    assert "PCOS.ownsFeedView('concord')" in render
    assert render.index("PCOS.ownsFeedView('concord')") < render.index("p.$('#feed')")
    assert "document.addEventListener('click'" not in CONCORD, (
        "an out-of-band click handler can repaint Concord after Code owns the feed")
    assert "isView: view => VIEW === view" in APP


def test_concord_is_precached_behind_the_messages_launcher():
    sw = (ROOT / "static/js/client/sw.js").read_text()
    assert "'/static/js/client/concord.js'" in sw
    assert "{ view:'concord', into:'#disc-sub'" not in APP
    assert 'data-view="messages"' in HTML


def test_classic_phone_concord_uses_os_style_rail_and_drawer_without_squeezed_chat():
    assert "mobileChatOpen=false" in CONCORD
    assert "mobileDrawerOpen=false" in CONCORD
    assert "mobileChatOpen||state.community==null?' show-chat'" in CONCORD
    assert "mobileChatOpen=true; mobileDrawerOpen=false; render(); enterChatBottom()" in CONCORD
    assert "mobileDrawerOpen=!mobileDrawerOpen" in CONCORD
    assert 'id="cc-drawer-backdrop"' in CONCORD
    assert 'body.concord-view .cc-app{position:fixed!important' in CONCORD_CSS
    assert 'grid-template-columns:58px minmax(0,1fr)!important' in CONCORD_CSS
    assert 'body.concord-view .cc-app.show-chat.drawer-open .cc-communities' in CONCORD_CSS
    assert 'body.concord-view .cc-app.show-chat.drawer-open .cc-channels' in CONCORD_CSS
    assert 'width:min(300px,calc(88vw - 58px))!important' in CONCORD_CSS
    assert 'body.concord-view .cc-app.show-chat{inset:0!important' in CONCORD_CSS
    assert 'body.concord-view .cc-app.home-view{inset:calc(58px + env(safe-area-inset-top)) 0 calc(61px + env(safe-area-inset-bottom)) 0!important' in CONCORD_CSS
    assert 'body.concord-view .cc-app.home-view{inset:0!important' not in CONCORD_CSS
    assert 'body.concord-view .cc-app.home-view #cc-back-channels{display:grid!important}' in CONCORD_CSS
    assert 'body.concord-view .cc-message-actions .cc-action-trigger' in CONCORD_CSS


def test_tablet_concord_consumes_the_full_shell_width():
    tablet = CONCORD_CSS.split('@media(min-width:821px) and (max-width:1180px)', 1)[1].split('}', 5)
    tablet = '}'.join(tablet)
    assert 'width:100vw!important' in tablet
    assert 'body.concord-view .main,body.concord-view .feed.feed-dm' in tablet
    assert 'body.concord-view .cc-app{width:100%!important;max-width:none!important' in tablet
    assert 'grid-template-columns:62px 220px minmax(0,1fr) 190px!important' in tablet


def test_web_concord_removes_the_timeline_shell_gutter_and_width_cap():
    assert 'body.concord-view .app{grid-template-columns:300px minmax(0,1fr)!important;width:100%!important;max-width:none!important;margin:0!important;padding:0!important;column-gap:0!important}' in CONCORD_CSS
    assert 'body.concord-view .main,body.concord-view .feed.feed-dm{width:100%!important;max-width:none!important' in CONCORD_CSS


def test_concord_owns_a_versioned_stylesheet_so_stale_shell_css_cannot_unstyle_it():
    assert 'static/css/concord.css' in HTML
    assert "data-concord-css" in CONCORD
    assert "concord.css?v=17" in CONCORD
    assert '.cc-compose textarea:focus' in CONCORD_CSS
    assert 'box-shadow:none!important' in CONCORD_CSS
    assert "'/static/css/concord.css'" in (ROOT / "static/js/client/sw.js").read_text()
    assert ".cc-app" in CONCORD_CSS and "grid-template-columns:68px 248px" in CONCORD_CSS
    assert CONCORD_CSS.count('{') == CONCORD_CSS.count('}'), "Concord CSS has an unclosed rule"


def test_native_bundles_package_the_concord_stylesheet():
    """Native shells load local /static assets, so a server copy cannot hide this omission.

    The bundlers no longer NAME each stylesheet — they derive the list from the shell template,
    because a hand-maintained list is what left `monero-wallet.css` out of the APK (present on the
    web, 404 in the app, the whole wallet unstyled). So the check is that concord.css is still
    linked by the shell and therefore still copied, rather than that a literal `cp` line exists."""
    html = (ROOT / "templates/client.html").read_text()
    assert '/static/css/concord.css' in html, (
        "the shell no longer links concord.css, so the bundlers will not copy it either")
    for bundle in ("mobile/build-www.sh", "desktop/build-www.sh"):
        build = (ROOT / bundle).read_text()
        assert 'templates/client.html' in build and 'static/css' in build, (
            f"{bundle} no longer derives its stylesheets from the shell template")


def test_desktop_release_audits_the_built_payload_not_only_source():
    workflow = (ROOT / ".github/workflows/desktop.yml").read_text()
    gate = workflow[workflow.index("name: Audit bundled Concord surface"):]
    for asset in ("concord.css", "concord.js", "cord-reader.js", "cord-protocol.js"):
        assert f"www/static/" in gate and asset in gate
    assert 'grep -q \'data-view="messages"\' www/index.html' in gate


def test_concord_fills_workspace_and_identifies_the_signed_in_user():
    assert "classList.toggle('concord-view', v==='concord')" in APP
    assert 'body.concord-view .rightbar,body.concord-view .rb-toggle,body.concord-view #rb-toggle{display:none!important' in CONCORD_CSS
    assert 'max-width:none!important' in CONCORD_CSS
    assert 'viewer: () =>' in APP
    assert 'Your Nostr identity' not in CONCORD
    assert '<small>You</small>' in CONCORD


def test_concord_has_discord_style_panes_and_dm_style_composer():
    for surface in ('cc-communities', 'cc-channels', 'cc-conversation', 'cc-members-pane', 'cc-messages', 'cc-compose'):
        assert surface in CONCORD
    assert 'Message #${state.channel' in CONCORD
    assert 'id="messages-direct"' in CONCORD
    assert 'id="messages-communities"' in APP


def test_desktop_members_are_a_right_column_and_mobile_uses_the_dialog():
    assert 'grid-template-columns:68px 248px minmax(0,1fr) 220px!important' in CONCORD_CSS
    assert 'class="cc-members-pane${membersHidden?' in CONCORD
    assert 'pc.concord.members.hidden' in CONCORD
    assert '.cc-app:not(:has(>.cc-members-pane)),.cc-app:has(>.cc-members-pane.hidden){grid-template-columns:68px 248px minmax(0,1fr)!important}' in CONCORD_CSS
    assert "!window.matchMedia||window.matchMedia('(max-width:820px)').matches" in CONCORD
    assert '@media(max-width:820px){.cc-members-pane{display:none!important}}' in CONCORD_CSS


def test_channels_can_be_starred_without_merging_concord_into_direct_messages():
    assert 'channelStarKey(room,name)' in CONCORD
    assert 'data-cc-star=' in CONCORD
    assert 'aria-pressed="${channelStarred(room,c.name)}"' in CONCORD
    assert 'orderedChannels(current)' in CONCORD
    assert '.cc-channel-star[aria-pressed="true"]' in CONCORD_CSS
    assert "VIEW==='messages' || VIEW==='concord'" in APP
    assert 'data-view="chat"' not in HTML


def test_thread_replies_tag_every_participant_once_but_never_the_sender():
    block = CONCORD.split('function threadParticipants(messages,target,viewerPubkey)', 1)[1].split('function webxdcOf', 1)[0]
    assert 'const root=rootId(target)' in block
    assert 'for(const node of [target,...rows])' in block
    assert 'rootId(node)!==root' in block
    assert 'node.pubkey!==viewerPubkey' in block
    assert 'people.add(node.pubkey)' in block
    assert "replyTags.push(['P',pk],['p',pk])" in CONCORD
    assert "filter(t=>['K','E'].includes(t[0]))" in CONCORD


def test_starred_channels_have_a_distinct_nonduplicated_section():
    assert 'function channelSectionsHtml(p,room,channels)' in CONCORD
    assert 'cc-starred-section">STARRED' in CONCORD
    assert "filter(c=>channelStarred(room,c.name))" in CONCORD
    assert "filter(c=>!channelStarred(room,c.name))" in CONCORD
    assert 'channelSectionsHtml(p,current,visibleChannels)' in CONCORD


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
    assert "localStorage.setItem('pc.concord.active',String(index))" in CONCORD
    assert 'mobileChatOpen=false, mobileDrawerOpen=false, discoveryOpen=false' in CONCORD
    assert "discoveryOpen=true; state.community=null" in CONCORD
    # `state.thread=null` joined this line: a thread belongs to one channel, and left set
    # across a move its filter matches nothing and the new channel looks empty — which reads
    # as lost messages rather than a stale filter. The drill-in rule itself is unchanged.
    assert ("state.channel=channel; state.thread=null; replyTarget=null; "
            "mobileChatOpen=true; mobileDrawerOpen=false") in CONCORD
    assert "mobileChatOpen=false; mobileDrawerOpen=false; render()" in CONCORD
    assert 'id="cc-home" title="Your rooms"' in CONCORD
    assert 'id="cc-discovery" title="Discover public communities"' in CONCORD
    assert "discoveryOpen=!rooms.length" in CONCORD
    assert "return channels.length?channels:[{name:'general',private:false}]" in CONCORD
    assert 'channelSectionsHtml(p,current,visibleChannels)' in CONCORD
    assert 'if(channels.length)room.channels=channels' in CONCORD
    assert "if(room&&room.cord&&!hydratedRoomViews.has(roomIdentity(room)))" in CONCORD
    assert "await hydrateRoomStreams(p,state.community)" in CONCORD
    assert "if(state.community==null){ const rooms=saved(),wanted=Number(localStorage.getItem('pc.concord.active')" in CONCORD
    assert "state.community==null?'Back to rooms':'Rooms and channels'" in CONCORD


def test_created_and_joined_communities_survive_browser_storage_loss():
    assert 'async function persistArmadaMembership(p,room)' in CONCORD
    assert 'await p.nip44enc(viewer.pubkey,JSON.stringify(list))' in CONCORD
    assert 'await p.publish(13302,content,[])' in CONCORD
    assert 'await persistArmadaMembership(p,room)' in CONCORD
    assert 'recoverOwnedInvite(p,item)' in CONCORD
    assert "item.source.pubkey!==viewer.pubkey" in CONCORD


def test_leaving_a_community_publishes_a_membership_tombstone_before_removal():
    assert 'id="cc-leave-shortcut" title="Leave community" aria-label="Leave community"' in CONCORD
    # Two entry points, one handler: the conversation header AND the rooms/channels pane, which is
    # the screen a phone opens on ('.cc-conversation' is display:none there until a channel is
    # opened, so a control that lives only in its header is present and 0x0).
    assert 'id="cc-leave-room" title="Leave community"' in CONCORD
    assert "const leaveShortcut=$('#cc-leave-shortcut');if(leaveShortcut)leaveShortcut.onclick=leaveByHeader;" in CONCORD
    assert "const leaveRoom=$('#cc-leave-room');if(leaveRoom)leaveRoom.onclick=leaveByHeader;" in CONCORD
    assert 'async function leaveArmadaMembership(p,room)' in CONCORD
    # The tombstone names the INVITE too — a tombstone keyed only on community_id is unmatchable
    # by the owner's own announcement, which is the only thing that can put the room back.
    # `cid` is roomIdentity(room): community id, else naddr, else url. A room joined by a plain
    # invite link has no community_id at all, and keying its tombstone on one wrote no tombstone —
    # leaving succeeded silently and the vault could put the room straight back.
    assert "tombs.set(cid,{community_id:cid,removed_at:removedAt," in CONCORD
    assert "const cid=roomIdentity(room);" in CONCORD
    assert "...(leftRef?{invite_ref:leftRef}:{}),...(leftNaddr?{naddr:leftNaddr}:{})});" in CONCORD
    assert "rememberLeftCommunity(viewer.pubkey,room,removedAt)" in CONCORD
    assert "forgetLeftCommunity(viewer.pubkey,room)" in CONCORD
    assert "wasLocallyLeft(viewer.pubkey,item)" in CONCORD
    # BOTH key forms. A tombstone written by this build names roomIdentity (community id, else
    # naddr, else url) because an invite-link room has no community id; an older one names only
    # the community id. Removing on one alone leaves the other kind of leave un-applied.
    assert ("kept=rooms.filter(room=>!dead.has(room.communityId)&&!dead.has(roomIdentity(room))"
            "&&!wasLocallyLeft(viewer.pubkey,room))") in CONCORD
    assert "await p.publish(13302,content,[])" in CONCORD
    handler = CONCORD.split("const leave=$('#cc-leave-community')", 1)[1].split("const leaveByHeader", 1)[0]
    assert handler.index('await leaveArmadaMembership(p,room)') < handler.index('const latest=saved()')
    assert 'removeCommunityByIdentity(latest,leavingId)' in handler
    assert "rooms.splice(index,1)" not in handler
    assert "localStorage.setItem('pc.concord.active',String(state.community))" in handler
    assert "localStorage.removeItem('pc.concord.active')" in handler
    assert "roomInvite.title='Invite people'" in CONCORD


def test_send_is_optimistic_and_does_not_wait_for_relays_to_paint():
    handler = CONCORD.split("send.onclick=async()=>", 1)[1].split("input.onkeydown", 1)[0]
    assert "pending:!room.local" in handler
    assert handler.index("saveTestMessages(storeId,m)") < handler.index("await publishCordMessage")
    assert "failed.pending=false;failed.failed=true" in handler


def test_relay_echo_racing_optimistic_send_cannot_leave_two_messages():
    """The echo may land before publish() resolves and renames the pending row to its rumor id."""
    assert "function uniqueMessages(v)" in CONCORD
    assert "const id=messageId(m),old=byId.get(id)" in CONCORD
    assert "byId.set(id,old?{...old,...m}:m)" in CONCORD
    assert "return uniqueMessages(v)" in CONCORD, "old duplicated caches are not repaired on read"
    assert "function mergeRelayMessages(prior,incoming)" in CONCORD
    assert "m&&m.pending&&String(m.pubkey||'')===String(remote&&remote.pubkey||'')" in CONCORD
    assert "const pending=pendingEchoMatch(out,remote)" in CONCORD
    assert "sort((a,b)=>a.gap-b.gap)" in CONCORD
    assert "if(candidates.length>1)return null" in CONCORD
    assert "Object.assign(pending,remote,{pending:false,remote:true})" in CONCORD
    assert "mergeRelayMessages(prior,incoming)" in CONCORD
    assert "JSON.stringify(clean.slice(-200))" in CONCORD, \
        "local test-room relay races can still be persisted"
    assert "remoteMessages.set(id,clean.slice(-5000))" in CONCORD
    assert "localStorage.removeItem('pc.concord.test.'+id)" in CONCORD, \
        "remote decrypted rumors must not remain as plaintext localStorage"


def test_desktop_recovery_merges_armada_list_shards_and_backs_off_empty_queries():
    assert "function membershipEvents(p,pubkey" in CONCORD
    assert "{kinds:[13302],authors:[pubkey],limit:1}" in CONCORD
    assert "{kinds:[33302],authors:[pubkey],'#d':[''],limit:20}" in CONCORD
    assert "kinds:[13302,33302]" not in CONCORD
    assert "for(const event of candidates)" in CONCORD
    assert "const entries=new Map(),tombs=new Map()" in CONCORD
    assert "Math.max(Number(tombs.get(t.community_id))" in CONCORD
    assert "membershipRetryTimer=setTimeout" in CONCORD
    assert "recovered?60000:120000" in CONCORD
    assert "window.PosterCordReader.inspectControl(bundle,[])" in CONCORD


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


def test_every_room_entry_tracks_async_growth_all_the_way_to_latest():
    """Community metadata, decrypted history and media arrive after the first paint."""
    assert "function enterChatBottom()" in CONCORD
    assert "[0,60,180,450,900,1600]" in CONCORD
    server = CONCORD.split("$$('[data-cc-server]')", 1)[1].split("$$('[data-cc-discover]')", 1)[0]
    activation = CONCORD.split('async function activateJoinedRoom', 1)[1].split('function render()', 1)[0]
    discover = CONCORD.split("$$('[data-cc-discover]')", 1)[1].split("$$('[data-cc-channel]')", 1)[0]
    channel = CONCORD.split("$$('[data-cc-channel]')", 1)[1].split("$$('[data-cc-star]')", 1)[0]
    assert 'activateJoinedRoom(p,i,inDrawer)' in server
    for handler in (activation, discover, channel):
        assert handler.count('enterChatBottom()') >= 2


def test_concord_ctrl_or_cmd_enter_sends_without_breaking_plain_enter():
    assert 'bind(me);' in CONCORD and 'function bind(me)' in CONCORD
    assert "e.key==='Enter'||e.code==='Enter'" in CONCORD
    assert 'e.preventDefault(); return send.onclick()' in CONCORD
    assert "e.key==='Enter'&&!e.shiftKey" not in CONCORD


def test_authors_can_delete_their_own_messages_after_relay_acceptance():
    assert 'data-cc-delete' in CONCORD
    assert "found.pubkey!==viewer.pubkey" in CONCORD
    assert "p.uiConfirm('Delete this message?',{ok:'Delete',danger:true})" in CONCORD
    assert "[['e',id],['k',String(found.kind||9)]],5" in CONCORD
    assert "messages.filter(m=>messageId(m)!==id)" in CONCORD
    assert "if(!removeMessageRow(id))preserveChatScroll(()=>render())" in CONCORD
    assert "above?Math.max(0,top-lost):top" in CONCORD


def test_owner_can_publish_an_interoperable_cord_ban():
    assert 'data-cc-member-ban' in CONCORD
    assert 'canBan=isOwner&&target!==viewer.pubkey' in CONCORD
    assert 'const banMember=async target=>' in CONCORD
    assert 'reader.createBanWrap' in CONCORD
    assert 'community relays rejected the ban' in CONCORD
    assert 'latest[roomIndex].banned=made.banned' in CONCORD


def test_concord_create_and_send_flow_executes_without_runtime_errors():
    run = subprocess.run(
        ["node", str(ROOT / "tests/client/concord_runtime.mjs")],
        cwd=ROOT, capture_output=True, text=True, timeout=10,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "concord runtime flow ok" in run.stdout


def test_armada_membership_snapshot_is_resolved_through_its_invite_bundle():
    assert "Armada's vault `current` is a CONTROL SNAPSHOT" in CONCORD
    assert "hydrated=await hydrateInvite(p,url)" in CONCORD
    assert "bundle=mergeArmadaBundle(hydrated.cord.bundle,current)" in CONCORD
    assert "catch(__){continue;}" in CONCORD


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


def test_public_community_cards_do_not_fan_out_across_invite_relays():
    on_event = CONCORD.split("const onEvent=ev=>", 1)[1].split("const onEose", 1)[0]
    assert 'hydrateDiscoveredIcon' not in on_event
    assert 'publicRoomIcon(p,r)' in CONCORD
    assert '.cc-public-icon img' in CONCORD_CSS
    assert "u.protocol==='https:'||u.protocol==='http:'" in CONCORD
    assert '.cc-server-img' in CONCORD_CSS


def test_concord_standard_controls_are_wired_not_decorative():
    assert 'id="cc-attach"' in CONCORD and 'file.onchange=async' in CONCORD
    assert "input.onpaste=event=>" in CONCORD
    assert "item.kind==='file'" in CONCORD
    assert "String(item.type||'').startsWith('image/')" in CONCORD
    assert 'event.preventDefault(); void uploadAttachments(images)' in CONCORD
    assert "pendingAttachments.set(url,tag)" in CONCORD
    assert "class=\"cc-plain-attachment\"" in CONCORD
    assert 'p.uploadBlob' in CONCORD
    assert 'p.blossomPicker(null,insertBlossomAttachment' in CONCORD
    assert '📁 Files' in CONCORD
    assert 'pendingAttachments.set(url,tag)' in CONCORD
    assert 'id="cc-emoji"' in CONCORD and 'p.openEmojiPopover' in CONCORD
    assert 'id="cc-members"' in CONCORD and "members.onclick" in CONCORD
    assert 'id="cc-notify"' in CONCORD and 'p.askOsNotify' in CONCORD
    assert 'id="cc-call"' in CONCORD and 'p.startGroupCall' in CONCORD
    call_handler = CONCORD.split("const call=$('#cc-call')", 1)[1].split("const cancel=", 1)[0]
    assert 'roomParticipants(room,viewerPk)' in call_handler
    assert 'activeMessages(room).map' not in call_handler
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
    assert 'communityId:bundle.community_id' in CONCORD
    join_handler = CONCORD.split("const go=$('#cc-join-go')", 1)[1].split("$$('[data-cc-server]')", 1)[0]
    assert 'await hydrateRoomStreams(p,state.community)' in join_handler
    assert join_handler.index('await hydrateRoomStreams') < join_handler.index("p.toast('community joined')")
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
    assert "if(room.cord&&!hydratedRoomViews.has(roomIdentity(room)))" in CONCORD
    assert 'let room=rooms[index]' in CONCORD and 'state.community=index' in CONCORD
    assert 'await p.nip44dec(viewer.pubkey,event.content)' in CONCORD
    assert 'cc-public-copy' in CONCORD and '.cc-public-copy' in CONCORD_CSS
    assert 'function isUnread(room)' in CONCORD
    assert '.cc-channel.unread' in CONCORD_CSS and '.cc-server.unread' in CONCORD_CSS
    assert "channelReadKey(room,name)" in CONCORD
    assert "markRead(current,state.channel||'general')" in CONCORD
    assert "seenAt(room,c.name)" in CONCORD
    assert "if(current)markRead(current);" not in CONCORD
    assert "notifyMentions(p,current,messages,viewer,me,state.channel||'general')" in CONCORD
    assert 'route:notificationRoute(room,channel,m)' in CONCORD and 'concord-mention-' in CONCORD
    assert "mentionSeenKey(room,channel)" in CONCORD
    # EVERY MENTION IS ITS OWN NOTIFICATION. The tag was room.naddr + channel, so a second mention
    # in the same channel REPLACED the first — reported as "I see 1 notification, I have way more
    # than 1" — and a room with no naddr (NIP-29, or joined by community id) tagged every mention
    # in every room `concord-mention-undefined:general`, collapsing them all into one.
    assert "roomIdentity(room)+':'+channel+':'+messageId(m)" in CONCORD
    assert "room.naddr+':'+channel" not in CONCORD
    # A p/P TAG IS WHAT MAKES A MESSAGE A MENTION, wherever that rule now lives. It used to be
    # inline here; it moved into `messageMentionsViewer` so the RENDERER can ask the same question
    # — a message that tags you has to look different, not merely raise a notification. Asserted as
    # the rule rather than as the old literal, so the next refactor does not fail for being tidy.
    assert "messageMentionsViewer(m,viewer,me)" in CONCORD
    assert "(t[0]==='p'||t[0]==='P')&&String(t[1]||'')===viewer.pubkey" in CONCORD, (
        "a p/P tag no longer decides whether a message mentions you")
    assert "mentionRecipients.set(handle.toLowerCase(),choice.pk)" in CONCORD
    assert "mentionTags.push(['P',pk],['p',pk])" in CONCORD
    # The text half of the same rule: a client that writes only the handle (no p tag) must still
    # register as a mention. Same move as above — it now lives in messageMentionsViewer.
    assert "textMentionsViewer(String(m.text||''),viewerHandles(viewer,me))" in CONCORD
    assert "lower.includes('@'+h)" not in CONCORD
    assert 'import_meta.env' not in CORD_READER
    assert 'CapacitorException' not in CORD_READER and 'registerPlugin' not in CORD_READER
    assert 'await decryptImagePointer(value,loadKey,ref)' in CONCORD and "crypto.subtle.decrypt({name:'AES-GCM'" in CONCORD
    assert 'await window.PCConcordCache.getIcon(loadKey,ref)' in CONCORD
    assert "search:'armada.buzz/invite'" in CONCORD and "search:'poster.place/invite'" in CONCORD
    assert 'data-cc-discover' in CONCORD and 'relaySubscribe:' in APP
    assert 'class="cc-message-avatar"' in CONCORD and '.cc-message-avatar' in CONCORD_CSS
    assert 'linkify, linkCardHtml, hydrateLinkCards' in APP


def test_concord_dove_icon_remains_available_inside_the_messages_app():
    sprite = (ROOT / 'static/js/client/sprite.js').read_text()
    assert 'id="i-concord"' in sprite
    assert 'data-view="concord"' not in HTML
    assert 'class="messages-tabs"' in CONCORD
    os_js = (ROOT / 'static/js/client/os.js').read_text()
    assert "btn.querySelector('svg use')" in os_js
    assert "if(view==='concord') snapTo(w,'max')" in os_js  # old invite/shortcut route compatibility
    assert '.osw-slot:has(>.cc-app)' in CONCORD_CSS
    assert '.osw-body>#feed.feed-dm:has(.cc-app)' in CONCORD_CSS


def test_maximised_os_window_uses_frame_width_not_zoomed_viewport_width():
    """100vw is narrower than a maximised frame when PosterChanOS body zoom is below one."""
    assert 'body.os-on.concord-view .osw-body>#feed.feed-dm:has(.cc-app)' in CONCORD_CSS
    os_rule = CONCORD_CSS[CONCORD_CSS.index(
        'body.os-on.concord-view .osw-body>#feed.feed-dm:has(.cc-app)'
    ):]
    os_rule = os_rule[:os_rule.index('}')]
    assert 'width:100%!important' in os_rule
    assert 'max-width:none!important' in os_rule
    assert '.cc-conversation' in os_rule and '.cc-messages' in os_rule
    assert '100vw' not in os_rule


def test_invite_parser_requires_naddr_and_secret_fragment():
    assert "/\\/invite\\/(naddr1" in CONCORD
    assert "m&&u.hash.length>3" in CONCORD
    assert "secret:u.hash.slice(1)" in CONCORD
    invite_loader = CONCORD[CONCORD.index("async function hydrateInvite"):CONCORD.index("function inviteRefUrl")]
    assert "fetch(" not in invite_loader, "invite fragments must never be posted to PosterChan"


def test_linkified_invites_stay_inside_concord_instead_of_opening_classic_ui():
    assert "function openInviteLink(raw,autoJoin=true)" in CONCORD
    assert "e.target.closest('a[href]')" in CONCORD
    assert "e.preventDefault();e.stopPropagation();openInviteLink(a.href,true)" in CONCORD
    assert "openInvite:openInviteLink" in CONCORD


def test_direct_invite_route_opens_concord_with_the_fragment_intact():
    assert "kind:'concord-invite', q:location.href" in APP
    routed = APP[APP.index("async function routeFromPath()"):APP.index("// PWA launch params")]
    assert "switchView('concord')" in routed
    assert "PCConcord.openInvite(e.q,true)" in routed
    assert "_withModule('concord.js','PCConcord',open)" in routed
    assert "_withModule('/static/js/client/concord.js'" not in routed
    boot_start = APP.index("const _deepLink = _entityFromPath()")
    boot = APP[boot_start:APP.index("// Drain a file/text shared IN", boot_start)]
    assert "_deepLink.kind==='concord-invite'" in boot
    assert "_deepLinkRouted=true; routeFromPath()" in boot
    assert "_entityFromPath() && !_deepLinkRouted" in boot


def test_concord_controls_are_phone_sized_and_single_column():
    """EVERY 600px block, not the first one that happens to be in the file.

    This read `re.search`, i.e. the FIRST `@media(max-width:600px)` in client.css, and asserted
    Concord's rules were inside it. That held only for as long as Concord's block stayed first: an
    unrelated feature adding its own phone breakpoint earlier in the stylesheet turned this red
    while every rule it checks was still present and correct. A stylesheet may have as many
    breakpoints as it likes and the cascade does not care which is first, so neither should this.
    """
    phones = re.findall(r"@media\(max-width:600px\)\{(.*?)\n\}", CSS, re.S)
    assert phones, "missing phone breakpoint"
    rules = "\n".join(phones)
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
    assert 'data-cc-quick-react=' not in CONCORD
    assert 'data-cc-actions=' in CONCORD
    assert "row.classList.toggle('cc-actions-open',open)" in CONCORD
    assert '.cc-message.cc-actions-open .cc-message-actions button{display:grid' in CONCORD_CSS
    assert 'cc-reaction${mine?' in CONCORD and 'aria-pressed="${mine}"' in CONCORD
    assert '.cc-reaction.mine' in CONCORD_CSS
    assert '.cc-action-sep' in CONCORD_CSS


def test_room_history_reads_pool_and_external_relays_without_erasing_cached_messages():
    assert 'async function cordQuery(' in CONCORD
    assert 'if(p.relayQuery)jobs.push' in CONCORD
    assert 'if(p.relayQueryFrom)jobs.push' in CONCORD
    assert 'const storeId=channelStoreId(room,channel.name);markRemoteStore(storeId);const prior=testMessages(storeId)' in CONCORD
    assert 'mergeRelayMessages(prior,msgs)' in CONCORD
    assert 'since,limit:500' in CONCORD


def test_concord_webxdc_mentions_live_sync_and_scroll_are_integrated():
    assert "application/x-webxdc" in CONCORD and "application/vnd.webxdc+zip" in CONCORD
    assert "pendingAttachments.set(url,tag)" in CONCORD
    assert 'hydrateWebxdcCards(current)' in CONCORD and 'PCWebxdc.cardHtml(app)' in CONCORD
    assert 'reactionIds:' in CORD_READER and 'extraTags' in CORD_READER
    assert 'mentionToken' in CONCORD and "e.key==='Tab'" in CONCORD
    assert 'mentionBox' not in CONCORD
    assert 'refreshActiveChannel(p)' in CONCORD and 'setInterval(()=>{refreshRoomMetadata(p);refreshActiveChannel(p);},4000)' in CONCORD
    assert 'scrollStates' in CONCORD and 'st.pinned' in CONCORD and 'preserveChatScroll' in CONCORD


def test_playable_webxdc_card_replaces_only_its_redundant_raw_url():
    content = CONCORD.split('function messageContentHtml(p,m,room,channelName)', 1)[1].split('async function decryptAttachment', 1)[0]
    assert 'const mini=webxdcOf(m,room,channelName)' in content
    assert 'window.PCWebxdc&&PCWebxdc.cardHtml' in content
    assert "text=text.split(mini.url).join('')" in content
    # The URL remains available as a normal link when no playable-card implementation is loaded.
    assert 'if(canPlayMini)' in content
    assert 'PCWebxdc.cardHtml(mini)' in content


def test_concord_replaces_stale_bare_link_card_with_explicit_armada_topic():
    hydrate = CONCORD.split('function hydrateWebxdcCards(room)', 1)[1].split('function hexBytes', 1)[0]
    assert "prior.uuid!==app.uuid" in hydrate
    assert "prior.urlTopicMessageId!==app.urlTopicMessageId" in hydrate
    assert "old.remove()" in hydrate
    assert "mime==='application/vnd.webxdc+zip'" in CONCORD.split('function publicAttachments', 1)[1].split('function messageContentHtml', 1)[0]
    assert 'function resolveWebxdcCard(card,fallback)' in CONCORD
    assert 'if(fallback&&fallback.uuid)return fallback' in CONCORD
    assert 'activeMessages(room).find(x=>messageId(x)===id)' in CONCORD
    assert 'PCConcord.resolveWebxdcCard(card,app)' in WEBXDC
    assert "rtDiagnostic('card-play'" in WEBXDC


def test_mobile_room_list_and_drawer_do_not_consume_channel_unread_state():
    assert "function conversationIsVisible(narrow,chatOpen,drawerOpen)" in CONCORD
    assert "return !narrow||(!!chatOpen&&!drawerOpen)" in CONCORD
    assert "conversationIsVisible(narrow,mobileChatOpen,mobileDrawerOpen)" in CONCORD
    assert "if(current)markRead(current,state.channel||'general')" not in CONCORD


def test_entering_channel_wins_scroll_race_with_history_and_media():
    assert 'function enterChatBottom()' in CONCORD
    assert "[0,60,180,450,900,1600]" in CONCORD
    assert "scroller.dataset.ccScrollRestore" in CONCORD
    assert "delete box.dataset.ccScrollRestore" in CONCORD
    channel_click = CONCORD.split("$$('[data-cc-channel]')", 1)[1].split("$$('[data-cc-star]')", 1)[0]
    assert channel_click.count('enterChatBottom()') == 2
    assert 'function watchPinnedRoomGrowth(scroller)' in CONCORD
    assert "new ResizeObserver" in CONCORD
    assert "if(st.pinned===false" in CONCORD
    assert "watchPinnedRoomGrowth(scroller)" in CONCORD


def test_all_joined_community_metadata_repaints_live_without_moving_chat():
    block = CONCORD.split('async function refreshRoomMetadata(p)', 1)[1].split('function startLiveSync', 1)[0]
    assert 'eligible[metadataCursor++%eligible.length]' in block
    assert 'roomControls.set(loadKey,wraps||[])' in block
    assert "assign('name'" in block and "assign('description'" in block
    assert "await applyRoomIconMetadata(room,info,loadKey,seed)" in block
    assert 'const roomIconRefs=new Map()' in CONCORD
    assert 'rooms[selected.index]=room;save(rooms);preserveChatScroll(()=>backgroundRender())' in block
    assert 'scrollChatBottom' not in block


def test_background_repaints_never_replace_a_focused_workspace_control():
    helper = CONCORD.split('function backgroundRender()', 1)[1].split('function handoffState', 1)[0]
    # Native/mobile Concord is not guaranteed to live under the classic #feed host. The composer
    # itself must independently qualify for deferral or a relay update replaces it and the IME.
    assert 'active===input||' in helper
    assert 'feed.contains&&feed.contains(active)' in helper
    assert 'backgroundRenderPending=true' in helper
    assert "active.addEventListener('focusout'" in helper
    assert 'if(backgroundRenderPending)backgroundRender()' in helper
    assert 'backgroundRenderPending=false;backgroundFocusHost=null;render();return true' in helper
    assert 'window.PCConcord={render,backgroundRender,' in CONCORD

    # These are the async paths that used to rebuild #feed underneath Android's keyboard.
    # The channel's merge-and-paint moved into absorbChatWraps when the live subscription was
    # added, so one implementation serves both the periodic tick and a pushed event.
    live = CONCORD.split('async function absorbChatWraps(', 1)[1].split(
        'async function refreshRoomMetadata(p)', 1)[0]
    metadata = CONCORD.split('async function refreshRoomMetadata(p)', 1)[1].split(
        'function startLiveSync', 1)[0]
    hydrate = CONCORD.split('async function hydrateRoomStreams', 1)[1].split(
        'async function publishCordMessage', 1)[0]
    assert 'backgroundRender()' in live
    assert 'backgroundRender()' in metadata
    assert hydrate.count('backgroundRender()') >= 2


def test_icon_removal_and_failure_cannot_block_room_history_hydration():
    helper = CONCORD.split('async function applyRoomIconMetadata', 1)[1].split('function reactionSummary', 1)[0]
    decrypt = CONCORD.split('async function decryptImagePointer', 1)[1].split('async function applyRoomIconMetadata', 1)[0]
    hydrate = CONCORD.split('async function hydrateRoomStreams', 1)[1].split('async function publishCordMessage', 1)[0]
    assert "hasOwnProperty.call(info,'icon')" in helper
    assert "const icon=value?" in helper and ":''" in helper
    assert "console.warn('Concord community icon could not be loaded'" in helper
    assert 'nonce.byteLength!==16&&nonce.byteLength!==12' in decrypt
    assert 'return false' in helper
    assert 'void applyRoomIconMetadata(room,info,loadKey,seed).then' in hydrate
    assert 'await applyRoomIconMetadata(room,info,loadKey' not in hydrate
    assert 'const channelCount=applyControl(controlWraps)' in hydrate
    assert 'const channelCount=await applyControl(controlWraps)' not in hydrate


def test_cached_room_history_first_paint_never_waits_for_the_community_icon():
    hydrate = CONCORD.split('async function hydrateRoomStreams', 1)[1].split(
        'async function publishCordNative', 1
    )[0]
    cached = hydrate.split('if(controlWraps.length)', 1)[1].split(
        'const completeControl=', 1
    )[0]
    assert 'applyControl(controlWraps);' in cached
    assert 'await applyControl(controlWraps)' not in cached
    assert 'cachedEnvelopePage' in cached
    assert 'await applyChannel(channel,wraps)' in cached
    assert cached.index('applyControl(controlWraps);') < cached.index('cachedEnvelopePage')


def test_armada_membership_refresh_preserves_hydrated_cache_and_clicks_restore_once_per_process():
    sync = CONCORD.split('async function syncArmadaMemberships', 1)[1].split(
        'async function persistArmadaMembership', 1)[0]
    assert 'mergeArmadaBundle(priorBundle,current)' in sync
    assert 'bundleUnchanged' in sync
    assert 'existing.cord.hydrated&&bundleUnchanged' in sync
    activation = CONCORD.split('async function activateJoinedRoom', 1)[1].split('function render()', 1)[0]
    assert '(!room.cord||!room.cord.bundle)' in activation
    assert 'room.cord&&!hydratedRoomViews.has(roomIdentity(room))' in activation
    assert 'hydratedRoomViews.add(identity)' in CONCORD
    assert 'room.cord.armadaList' not in activation
    assert "if(membershipViewer!==viewer.pubkey){membershipViewer=viewer.pubkey;membershipDocs.clear();}" in sync
    assert 'let doc=membershipDocs.get(event.id)' in sync
    assert 'while(membershipDocs.size>256)' in sync


def test_room_history_errors_are_coalesced_instead_of_toaster_spam():
    """SAID ONCE, UNTIL IT WORKS — not once every thirty seconds.

    This used to assert a 30s re-toast window, which is the behaviour it was written to prevent,
    only slower: on a phone the condition LASTS — a community whose relays are unreachable is not a
    passing blip — so the screen filled with "could not refresh room history" on a loop, over a room
    the client was already retrying every four seconds. Reported as exactly that.

    A DIFFERENT message must still speak, and the notice is cleared on success by the channel
    handler below, so a room that recovers and breaks again says so again."""
    assert 'const roomLoadNotices=new Map()' in CONCORD
    warning = CONCORD.split('function roomLoadWarning', 1)[1].split(
        'function decodeMembershipLists', 1)[0]
    # AND COMPARING THE WHOLE MESSAGE WAS NOT ENOUGH, which is why this now asserts the shape.
    # The commonest failure here is deliberately diagnostic — "no channels readable yet - 4 relay(s)
    # asked, 191 control event(s) held, 3 new" — so its COUNTS move on every attempt. Compared
    # whole, no two are ever equal, this guard never matched, and the four-second retry toasted
    # every four seconds anyway: the same wall of messages, reported again.
    assert "const shape=message.replace(/\\d+/g,'#')" in warning, warning
    assert 'if(old&&old.shape===shape)return;' in warning, warning
    assert 'now-old.at>' not in warning, "a time window re-toasts a condition nobody can act on"
    channel = CONCORD.split("$$('[data-cc-channel]')", 1)[1].split(
        "$$('[data-cc-star]')", 1)[0]
    assert "roomLoadWarning(p,noticeKey,'could not refresh room history: ',e)" in channel


def test_slow_history_load_persists_by_room_identity_not_stale_array_index():
    hydrate = CONCORD.split('async function hydrateRoomStreams', 1)[1].split(
        'async function publishCordNative', 1)[0]
    assert 'latest.findIndex(item=>roomIdentity(item)===identity)' in hydrate
    assert 'if(!persistRoom())return' in hydrate
    assert 'rooms[index]=room' not in hydrate


def test_armada_encrypted_attachments_are_decrypted_before_media_rendering():
    assert "f['encryption-algorithm']" in CONCORD
    assert "crypto.subtle.decrypt({name:'AES-GCM'" in CONCORD
    assert "hash!==file.hash" in CONCORD
    assert 'messageContentHtml(p,m,current,state.channel)' in CONCORD
    assert 'hydrateEncryptedAttachments(messages)' in CONCORD
    assert 'class="cc-attachment-open"' in CONCORD
    assert 'attachmentLightbox(p,host,got.url,null)' in CONCORD
    assert 'e.preventDefault();e.stopPropagation()' in CONCORD
    assert 'data-cc-attachment-index' in CONCORD


def test_concord_attachments_fit_chat_and_use_the_post_lightbox():
    assert 'function attachmentLightbox(p,host,url,kind)' in CONCORD
    assert "querySelectorAll('.cc-encrypted-attachment img,.cc-encrypted-attachment video')" in CONCORD
    assert "attachmentLightbox(p,host,got.url,'video')" in CONCORD
    assert 'class="cc-attachment-expand"' in CONCORD
    assert 'width:fit-content!important;max-width:min(560px,100%)' in CONCORD_CSS
    assert 'object-fit:contain!important' in CONCORD_CSS


def test_plain_room_images_and_videos_open_fullscreen_on_every_layout():
    assert "function wireRoomMedia(p)" in CONCORD
    assert "querySelectorAll('.cc-message-body img,.cc-message-body video')" in CONCORD
    assert "!el.closest('.cc-encrypted-attachment')" in CONCORD
    assert "el.tagName==='VIDEO'?'video':null" in CONCORD
    assert "e.preventDefault();e.stopPropagation()" in CONCORD
    assert "wireRoomMedia(p);" in CONCORD
    assert "if(video)video.ondblclick=openVideo" in CONCORD
    assert "if(open)open.onclick=openVideo" in CONCORD


def test_video_playback_controls_are_not_replaced_by_the_lightbox_handler():
    wire = CONCORD.split("function wireRoomMedia(p)", 1)[1].split("async function hydrateEncryptedAttachments", 1)[0]
    encrypted = CONCORD.split("async function hydrateEncryptedAttachments", 1)[1].split("function channelStarKey", 1)[0]
    assert "if(el.tagName==='VIDEO')" in wire
    assert "el.ondblclick=open" in wire
    assert "else el.onclick=open" in wire
    assert "el.onclick=open" not in wire.split("if(el.tagName==='VIDEO')", 1)[1].split("else el.onclick=open", 1)[0]
    assert "if(video)video.ondblclick=openVideo" in encrypted
    assert "if(video)video.onclick=openVideo" not in encrypted
    assert 'class="cc-attachment-expand"' in encrypted


def test_concord_brand_always_returns_to_discovery():
    assert 'id="cc-home"' in CONCORD
    assert "state.community=null; state.channel=null; render()" in CONCORD
