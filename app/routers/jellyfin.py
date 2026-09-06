"""Jellyfin 10.x playback API adapter over Media Center, not a Jellyfin server.

Quick Connect tokens are local encrypted Media Center documents. They never grant
Posterchan account/admin access. Every request rechecks the account permission;
all content requests traverse the same NAS proxy and library ACL as the web UI.
"""
import asyncio
from collections import OrderedDict
import hashlib
import hmac
import json
import re
import secrets
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, Body, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import AliasChoices, BaseModel, Field
from starlette.requests import Request as InternalRequest

from app.auth import SECRET_KEY
from app.database import get_db
from app.models import User
from app.routers import media_center as native
from app.services import media_center as media
from app.services.nostr.nostr_service import npub_of

class ClientCORS:
    """Bundled Jellyfin web clients use their own origins and media-only tokens.

    Keep this separate from Posterchan's credentialed Nostr approval endpoints.
    """
    def __init__(self, app):
        from starlette.middleware.cors import CORSMiddleware
        self.app = app
        # Jellyfin's ASP.NET routes accept casing used by different clients.
        # Canonicalize only known API routes, preserving opaque parameter values.
        self.paths = []
        for route in router.routes:
            parts = re.split(r'(\{[^}]+\})', route.path)
            pattern = ''.join('([^/]+)' if part.startswith('{') else re.escape(part) for part in parts)
            self.paths.append((re.compile('^' + pattern + '$', re.IGNORECASE), parts))
        self.cors = CORSMiddleware(app, allow_origins=['*'], allow_credentials=False,
                                   allow_methods=['GET', 'HEAD', 'POST', 'OPTIONS'], allow_headers=['*'],
                                   expose_headers=['Content-Length', 'Content-Type'])

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        is_api = path.lower() == '/jellyfin' or path.lower().startswith('/jellyfin/')
        if is_api:
            for pattern, parts in self.paths:
                match = pattern.fullmatch(path)
                if match:
                    values = iter(match.groups())
                    canonical = ''.join(next(values) if part.startswith('{') else part for part in parts)
                    scope = {**scope, 'path': canonical, 'raw_path': canonical.encode()}
                    break
        target = self.cors if scope.get('type') == 'http' and is_api else self.app
        await target(scope, receive, send)


router = APIRouter(prefix='/jellyfin', tags=['jellyfin'], route_class=native.PrivateRoute)
account_router = APIRouter(prefix='/api/media-center/jellyfin-account', tags=['media-center'],
                           route_class=native.PrivateRoute)
_account_lock = asyncio.Lock()
_quick = OrderedDict()
_approvals = OrderedDict()
_locators = OrderedDict()
_plays = OrderedDict()
_socket_count = 0
TOKEN_AGE = 90 * 86400
SERVER_ID = hmac.new(SECRET_KEY.encode(), b'jellyfin-server-id', hashlib.sha256).hexdigest()[:32]


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def account_id(user):
    return digest('jellyfin-user:' + media.identity(user))[:32]


def account_key(uid):
    return 'jellyfin-account:' + uid


def find_user(db, pubkey):
    return db.query(User).filter(User.nostr_npub.in_([pubkey, npub_of(pubkey)])).first()


def query(request, key, default=None):
    return next((v for k, v in request.query_params.items() if k.lower() == key.lower()), default)


def api_token(request):
    token = request.headers.get('X-Emby-Token') or query(request, 'api_key')
    if not token:
        header = request.headers.get('X-Emby-Authorization') or request.headers.get('Authorization', '')
        match = re.search(r'(?:^|[, ])Token\s*=\s*"([^"\r\n]+)"', header, re.I)
        token = match.group(1) if match else ''
    return token if len(token or '') <= 256 else ''


async def authenticate(request: Request, db=Depends(get_db)):
    token = api_token(request)
    uid = token.split('.', 1)[0]
    if not re.fullmatch('[a-f0-9]{32}', uid):
        raise HTTPException(401, 'Jellyfin app login required')
    record = await media.read(account_key(uid)) or {}
    session = next((s for s in record.get('sessions', []) if
                    s['expires'] > time.time() and hmac.compare_digest(s['hash'], digest(token))), None)
    user = find_user(db, record['pubkey']) if session else None
    if not native.media_allowed(user):
        raise HTTPException(401, 'Jellyfin login expired, disabled, or Media Center access revoked')
    requested_user = request.path_params.get('user_id') or query(request, 'UserId')
    if requested_user and requested_user.replace('-', '').lower() != uid:
        raise HTTPException(403, 'Only your own account is accessible')
    return SimpleNamespace(user=user, uid=uid, token=token, session=session)


