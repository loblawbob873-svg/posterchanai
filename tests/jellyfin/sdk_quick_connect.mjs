// Real official SDK requests, including server scoring and URL construction.
// Used by test_jellyfin.py against isolated servers and generated media only.
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
const root = process.env.JELLYFIN_TEST_SDK;
const { Jellyfin } = await import(pathToFileURL(root+'/lib/index.js'));
/* @jellyfin/sdk 1.0.0 MOVED FOUR OF THESE, and this file is the only place that names them —
 * our own Quick Connect is served by `app/routers/jellyfin.py` over plain HTTP and never imported
 * the SDK, so the endpoints kept working while this compatibility check went red:
 *   getQuickConnectApi -> getAuthenticationApi   (Quick Connect is authentication now)
 *   getUserViewsApi    -> getUserViewApi         (singular)
 *   getItemsApi        -> getLibraryApi          (getItems moved to the library API)
 *   getPlaystateApi    -> getSessionApi          (reportPlaybackStopped moved to sessions)
 * `authenticateWithQuickConnect` came off the user API at the same time. Verified against the
 * published 1.0.0 tarball rather than guessed. */
const { getAuthenticationApi, getUserApi, getUserViewApi, getLibraryApi, getMediaInfoApi, getSessionApi } =
  await import(pathToFileURL(root+'/lib/utils/api/index.js'));
const sdk = new Jellyfin({clientInfo:{name:'Posterchan compatibility test',version:'1'},deviceInfo:{name:'Test TV',id:'isolated-tv'}});
const base = process.env.JELLYFIN_TEST_SERVER;
const discovered = await sdk.discovery.getRecommendedServers([base]);
assert.equal(discovered.length,1,'official SDK rejected the server');
assert(!discovered[0].issues.some(issue=>issue.constructor.name==='ProductNameIssue'));
let api = sdk.createApi(base);
assert.equal((await getAuthenticationApi(api).getQuickConnectEnabled()).data,true);
const pending = (await getAuthenticationApi(api).initiateQuickConnect()).data;
assert.equal(pending.Authenticated,false);
// Simulate the Nostr-authenticated approval; the isolated server fixture provides identity.
const approval = await fetch(new URL('/api/media-center/jellyfin-account/authorize',base),{
 method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:pending.Code})
});
assert.equal(approval.status,200);
assert.equal((await getAuthenticationApi(api).getQuickConnectState({secret:pending.Secret})).data.Authenticated,true);
const login = (await getAuthenticationApi(api).authenticateWithQuickConnect({quickConnectDto:{Secret:pending.Secret}})).data;
/* `Api.accessToken` is a GETTER in 1.0.0 — assigning it throws "which has only a getter". A token
 * is carried by a NEW Api now, which is `createApi`'s second argument. Everything below therefore
 * runs on the authenticated `api`, not the anonymous one Quick Connect was performed on. */
api = sdk.createApi(base, login.AccessToken);
assert.equal((await getUserApi(api).getCurrentUser()).data.Id,login.User.Id);
const views = (await getUserViewApi(api).getUserViews({userId:login.User.Id})).data.Items;
assert.equal(views.length,1);
const items = (await getLibraryApi(api).getItems({userId:login.User.Id,parentId:views[0].Id})).data.Items;
assert(items.length>0);
const artwork=await fetch(api.getUri('Items/'+items[0].Id+'/Images/Primary',{api_key:api.accessToken}));
assert.equal(artwork.status,200);
assert.equal(artwork.headers.get('content-type'),'image/jpeg');
const info = (await getMediaInfoApi(api).getPostedPlaybackInfo({itemId:items[0].Id,userId:login.User.Id,
 maxStreamingBitrate:1600000,playbackInfoDto:{IsPlayback:true,EnableTranscoding:true}})).data;
const stream = api.getUri(info.MediaSources[0].TranscodingUrl);
assert(new URL(stream).pathname.startsWith('/jellyfin/Videos/'));
const playlist = await fetch(stream);
assert.equal(playlist.status,200);
assert.equal(playlist.headers.get('cache-control'),'private, no-store');
await promisify(execFile)('ffmpeg',['-v','error','-i',stream,'-t','2','-f','null','-'],{timeout:45000,encoding:'utf8'});
await getSessionApi(api).reportPlaybackStopped({playbackStopInfo:{ItemId:items[0].Id,PlaySessionId:info.PlaySessionId}});
assert.equal((await fetch(stream)).status,404);
console.log('PASS: official Jellyfin SDK discovery, Quick Connect, browsing, playback URL, real FFmpeg decode, stop');