@account_router.get('')
async def account_status(user=Depends(native.get_media_user)):
    record = await media.read(account_key(account_id(user))) or {}
    return {'enabled': True, 'username': user.username,
            'server_path': '/jellyfin', 'sessions': sum(s['expires'] > time.time() for s in record.get('sessions', []))}


@account_router.delete('', status_code=204)
async def account_disable(user=Depends(native.get_media_user)):
    async with _account_lock:
        await media.write(account_key(account_id(user)), {})
        for key, entry in list(_quick.items()):
            if entry.get('pubkey') == media.identity(user):
                _quick.pop(key, None)
    return Response(status_code=204)


def session_dto(session, user):
    now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    return {'Id': session['id'], 'UserId': account_id(user), 'UserName': user.username,
            'ServerId': SERVER_ID, 'SupportsMediaControl': False, 'SupportsRemoteControl': False,
            'PlayableMediaTypes': ['Video', 'Audio'], 'SupportedCommands': [],
            'LastActivityDate': now, 'LastPlaybackCheckIn': now, 'IsActive': True,
            'HasCustomDeviceName': False}


def user_dto(user):
    return {'Id': account_id(user), 'Name': user.username, 'ServerId': SERVER_ID,
            'HasPassword': False, 'HasConfiguredPassword': False, 'EnableAutoLogin': False,
            'Configuration': {'PlayDefaultAudioTrack': True, 'SubtitleMode': 'None',
                              'EnableNextEpisodeAutoPlay': True, 'OrderedViews': [], 'MyMediaExcludes': [],
                              'DisplayMissingEpisodes': False, 'GroupedFolders': [], 'DisplayCollectionsView': False,
                              'EnableLocalPassword': False, 'LatestItemsExcludes': [], 'HidePlayedInLatest': False,
                              'RememberAudioSelections': True, 'RememberSubtitleSelections': True},
            'Policy': {'IsAdministrator': False, 'IsHidden': True, 'IsDisabled': False,
                       'EnableMediaPlayback': True, 'EnableAudioPlaybackTranscoding': True,
                       'EnableVideoPlaybackTranscoding': True, 'EnablePlaybackRemuxing': False,
                       'EnableContentDownloading': False, 'EnableContentDeletion': False,
                       'EnableRemoteAccess': True, 'EnableAllDevices': True, 'EnableAllFolders': False,
                       'EnableLiveTvAccess': False, 'EnableUserPreferenceAccess': False,
                       'EnableRemoteControlOfOtherUsers': False, 'EnableSharedDeviceControl': False,
                       'AuthenticationProviderId': 'Posterchan.MediaCenter',
                       'PasswordResetProviderId': 'Posterchan.MediaCenter',
                       'EnableLiveTvManagement': False, 'ForceRemoteSourceTranscoding': True,
                       'EnableSyncTranscoding': False, 'EnableMediaConversion': False,
                       'EnableAllChannels': False, 'InvalidLoginAttemptCount': 0,
                       'LoginAttemptsBeforeLockout': 0, 'MaxActiveSessions': 0,
                       'EnablePublicSharing': False, 'RemoteClientBitrateLimit': 0,
                       'SyncPlayAccess': 'None'}}


@router.api_route('', methods=['GET', 'HEAD'])
@router.api_route('/', methods=['GET', 'HEAD'])
@router.api_route('/System/Info/Public', methods=['GET', 'HEAD'])
async def public_info(request: Request):
    scheme = request.headers.get('x-forwarded-proto', '').split(',')[0].strip().lower()
    base = request.base_url.replace(scheme=scheme) if scheme in ('http', 'https') else request.base_url
    return {'Id': SERVER_ID, 'ServerName': 'Posterchan Media Center',
            # Official SDK discovery requires this literal protocol marker.
            'ProductName': 'Jellyfin Server', 'Version': '10.11.11',
            'LocalAddress': str(base).rstrip('/') + '/jellyfin',
            'StartupWizardCompleted': True}


@router.get('/System/Info')
async def system_info(request: Request, auth=Depends(authenticate)):
    return await public_info(request)


@router.get('/System/Configuration/encoding')
async def encoding_configuration(auth=Depends(authenticate)):
    # Client capability hints only. Native Media Center owns encoder selection,
    # filesystem paths and enforced limits; app tokens cannot change settings.
    return {'EncodingThreadCount': 1, 'EnableFallbackFont': False, 'EnableAudioVbr': False,
            'DownMixAudioBoost': 1.0, 'DownMixStereoAlgorithm': 'None', 'MaxMuxingQueueSize': 2048,
            'EnableThrottling': True, 'ThrottleDelaySeconds': 0, 'EnableSegmentDeletion': True,
            'SegmentKeepSeconds': 6, 'HardwareAccelerationType': 'none',
            'EnableTonemapping': False, 'EnableVppTonemapping': False,
            'EnableVideoToolboxTonemapping': False, 'TonemappingAlgorithm': 'none',
            'TonemappingMode': 'auto', 'TonemappingRange': 'auto', 'TonemappingDesat': 0.0,
            'TonemappingPeak': 0.0, 'TonemappingParam': 0.0, 'VppTonemappingBrightness': 0.0,
            'VppTonemappingContrast': 1.0, 'H264Crf': 23, 'H265Crf': 28, 'EncoderPreset': 'auto',
            'DeinterlaceDoubleRate': False, 'DeinterlaceMethod': 'yadif',
            'EnableDecodingColorDepth10Hevc': False, 'EnableDecodingColorDepth10Vp9': False,
            'EnableDecodingColorDepth10HevcRext': False, 'EnableDecodingColorDepth12HevcRext': False,
            'EnableEnhancedNvdecDecoder': False, 'PreferSystemNativeHwDecoder': False,
            'EnableIntelLowPowerH264HwEncoder': False, 'EnableIntelLowPowerHevcHwEncoder': False,
            'EnableHardwareEncoding': False, 'AllowHevcEncoding': False, 'AllowAv1Encoding': False,
            'EnableSubtitleExtraction': True, 'SubtitleExtractionTimeoutMinutes': 2}


@router.api_route('/System/Ping', methods=['GET', 'POST'])
async def ping():
    return Response('Jellyfin Server', media_type='text/plain')


@router.get('/QuickConnect/Enabled')
async def quick_connect_enabled():
    return True


@router.get('/Users/Public')
async def public_users():
    return []  # Never enumerate Nostr identities to anonymous clients.


@router.get('/Branding/Configuration')
async def branding():
    return {'LoginDisclaimer': 'Use Quick Connect and approve the code in Posterchan Media Center.', 'CustomCss': ''}


def expire_quick():
    for key, entry in list(_quick.items()):
        if time.monotonic() - entry['created'] > 300:
            _quick.pop(key, None)


def quick_dto(entry, secret):
    return {'Secret': secret, 'Code': entry['code'], 'Authenticated': bool(entry.get('pubkey')),
            'DateAdded': entry['date']}


@router.post('/QuickConnect/Initiate')
async def initiate_quick():
    from datetime import datetime, timezone
    async with _account_lock:
        expire_quick()
        if len(_quick) >= 128:
            raise HTTPException(429, 'Too many pending Quick Connect requests; try again shortly')
        secret = secrets.token_urlsafe(32)
        used = {entry['code'] for entry in _quick.values()}
        code = f'{secrets.randbelow(1000000):06d}'
        while code in used:
            code = f'{secrets.randbelow(1000000):06d}'
        entry = {'code': code, 'created': time.monotonic(),
                 'date': datetime.now(timezone.utc).isoformat()}
        _quick[digest(secret)] = entry
    return quick_dto(entry, secret)


@router.get('/QuickConnect/Connect')
async def poll_quick(request: Request):
    expire_quick()
    secret = query(request, 'Secret', '')
    entry = _quick.get(digest(secret)) if len(secret) <= 128 else None
    if not entry:
        raise HTTPException(404, 'Quick Connect request expired')
    return quick_dto(entry, secret)


class QuickApproval(BaseModel):
    code: str = Field(pattern=r'^[0-9]{6}$')


async def approve_quick(code, user):
    if not re.fullmatch(r'[0-9]{6}', code):
        raise HTTPException(400, 'Enter the six-digit code shown in your app')
    async with _account_lock:
        expire_quick()
        uid = account_id(user)
        now = time.monotonic()
        attempts = [t for t in _approvals.get(uid, []) if now - t < 60]
        if len(attempts) >= 10:
            raise HTTPException(429, 'Too many Quick Connect approvals; wait one minute')
        _approvals[uid] = attempts + [now]
        _approvals.move_to_end(uid)
        while len(_approvals) > 1024:
            _approvals.popitem(last=False)
        entry = next((e for e in _quick.values() if hmac.compare_digest(e['code'], code)), None)
        if not entry or entry.get('pubkey'):
            raise HTTPException(404, 'Code expired or already approved')
        entry['pubkey'] = media.identity(user)
    return True


@account_router.post('/authorize')
async def approve_from_posterchan(body: QuickApproval, user=Depends(native.get_media_user)):
    return {'ok': await approve_quick(body.code, user)}


@router.post('/QuickConnect/Authorize')
async def approve_from_jellyfin(request: Request, auth=Depends(authenticate)):
    return await approve_quick(query(request, 'Code', ''), auth.user)


class QuickSecret(BaseModel):
    Secret: str = Field(min_length=20, max_length=128, validation_alias=AliasChoices('Secret', 'secret'))


def body_value(body, key, default=None):
    """ASP.NET-style JSON field matching for native clients using camelCase."""
    value = next((value for name, value in body.items() if name.casefold() == key.casefold()), None)
    return default if value is None else value


@router.post('/Users/AuthenticateWithQuickConnect')
async def redeem_quick(body: QuickSecret, db=Depends(get_db)):
    async with _account_lock:
        expire_quick()
        key = digest(body.Secret)
        entry = _quick.get(key)
        user = find_user(db, entry['pubkey']) if entry and entry.get('pubkey') else None
        if not native.media_allowed(user):
            raise HTTPException(401, 'Quick Connect is not approved or Media Center access was revoked')
        uid = account_id(user)
        record = await media.read(account_key(uid)) or {'pubkey': media.identity(user)}
        token = uid + '.' + secrets.token_urlsafe(32)
        session = {'id': secrets.token_hex(16), 'hash': digest(token), 'expires': int(time.time()) + TOKEN_AGE}
        record['pubkey'] = media.identity(user)
        record['sessions'] = [s for s in record.get('sessions', []) if s['expires'] > time.time()][-15:] + [session]
        await media.write(account_key(uid), record)
        _quick.pop(key, None)  # Consume only after the encrypted token record is acknowledged.
    return {'User': user_dto(user), 'AccessToken': token, 'ServerId': SERVER_ID,
            'SessionInfo': session_dto(session, user)}


@router.post('/Sessions/Logout', status_code=204)
async def logout(auth=Depends(authenticate)):
    async with _account_lock:
        record = await media.read(account_key(auth.uid)) or {}
        record['sessions'] = [s for s in record.get('sessions', []) if s['hash'] != digest(auth.token)]
        await media.write(account_key(auth.uid), record)
    return Response(status_code=204)


@router.get('/Users/{user_id}')
@router.get('/Users/Me')
async def me(auth=Depends(authenticate)):
    return user_dto(auth.user)


async def media_call(request, auth, db, path='', method='GET', body=None):
    """Reuse native proxy/auth/ACL/limits without a second HTTP listener or self-HTTP hop."""
    content = json.dumps(body).encode() if body is not None else b''
    parsed = urlsplit('/api/media-center' + path)
    scope = {**request.scope, 'method': method, 'path': parsed.path, 'raw_path': parsed.path.encode(),
             'query_string': parsed.query.encode(), 'headers': [(b'content-type', b'application/json')]}
    async def receive():
        return {'type': 'http.request', 'body': content, 'more_body': False}
    try:
        await native.proxy_request(InternalRequest(scope, receive), auth.user, db)
    except native.ProxiedResponse as proxied:
        response = proxied.response
        if response.status_code >= 400 or 'application/json' in response.headers.get('content-type', ''):
            data = b''.join([chunk async for chunk in response.body_iterator])
            if response.status_code >= 400:
                raise HTTPException(response.status_code, 'Media Center request failed')
            return json.loads(data)
        return response
    parts = parsed.path.removeprefix('/api/media-center').strip('/').split('/')
    if path == '':
        return await native.list_libraries(auth.user)
    if parts == ['sessions', 'stop']:
        return await native.stop_session(native.StopSession(**body), auth.user)
    if len(parts) == 2 and parts[1] == 'items':
        return await native.items(parts[0], auth.user)
    if len(parts) == 3 and parts[1] == 'art':
        return await native.artwork(parts[0], parts[2], auth.user)
    if len(parts) == 3 and parts[1] == 'tracks':
        return await native.tracks(parts[0], parts[2], auth.user)
    if len(parts) == 3 and parts[1] == 'play':
        return await native.playback(parts[0], parts[2], auth.user)
    if len(parts) == 4 and parts[1] == 'hls':
        params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        return await native.hls(parts[0], parts[2], parts[3], params['viewer'], int(params['expires']),
                                params['ticket'], audio=int(params.get('audio', -1)),
                                subtitle=int(params.get('subtitle', -1)), user=auth.user, db=db)
    raise HTTPException(404, 'Unsupported Media Center operation')


def library_id(lib):
    return digest('jellyfin-library:' + lib['id'])[:32]


def item_id(lib, item):
    return digest('jellyfin-item:' + lib['id'] + ':' + item['id'])[:32]


def remember(lib, item):
    key = item_id(lib, item)
    _locators[key] = (lib['id'], item['id'])
    _locators.move_to_end(key)
    while len(_locators) > 8192:
        _locators.popitem(last=False)
    return key


def library_dto(lib):
    return {'Id': library_id(lib), 'ServerId': SERVER_ID, 'Name': lib['name'], 'Type': 'CollectionFolder',
            'IsFolder': True, 'CollectionType': 'homevideos', 'ChildCount': lib.get('count', 0),
            'RecursiveItemCount': lib.get('count', 0), 'ImageTags': {}, 'LocationType': 'FileSystem'}


def item_dto(lib, item):
    uid = remember(lib, item)
    folder = item.get('folder', '.')
    name = item['name'] if folder == '.' else folder + ' / ' + item['name']
    video = item.get('video', True)
    return {'Id': uid, 'ServerId': SERVER_ID, 'Name': name, 'SortName': name,
            'ParentId': library_id(lib), 'Type': 'Video' if video else 'Audio',
            'MediaType': 'Video' if video else 'Audio', 'IsFolder': False,
            'RunTimeTicks': int(item['duration'] * 10000000), 'CanDownload': False, 'CanDelete': False,
            'PlayAccess': 'Full', 'LocationType': 'FileSystem', 'VideoType': 'VideoFile',
            'ImageTags': {'Primary': digest(str(item))[:32]}, 'MediaSources': [],
            'UserData': {'PlaybackPositionTicks': 0, 'PlayCount': 0, 'IsFavorite': False, 'Played': False,
                         'Key': uid, 'ItemId': uid}}


async def libraries(request, auth, db):
    return (await media_call(request, auth, db))['libraries']


async def resolve(request, auth, db, uid):
    uid = uid.replace('-', '').lower()
    libs = await libraries(request, auth, db)  # Always read live ACL, including cached locators.
    for lib in libs:
        if library_id(lib) == uid:
            return lib, None
    locator = _locators.get(uid)
    for lib in libs:
        if locator and lib['id'] != locator[0]:
            continue
        for item in (await media_call(request, auth, db, '/' + lib['id'] + '/items'))['items']:
            if item_id(lib, item) == uid:
                remember(lib, item)
                return lib, item
    raise HTTPException(404, 'Media not found')


def envelope(items, start=0, total=None):
    return {'Items': items, 'TotalRecordCount': len(items) if total is None else total, 'StartIndex': start}


@router.get('/Users/{user_id}/Views')
@router.get('/UserViews')
async def views(request: Request, auth=Depends(authenticate), db=Depends(get_db)):
    return envelope([library_dto(lib) for lib in await libraries(request, auth, db)])


@router.get('/Users/{user_id}/Items/Root')
async def root_item(auth=Depends(authenticate)):
    return {'Id': SERVER_ID, 'Name': 'Media Center', 'Type': 'Folder', 'IsFolder': True}


@router.get('/Items')
@router.get('/Users/{user_id}/Items')
@router.get('/Users/{user_id}/Items/Latest')
@router.get('/Items/Latest')
async def browse(request: Request, auth=Depends(authenticate), db=Depends(get_db)):
    parent = query(request, 'ParentId', '').replace('-', '').lower()
    recursive = query(request, 'Recursive', 'false').lower() == 'true'
    latest = request.url.path.endswith('/Latest')
    search = query(request, 'SearchTerm', '').casefold()
    ids = set(query(request, 'Ids', '').replace('-', '').lower().split(',')) - {''}
    types = set(query(request, 'IncludeItemTypes', '').split(',')) - {''}
    try:
        start = max(0, int(query(request, 'StartIndex', '0')))
        limit = min(500, max(0, int(query(request, 'Limit', '100'))))
    except ValueError:
        raise HTTPException(400, 'Invalid pagination')
    result = []
    for lib in await libraries(request, auth, db):
        if parent and parent not in (SERVER_ID, library_id(lib)):
            continue
        if not parent and not recursive and not latest and not search and not ids:
            result.append(library_dto(lib))
            continue
        for item in (await media_call(request, auth, db, '/' + lib['id'] + '/items'))['items']:
            dto = item_dto(lib, item)
            if (not ids or dto['Id'] in ids) and (not search or search in dto['Name'].casefold()):
                result.append(dto)
    if types:
        result = [item for item in result if item['Type'] in types]
    # Native catalogs already preserve natural folder/filename order.
    page = result[start:start + limit]
    return page if latest else envelope(page, start, len(result))


@router.get('/Items/Resume')
@router.get('/Users/{user_id}/Items/Resume')
async def resume(auth=Depends(authenticate)):
    return envelope([])  # Watch history is not implemented by Media Center yet.


@router.get('/Items/{uid}')
@router.get('/Users/{user_id}/Items/{uid}')
async def item_details(uid: str, request: Request, auth=Depends(authenticate), db=Depends(get_db)):
    lib, item = await resolve(request, auth, db, uid)
    return item_dto(lib, item) if item else library_dto(lib)


@router.get('/Items/{uid}/Images/Primary')
@router.get('/Items/{uid}/Images/Primary/{index}')
async def image(uid: str, request: Request, auth=Depends(authenticate), db=Depends(get_db)):
    lib, item = await resolve(request, auth, db, uid)
    if not item:
        raise HTTPException(404, 'No library artwork')
    return await media_call(request, auth, db, f"/{lib['id']}/art/{item['id']}")


def play_record(auth, play_id, uid=None):
    record = _plays.get(play_id)
    if (not record or time.monotonic() - record['seen'] > 900 or record['token'] != digest(auth.token)
            or (uid and record['item'] != uid)):
        raise HTTPException(404, 'Playback session expired; reopen the item')
    record['seen'] = time.monotonic()
    _plays.move_to_end(play_id)
    return record


@router.api_route('/Items/{uid}/PlaybackInfo', methods=['GET', 'POST'])
async def playback_info(uid: str, request: Request, body: dict = Body(default={}),
                        auth=Depends(authenticate), db=Depends(get_db)):
    lib, item = await resolve(request, auth, db, uid)
    if not item:
        raise HTTPException(400, 'Select a playable item')
    listing = await media_call(request, auth, db)
    try:
        cap = int(body_value(body, 'MaxStreamingBitrate') or query(request, 'MaxStreamingBitrate', '1000000000'))
    except (ValueError, TypeError):
        raise HTTPException(400, 'Invalid streaming bitrate')
    profiles = [p for p in listing['profiles'] if sum(media.PROFILES[p][2:]) * 1200 <= cap]
    if not profiles:
        return {'MediaSources': [], 'ErrorCode': 'NoCompatibleStream'}
    track_data = await media_call(request, auth, db, f"/{lib['id']}/tracks/{item['id']}")
    tracks = track_data['tracks']
    try:
        audio_index = int(body_value(body, 'AudioStreamIndex', query(request, 'AudioStreamIndex', '-1')))
        subtitle_index = int(body_value(body, 'SubtitleStreamIndex', query(request, 'SubtitleStreamIndex', '-1')))
    except (ValueError, TypeError):
        raise HTTPException(400, 'Invalid media track')
    if audio_index >= 0 and not any(t['type'] == 'audio' and t['index'] == audio_index for t in tracks):
        raise HTTPException(400, 'Unavailable audio track')
    selected_subtitle = next((t for t in tracks if t['type'] == 'subtitle' and t['index'] == subtitle_index), None)
    if subtitle_index >= 0 and not selected_subtitle:
        raise HTTPException(400, 'Unavailable subtitle track')
    playback = await media_call(request, auth, db, f"/{lib['id']}/play/{item['id']}", 'POST')
    playback['url'] += '&' + urlencode({'audio': audio_index,
                                        'subtitle': subtitle_index if selected_subtitle and not selected_subtitle['text'] else -1})
    play_id = secrets.token_hex(16)
    _plays[play_id] = {'token': digest(auth.token), 'item': uid, 'url': playback['url'],
                       'seen': time.monotonic(), 'profiles': profiles,
                       'audio_indices': [t['index'] for t in tracks if t['type'] == 'audio'],
                       'subtitle_indices': [t['index'] for t in tracks if t['type'] == 'subtitle'],
                       'text_indices': [t['index'] for t in tracks if t['type'] == 'subtitle' and t['text']]}
    while len(_plays) > 256:
        _plays.popitem(last=False)
    video = item.get('video', True)
    params = urlencode({'api_key': auth.token, 'PlaySessionId': play_id})
    streams = ([{'Type': 'Video', 'Codec': 'h264', 'Index': 0, 'IsDefault': True}] if video else [])
    for track in tracks:
        stream = {'Type': track['type'].title(), 'Codec': 'aac' if track['type'] == 'audio' else track['codec'],
                  'Index': track['index'], 'Language': track['language'], 'Title': track['title'],
                  'DisplayTitle': ' · '.join(filter(None, [track['language'], track['title'], track['codec']])),
                  'IsDefault': track['default'], 'IsExternal': False}
        if track['type'] == 'audio':
            stream.update(Channels=2, SampleRate=48000)
        else:
            stream.update(IsTextSubtitleStream=track['text'], SupportsExternalStream=track['text'],
                          DeliveryMethod='External' if track['text'] else 'Encode')
            if track['text']:
                stream['Codec'] = 'webvtt'
                stream['DeliveryUrl'] = f"/jellyfin/Videos/{uid}/{uid}/Subtitles/{track['index']}/Stream.vtt?{params}"
        streams.append(stream)
    for stream in streams:
        for key in ('IsInterlaced', 'IsForced', 'IsHearingImpaired', 'IsOriginal',
                    'IsExternal', 'IsTextSubtitleStream', 'SupportsExternalStream'):
            stream.setdefault(key, False)
    default_audio = next((t['index'] for t in tracks if t['type'] == 'audio' and t['default']),
                         next((t['index'] for t in tracks if t['type'] == 'audio'), -1))
    source = {'Id': uid, 'Name': item['name'], 'Protocol': 'Http', 'Container': 'ts', 'Type': 'Default',
              'RunTimeTicks': int(item['duration'] * 10000000), 'MediaStreams': streams,
              'SupportsDirectPlay': False, 'SupportsDirectStream': False, 'SupportsTranscoding': True,
              'TranscodingUrl': f'Videos/{uid}/master.m3u8?{params}', 'TranscodingSubProtocol': 'hls',
              'TranscodingContainer': 'ts', 'DefaultAudioStreamIndex': audio_index if audio_index >= 0 else default_audio,
              'DefaultSubtitleStreamIndex': subtitle_index, 'RequiresOpening': False, 'RequiresClosing': False}
    source.update(IsRemote=True, ReadAtNativeFramerate=False, IgnoreDts=False, IgnoreIndex=False,
                  GenPtsInput=False, IsInfiniteStream=False, RequiresLooping=False,
                  SupportsProbing=False, HasSegments=True)
    return {'MediaSources': [source], 'PlaySessionId': play_id}


@router.get('/Videos/{uid}/{asset}')
async def hls(uid: str, asset: str, request: Request, auth=Depends(authenticate), db=Depends(get_db)):
    play_id = query(request, 'PlaySessionId', '')
    record = play_record(auth, play_id, uid)
    profile = asset.split('-', 1)[0].removesuffix('.m3u8')
    if asset != 'master.m3u8' and profile not in record['profiles']:
        raise HTTPException(404, 'Unavailable streaming profile')
    original = urlsplit(record['url'])
    native_params = {key: values[0] for key, values in parse_qs(original.query).items()}
    selections = {}
    for field, parameter, allowed in [('AudioStreamIndex', 'audio', 'audio_indices'), ('SubtitleStreamIndex', 'subtitle', 'subtitle_indices')]:
        value = query(request, field, None)
        if value is not None:
            try:
                index = int(value)
            except (ValueError, TypeError):
                raise HTTPException(400, 'Invalid media track')
            if index != -1 and index not in record.get(allowed, []):
                raise HTTPException(400, 'Unavailable media track')
            selections[field] = index
            native_params[parameter] = -1 if parameter == 'subtitle' and index in record.get('text_indices', []) else index
    path = original.path.removeprefix('/api/media-center').rsplit('/', 1)[0] + '/' + asset + '?' + urlencode(native_params)
    response = await media_call(request, auth, db, path)
    if not asset.endswith('.m3u8'):
        return response
    data = response.body if hasattr(response, 'body') else b''.join([part async for part in response.body_iterator])
    lines = []
    params = urlencode({'api_key': auth.token, 'PlaySessionId': play_id, **selections})
    for line in data.decode().splitlines():
        if line.startswith('#EXT-X-STREAM-INF:'):
            lines.append(line)
        elif line and not line.startswith('#'):
            target = line.split('?', 1)[0]
            if asset == 'master.m3u8' and target.removesuffix('.m3u8') not in record['profiles']:
                lines.pop()  # Remove the paired stream-info line too.
                continue
            lines.append(target + '?' + params)
        else:
            lines.append(line)
    return Response('\n'.join(lines) + '\n', media_type='application/vnd.apple.mpegurl')


@router.get('/Videos/{uid}/{source_id}/Subtitles/{index}/Stream.vtt')
async def subtitles(uid: str, source_id: str, index: int, request: Request,
                    auth=Depends(authenticate), db=Depends(get_db)):
    if source_id != uid:
        raise HTTPException(404, 'Media source not found')
    record = play_record(auth, query(request, 'PlaySessionId', ''), uid)
    original = urlsplit(record['url'])
    path = original.path.removeprefix('/api/media-center').rsplit('/', 1)[0] + f'/subtitle-{index}.vtt?' + original.query
    return await media_call(request, auth, db, path)


@router.post('/Sessions/Playing', status_code=204)
@router.post('/Sessions/Playing/Progress', status_code=204)
async def progress(body: dict = Body(default={}), auth=Depends(authenticate)):
    play_record(auth, body_value(body, 'PlaySessionId', ''))
    return Response(status_code=204)


@router.post('/Sessions/Playing/Stopped', status_code=204)
async def stopped(request: Request, body: dict = Body(default={}), auth=Depends(authenticate), db=Depends(get_db)):
    play_id = body_value(body, 'PlaySessionId', '')
    record = play_record(auth, play_id)
    ticket = parse_qs(urlsplit(record['url']).query)['ticket'][0]
    await media_call(request, auth, db, '/sessions/stop', 'POST', {'ticket': ticket})
    _plays.pop(play_id, None)
    return Response(status_code=204)


@router.post('/Sessions/Capabilities/Full', status_code=204)
@router.post('/Sessions/Capabilities', status_code=204)
async def capabilities(auth=Depends(authenticate)):
    return Response(status_code=204)


@router.get('/Sessions')
async def sessions(auth=Depends(authenticate)):
    return [session_dto(auth.session, auth.user)]


@router.get('/DisplayPreferences/{preference_id}')
async def display_preferences(preference_id: str, request: Request, auth=Depends(authenticate)):
    return {'Id': preference_id, 'ViewType': 'Poster', 'SortBy': 'SortName', 'SortOrder': 'Ascending',
            'IndexBy': 'None', 'RememberIndexing': False, 'RememberSorting': False, 'CustomPrefs': {},
            'PrimaryImageHeight': 250, 'PrimaryImageWidth': 250, 'ScrollDirection': 'Horizontal',
            'ShowBackdrop': True, 'ShowSidebar': False, 'Client': query(request, 'client', 'emby')}


@router.get('/Items/{uid}/Similar')
@router.get('/Items/{uid}/LocalTrailers')
@router.get('/Shows/NextUp')
async def empty_related(request: Request, auth=Depends(authenticate)):
    return [] if request.url.path.endswith('/LocalTrailers') else envelope([])


@router.websocket('/socket')
async def client_socket(websocket: WebSocket, db=Depends(get_db)):
    global _socket_count
    try:
        await authenticate(websocket, db)
    except HTTPException:
        await websocket.close(code=1008)
        return
    if _socket_count >= 256:
        await websocket.close(code=1013)
        return
    _socket_count += 1
    try:
        db.rollback()  # Never hold a database connection for an idle client socket.
        await websocket.accept()
        await websocket.send_json({'MessageType': 'ForceKeepAlive', 'Data': 30})
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive_text(), timeout=60)
                if len(message) > 4096:
                    await websocket.close(code=1009)
                    return
            except asyncio.TimeoutError:
                await websocket.close(code=1000)
                return
            try:
                db.expire_all()
                await authenticate(websocket, db)
                db.rollback()
            except HTTPException:
                await websocket.close(code=1008)
                return
            await websocket.send_json({'MessageType': 'KeepAlive'})
    except WebSocketDisconnect:
        pass
    finally:
        _socket_count -= 1
