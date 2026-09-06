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
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
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
                                   allow_methods=['GET', 'HEAD', 'POST', 'DELETE', 'OPTIONS'], allow_headers=['*'],
                                   expose_headers=['Content-Length', 'Content-Type'])

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        is_api = path.lower() == '/jellyfin' or path.lower().startswith('/jellyfin/')
        if is_api:
            for pattern, parts in self.paths:
                match = pattern.fullmatch(path) or pattern.fullmatch(path.rstrip('/'))
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


def with_image_ticket(dto, auth):
    # TV image loaders omit app auth but preserve ImageTags in the tag query.
    # This capability grants only this item's artwork, not API or video access.
    expires = min(int(auth.session['expires']), (int(time.time()) // 3600 + 2) * 3600)
    payload = f"{auth.uid}.{auth.session['id']}.{expires}"
    signature = hmac.new(SECRET_KEY.encode(), (dto['Id'] + '.Primary.' + payload + '.' + auth.session['hash']).encode(), hashlib.sha256).hexdigest()
    dto['ImageTags'] = {'Primary': 'pcimg.' + payload + '.' + signature}
    return dto


async def authenticate_image(request: Request, db=Depends(get_db)):
    if api_token(request):
        return await authenticate(request, db)
    tag = query(request, 'tag', '')
    match = re.fullmatch(r'pcimg\.([a-f0-9]{32})\.([a-f0-9]{32})\.([0-9]{10})\.([a-f0-9]{64})', tag)
    if not match:
        raise HTTPException(401, 'Private artwork requires authorization')
    uid, sid, expires, signature = match.groups()
    record = await media.read(account_key(uid)) or {}
    session = next((s for s in record.get('sessions', []) if s['id'] == sid and s['expires'] > time.time()), None)
    item = request.path_params['uid'].replace('-', '').lower()
    payload = f"{uid}.{sid}.{expires}"
    expected = hmac.new(SECRET_KEY.encode(), (item + '.Primary.' + payload + '.' + session['hash']).encode(), hashlib.sha256).hexdigest() if session else ''
    user = find_user(db, record['pubkey']) if session else None
    if int(expires) <= time.time() or not hmac.compare_digest(signature, expected) or not native.media_allowed(user):
        raise HTTPException(401, 'Private artwork authorization expired or revoked')
    return SimpleNamespace(user=user, uid=uid, token='', session=session)


@account_router.get('')
async def account_status(user=Depends(native.get_media_user)):
    record = await media.read(account_key(account_id(user))) or {}
    devices = [{'id': session['id'], 'name': session.get('device', 'Jellyfin app'),
                'client': session.get('client', 'Jellyfin'), 'version': session.get('version', ''),
                'created': session.get('created'), 'expires': session['expires']}
               for session in record.get('sessions', []) if session['expires'] > time.time()]
    return {'enabled': True, 'username': user.username,
            'server_path': '/jellyfin', 'sessions': len(devices), 'devices': devices}


@account_router.delete('/devices/{device_id}', status_code=204)
async def revoke_device(device_id: str, user=Depends(native.get_media_user)):
    async with _account_lock:
        key = account_key(account_id(user))
        record = await media.read(key) or {}
        sessions = record.get('sessions', [])
        if not any(session['id'] == device_id for session in sessions):
            raise HTTPException(404, 'Connected device not found')
        record['sessions'] = [session for session in sessions if session['id'] != device_id]
        await media.write(key, record)
    return Response(status_code=204)


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
                       'EnableLiveTvAccess': False, 'EnableUserPreferenceAccess': True,
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
    if request.url.path.rstrip('/').lower() == '/jellyfin' and 'text/html' in request.headers.get('accept', '').lower():
        return FileResponse(Path(__file__).resolve().parents[2] / 'templates/media_center_app.html',
                            headers={'Vary': 'Accept'}, media_type='text/html')
    scheme = request.headers.get('x-forwarded-proto', '').split(',')[0].strip().lower()
    base = request.base_url.replace(scheme=scheme) if scheme in ('http', 'https') else request.base_url
    return {'Id': SERVER_ID, 'ServerName': 'Posterchan Media Center',
            # Official SDK discovery requires this literal protocol marker.
            'ProductName': 'Jellyfin Server', 'Version': '10.11.11',
            'LocalAddress': str(base).rstrip('/') + '/jellyfin',
            'StartupWizardCompleted': True}


@router.get('/main.posterchan.bundle.js')
async def android_host_bundle():
    # Android intercepts this filename to inject its native bridge and signal readiness,
    # then fetches our script again with ?deferred=true. Both requests use the same asset.
    return FileResponse(Path(__file__).resolve().parents[2] / 'static/js/media_center_app.js',
                        media_type='application/javascript')


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
async def initiate_quick(request: Request):
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
        header = request.headers.get('X-Emby-Authorization') or request.headers.get('Authorization', '')
        fields = {key.lower(): re.sub(r'[\x00-\x1f\x7f]', '', value)[:100]
                  for key, value in re.findall(r'(Client|Device|Version)\s*=\s*"([^"\r\n]*)"', header, re.I)}
        entry = {'client': fields.get('client', 'Jellyfin'), 'device': fields.get('device', 'Jellyfin app'),
                 'version': fields.get('version', ''), 'code': code, 'created': time.monotonic(),
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
        session = {'id': secrets.token_hex(16), 'hash': digest(token), 'expires': int(time.time()) + TOKEN_AGE,
                   'created': int(time.time()), 'device': entry.get('device', 'Jellyfin app'),
                   'client': entry.get('client', 'Jellyfin'), 'version': entry.get('version', '')}
        record['pubkey'] = media.identity(user)
        record['sessions'] = [s for s in record.get('sessions', []) if s['expires'] > time.time()][-15:] + [session]
        await media.write(account_key(uid), record)
        _quick.pop(key, None)  # Consume only after the encrypted token record is acknowledged.
    return {'User': await hydrated_user(user), 'AccessToken': token, 'ServerId': SERVER_ID,
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
    return await hydrated_user(auth.user)


async def hydrated_user(user):
    result = user_dto(user)
    saved = await media.read('jellyfin-preferences:' + account_id(user)) or {}
    result['Configuration'].update(saved.get('configuration', {}))
    return result


async def save_preferences(uid, key, value):
    # One bounded, encrypted local document per identity, independent of app tokens.
    async with _account_lock:
        storage = 'jellyfin-preferences:' + uid
        saved = await media.read(storage) or {}
        saved[key] = value
        if sum(k.startswith('display:') for k in saved) > 32 or len(json.dumps(saved).encode()) > 48000:
            raise HTTPException(400, 'Too many or oversized client preferences')
        await media.write(storage, saved)


@router.post('/Users/{user_id}/Configuration', status_code=204)
async def save_user_configuration(body: dict = Body(...), auth=Depends(authenticate)):
    defaults = user_dto(auth.user)['Configuration']
    defaults.update(AudioLanguagePreference='', SubtitleLanguagePreference='', SubtitleMode='None')
    allowed = {key.casefold(): key for key in defaults}
    update = {}
    for key, value in body.items():
        canonical = allowed.get(key.casefold())
        if canonical is None:
            continue
        expected = defaults[canonical]
        if type(value) is not type(expected):
            raise HTTPException(400, 'Invalid preference type')
        if isinstance(value, list) and (len(value) > 100 or any(not isinstance(v, str) or len(v) > 128 for v in value)):
            raise HTTPException(400, 'Invalid preference list')
        if isinstance(value, list):
            try:
                value = [UUID(v).hex for v in value]
            except ValueError:
                raise HTTPException(400, 'Preference lists must contain item UUIDs')
        if isinstance(value, str) and len(value) > 128:
            raise HTTPException(400, 'Preference is too long')
        if canonical == 'SubtitleMode' and value not in ('Default', 'Always', 'OnlyForced', 'None', 'Smart'):
            raise HTTPException(400, 'Invalid subtitle mode')
        update[canonical] = value
    # Merge while holding the same lock as concurrent device preference changes.
    async with _account_lock:
        storage = 'jellyfin-preferences:' + auth.uid
        saved = await media.read(storage) or {}
        saved['configuration'] = {**saved.get('configuration', {}), **update}
        if len(saved) > 33 or len(json.dumps(saved).encode()) > 48000:
            raise HTTPException(400, 'Preferences are too large')
        await media.write(storage, saved)
    return Response(status_code=204)


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
    if len(parts) == 2 and parts[1] == 'folder-art':
        return await native.folder_artwork(parts[0], parse_qs(parsed.query).get('path', ['.'])[0], auth.user)
    if len(parts) == 2 and parts[1] == 'items':
        return await native.items(parts[0], auth.user)
    if len(parts) == 3 and parts[1] == 'art':
        return await native.artwork(parts[0], parts[2], auth.user)
    if len(parts) == 3 and parts[1] == 'progress':
        return await native.save_playback_progress(parts[0], parts[2], native.PlaybackProgress(**body), auth.user)
    if len(parts) == 3 and parts[1] == 'user-data':
        return await native.save_user_media_data(parts[0], parts[2], native.UserMediaData(**body), auth.user)
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
    return {'Id': library_id(lib), 'DisplayPreferencesId': library_id(lib), 'ServerId': SERVER_ID, 'Name': lib['name'], 'Type': 'CollectionFolder',
            'IsFolder': True, 'CollectionType': 'homevideos', 'ChildCount': lib.get('count', 0),
            'RecursiveItemCount': lib.get('count', 0), 'ImageTags': {}, 'LocationType': 'FileSystem'}


def folder_id(lib, path):
    return library_id(lib) if path == '.' else digest('jellyfin-folder:' + lib['id'] + ':' + path)[:32]


def catalog_folders(entries):
    folders = {}
    for entry in entries:
        path = Path(entry.get('folder', '.'))
        if path.is_absolute() or '..' in path.parts:
            continue
        while path.as_posix() != '.':
            name = path.as_posix()
            folders[name] = folders.get(name, 0) + 1
            path = path.parent
    return folders


def folder_dto(lib, path, count=0):
    return {'Id': folder_id(lib, path), 'DisplayPreferencesId': folder_id(lib, path), 'ServerId': SERVER_ID, 'Name': Path(path).name,
            'SortName': Path(path).name, 'ParentId': folder_id(lib, Path(path).parent.as_posix()),
            'Type': 'Folder', 'IsFolder': True, 'RecursiveItemCount': count,
            'ChildCount': count, 'LocationType': 'FileSystem',
            'ImageTags': {'Primary': folder_id(lib, path)}}


def stream_dtos(item, tracks):
    used_indices = {t['index'] for t in tracks}
    if any(not isinstance(index, int) or index < 0 or index > 1024 for index in used_indices):
        raise HTTPException(400, 'Unsupported media stream index')
    video_index = next(index for index in range(1026) if index not in used_indices)
    video = item.get('video', True)
    streams = ([{'Type': 'Video', 'Codec': 'h264', 'Index': video_index, 'IsDefault': True}] if video else [])
    for track in tracks:
        stream = {'Type': track['type'].title(), 'Codec': 'aac' if track['type'] == 'audio' else track['codec'],
                  'Index': track['index'], 'Language': track['language'], 'Title': track['title'],
                  'DisplayTitle': ' · '.join(filter(None, [track['language'], track['title'], track['codec']])),
                  'IsDefault': track['default'], 'IsForced': track.get('forced', False), 'IsExternal': False}
        if track['type'] == 'audio':
            stream.update(Channels=2, SampleRate=48000)
        else:
            stream.update(IsTextSubtitleStream=track['text'], SupportsExternalStream=track['text'],
                          DeliveryMethod='External' if track['text'] else 'Encode')
            if track['text']:
                stream['Codec'] = 'webvtt'
        streams.append(stream)
    # Legacy TV StreamInfo indexes this list directly by the FFmpeg stream index.
    # Preserve gaps left by attachments/data rather than shifting subtitle tracks.
    indexed = {stream['Index']: stream for stream in streams}
    streams = [indexed.get(index, {'Type': 'Data', 'Codec': 'bin_data', 'Index': index, 'IsDefault': False})
               for index in range(max(indexed, default=-1) + 1)]
    for stream in streams:
        for key in ('IsInterlaced', 'IsForced', 'IsHearingImpaired', 'IsOriginal',
                    'IsExternal', 'IsTextSubtitleStream', 'SupportsExternalStream'):
            stream.setdefault(key, False)
    return streams


def source_dto(uid, item):
    # Item details must include a source even before PlaybackInfo creates a session.
    # Roku reads MediaSources[0].Container without checking whether the list is empty.
    return {'Id': uid, 'Name': item['name'], 'Protocol': 'File', 'Container': 'ts', 'Type': 'Default',
            'RunTimeTicks': int(item['duration'] * 10000000), 'MediaStreams': [],
            'SupportsDirectPlay': False, 'SupportsDirectStream': False, 'SupportsTranscoding': True,
            'TranscodingSubProtocol': 'hls', 'TranscodingContainer': 'ts',
            'DefaultSubtitleStreamIndex': -1, 'RequiresOpening': False, 'RequiresClosing': False,
            'IsRemote': False, 'ReadAtNativeFramerate': False, 'IgnoreDts': False, 'IgnoreIndex': False,
            'GenPtsInput': False, 'IsInfiniteStream': False, 'RequiresLooping': False,
            'SupportsProbing': False, 'HasSegments': False}


def item_dto(lib, item):
    uid = remember(lib, item)
    folder = item.get('folder', '.')
    name = item['name']
    video = item.get('video', True)
    saved = item.get('progress', {})
    return {'Id': uid, 'ServerId': SERVER_ID, 'Name': name, 'SortName': name,
            'ParentId': folder_id(lib, folder), 'Type': 'Video' if video else 'Audio',
            'MediaType': 'Video' if video else 'Audio', 'IsFolder': False,
            'RunTimeTicks': int(item['duration'] * 10000000), 'CanDownload': False, 'CanDelete': False,
            'PlayAccess': 'Full', 'LocationType': 'FileSystem', 'VideoType': 'VideoFile',
            'ImageTags': {'Primary': digest(str(item))[:32]},
            'UserData': {'PlaybackPositionTicks': int(saved.get('position', 0) * 10000000),
                         'PlayCount': int(saved.get('played', False)), 'IsFavorite': saved.get('favorite', False),
                         'Played': saved.get('played', False),
                         'LastPlayedDate': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(saved.get('updated', 0))),
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
        entries = (await media_call(request, auth, db, '/' + lib['id'] + '/items'))['items']
        if not locator:
            for path, count in catalog_folders(entries).items():
                if folder_id(lib, path) == uid:
                    return lib, {'_folder': path, 'count': count}
        for item in entries:
            if item_id(lib, item) == uid:
                remember(lib, item)
                return lib, item
    raise HTTPException(404, 'Media not found')


def envelope(items, start=0, total=None):
    return {'Items': items, 'TotalRecordCount': len(items) if total is None else total, 'StartIndex': start}


@router.get('/Users/{user_id}/Views')
@router.get('/UserViews')
async def views(request: Request, auth=Depends(authenticate), db=Depends(get_db)):
    return envelope([with_image_ticket(library_dto(lib), auth) for lib in await libraries(request, auth, db)])


@router.get('/Users/{user_id}/Items/Root')
async def root_item(auth=Depends(authenticate)):
    return {'Id': SERVER_ID, 'DisplayPreferencesId': SERVER_ID, 'Name': 'Media Center', 'Type': 'Folder', 'IsFolder': True}


@router.get('/Items')
@router.get('/Users/{user_id}/Items')
@router.get('/Users/{user_id}/Items/Latest')
@router.get('/Items/Latest')
async def browse(request: Request, auth=Depends(authenticate), db=Depends(get_db)):
    parent = query(request, 'ParentId', '').replace('-', '').lower()
    recursive = query(request, 'Recursive', 'false').lower() == 'true'
    latest = request.url.path.endswith('/Latest')
    search = query(request, 'SearchTerm', '').casefold()
    filters = set(query(request, 'Filters', '').lower().split(','))
    favorites = query(request, 'IsFavorite', '').lower() == 'true' or 'isfavorite' in filters
    ids = set(query(request, 'Ids', '').replace('-', '').lower().split(',')) - {''}
    types = set(query(request, 'IncludeItemTypes', '').split(',')) - {''}
    try:
        start = max(0, int(query(request, 'StartIndex', '0')))
        limit = min(500, max(0, int(query(request, 'Limit', '100'))))
    except ValueError:
        raise HTTPException(400, 'Invalid pagination')
    result = []
    for lib in await libraries(request, auth, db):
        if not parent and not recursive and not latest and not search and not ids and not favorites and not (filters & {'isplayed', 'isunplayed'}):
            result.append(library_dto(lib))
            continue
        entries = (await media_call(request, auth, db, '/' + lib['id'] + '/items'))['items']
        folders = catalog_folders(entries)
        target = '.'
        if parent and parent not in (SERVER_ID, library_id(lib)):
            target = next((path for path in folders if folder_id(lib, path) == parent), None)
            if target is None:
                continue
        flat = recursive or latest or bool(search) or bool(ids) or favorites or bool(filters & {'isplayed', 'isunplayed'})
        if not flat:
            result.extend(folder_dto(lib, path, count) for path, count in folders.items()
                          if Path(path).parent.as_posix() == target)
        for item in entries:
            folder = item.get('folder', '.')
            if (not flat and folder != target) or (flat and target != '.' and folder != target and not folder.startswith(target + '/')):
                continue
            dto = item_dto(lib, item)
            searchable = folder + '/' + dto['Name']
            if (not ids or dto['Id'] in ids) and (not search or search in searchable.casefold()):
                result.append(dto)
    result.sort(key=lambda entry: (not entry['IsFolder'], media.natural(entry['Name'])))
    if types:
        result = [item for item in result if item['Type'] in types]
    if favorites:
        result = [item for item in result if item.get('UserData', {}).get('IsFavorite')]
    if 'isplayed' in filters:
        result = [item for item in result if item.get('UserData', {}).get('Played')]
    if 'isunplayed' in filters:
        result = [item for item in result if not item.get('UserData', {}).get('Played')]
    # Native catalogs already preserve natural folder/filename order.
    page = [with_image_ticket(dto, auth) for dto in result[start:start + limit]]
    return page if latest else envelope(page, start, len(result))


@router.api_route('/Users/{user_id}/PlayedItems/{uid}', methods=['POST', 'DELETE'])
@router.api_route('/UserPlayedItems/{uid}', methods=['POST', 'DELETE'])
@router.api_route('/Users/{user_id}/FavoriteItems/{uid}', methods=['POST', 'DELETE'])
@router.api_route('/UserFavoriteItems/{uid}', methods=['POST', 'DELETE'])
async def change_user_data(uid: str, request: Request, auth=Depends(authenticate), db=Depends(get_db)):
    lib, item = await resolve(request, auth, db, uid)
    if not item or '_folder' in item:
        raise HTTPException(400, 'Select a playable item')
    field = 'favorite' if 'favoriteitems' in request.url.path.lower() else 'played'
    saved = await media_call(request, auth, db, f"/{lib['id']}/user-data/{item['id']}",
                             'POST', {field: request.method == 'POST'})
    return item_dto(lib, {**item, 'progress': saved})['UserData']


@router.get('/UserItems/Resume')
@router.get('/Items/Resume')
@router.get('/Users/{user_id}/Items/Resume')
async def resume(request: Request, auth=Depends(authenticate), db=Depends(get_db)):
    result = []
    for lib in await libraries(request, auth, db):
        for item in (await media_call(request, auth, db, '/' + lib['id'] + '/items'))['items']:
            if item.get('progress', {}).get('position', 0) > 0:
                result.append((item['progress']['updated'], item_dto(lib, item)))
    result.sort(key=lambda entry: entry[0], reverse=True)
    try:
        start = max(0, int(query(request, 'StartIndex', '0')))
        limit = min(200, max(0, int(query(request, 'Limit', '100'))))
    except ValueError:
        raise HTTPException(400, 'Invalid pagination')
    return envelope([with_image_ticket(entry[1], auth) for entry in result[start:start + limit]], start, len(result))


@router.get('/Items/Filters')
@router.get('/Items/Filters2')
async def item_filters(auth=Depends(authenticate)):
    return {'Genres': [], 'Tags': [], 'Years': [], 'OfficialRatings': []}


@router.get('/Persons')
@router.get('/Artists')
@router.get('/Artists/AlbumArtists')
@router.get('/LiveTv/Programs/Recommended')
async def unmodeled_collections(auth=Depends(authenticate)):
    return envelope([])


@router.get('/MediaSegments/{uid}')
@router.get('/Videos/{uid}/AdditionalParts')
async def extra_parts(uid: str, request: Request, auth=Depends(authenticate), db=Depends(get_db)):
    await resolve(request, auth, db, uid)
    return envelope([])


@router.get('/Items/{uid}/Images')
async def image_list(uid: str, request: Request, auth=Depends(authenticate), db=Depends(get_db)):
    await resolve(request, auth, db, uid)
    client_header = request.headers.get('X-Emby-Authorization') or request.headers.get('Authorization', '')
    if re.search(r'Client\s*=\s*"Jellyfin Roku"', client_header, re.I):
        # Roku PosterImage discards ImageTag from this list and overrides its
        # authenticated-tag poster URL. An empty list selects the ImageTags fallback.
        return []
    tag = with_image_ticket({'Id': uid.replace('-', '').lower()}, auth)['ImageTags']['Primary']
    return [{'ImageType': 'Primary', 'ImageIndex': 0, 'ImageTag': tag, 'Size': 0}]


@router.get('/Items/{uid}')
@router.get('/Users/{user_id}/Items/{uid}')
async def item_details(uid: str, request: Request, auth=Depends(authenticate), db=Depends(get_db)):
    lib, item = await resolve(request, auth, db, uid)
    if item and '_folder' in item:
        return with_image_ticket(folder_dto(lib, item['_folder'], item['count']), auth)
    if not item:
        return with_image_ticket(library_dto(lib), auth)
    dto = item_dto(lib, item)
    tracks = (await media_call(request, auth, db, f"/{lib['id']}/tracks/{item['id']}"))['tracks']
    dto['MediaSources'] = [source_dto(dto['Id'], item)]
    dto['MediaStreams'] = stream_dtos(item, tracks)
    dto['MediaSources'][0]['MediaStreams'] = dto['MediaStreams']
    dto['MediaSources'][0]['DefaultAudioStreamIndex'] = next(
        (t['index'] for t in tracks if t['type'] == 'audio' and t['default']),
        next((t['index'] for t in tracks if t['type'] == 'audio'), -1))
    return with_image_ticket(dto, auth)


@router.get('/Items/{uid}/Images/Primary')
@router.get('/Items/{uid}/Images/Primary/{index}')
async def image(uid: str, request: Request, auth=Depends(authenticate_image), db=Depends(get_db)):
    lib, item = await resolve(request, auth, db, uid)
    if not item or '_folder' in item:
        path = item['_folder'] if item else '.'
        return await media_call(request, auth, db, f"/{lib['id']}/folder-art?" + urlencode({'path': path}))
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
    if not item or '_folder' in item:
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
    preferences = (await media.read('jellyfin-preferences:' + auth.uid) or {}).get('configuration', {})
    audio_choice = body_value(body, 'AudioStreamIndex', query(request, 'AudioStreamIndex'))
    subtitle_choice = body_value(body, 'SubtitleStreamIndex', query(request, 'SubtitleStreamIndex'))
    if audio_choice is None:
        preferred = preferences.get('AudioLanguagePreference')
        audio_choice = next((t['index'] for t in tracks if t['type'] == 'audio' and t['language'] == preferred), -1) if not preferences.get('PlayDefaultAudioTrack', True) else -1
    if subtitle_choice is None:
        mode = preferences.get('SubtitleMode', 'None')
        subtitles = [t for t in tracks if t['type'] == 'subtitle']
        preferred = preferences.get('SubtitleLanguagePreference')
        matches = [t for t in subtitles if t['language'] == preferred]
        if mode == 'OnlyForced':
            matches = [t for t in subtitles if t.get('forced') and (not preferred or t['language'] == preferred)]
        elif mode == 'Default':
            matches = [t for t in subtitles if t['default']]
        elif mode == 'Always':
            matches = matches or subtitles
        elif mode == 'Smart':
            selected_audio = next((t for t in tracks if t['type'] == 'audio' and str(t['index']) == str(audio_choice)),
                                  next((t for t in tracks if t['type'] == 'audio' and t['default']), {}))
            if selected_audio.get('language') == preferred:
                matches = [t for t in matches if t.get('forced')]
        else:
            matches = []
        subtitle_choice = matches[0]['index'] if matches else -1
    try:
        audio_index = int(audio_choice)
        subtitle_index = int(subtitle_choice)
    except (ValueError, TypeError):
        raise HTTPException(400, 'Invalid media track')
    if audio_index < -1 or subtitle_index < -1:
        raise HTTPException(400, 'Invalid media track')
    if audio_index >= 0 and not any(t['type'] == 'audio' and t['index'] == audio_index for t in tracks):
        raise HTTPException(400, 'Unavailable audio track')
    selected_subtitle = next((t for t in tracks if t['type'] == 'subtitle' and t['index'] == subtitle_index), None)
    if subtitle_index >= 0 and not selected_subtitle:
        raise HTTPException(400, 'Unavailable subtitle track')
    streams = stream_dtos(item, tracks)
    playback = await media_call(request, auth, db, f"/{lib['id']}/play/{item['id']}", 'POST')
    playback['url'] += '&' + urlencode({'audio': audio_index,
                                        'subtitle': subtitle_index if selected_subtitle and not selected_subtitle['text'] else -1})
    play_id = secrets.token_hex(16)
    _plays[play_id] = {'token': digest(auth.token), 'item': uid, 'url': playback['url'],
                       'seen': time.monotonic(), 'profiles': profiles, 'library_id': lib['id'], 'native_id': item['id'],
                       'audio_indices': [t['index'] for t in tracks if t['type'] == 'audio'],
                       'subtitle_indices': [t['index'] for t in tracks if t['type'] == 'subtitle'],
                       'text_indices': [t['index'] for t in tracks if t['type'] == 'subtitle' and t['text']]}
    while len(_plays) > 256:
        _plays.popitem(last=False)
    params = urlencode({'api_key': auth.token, 'PlaySessionId': play_id})
    for stream in streams:
        if stream['Type'] == 'Subtitle' and stream['IsTextSubtitleStream']:
            stream['DeliveryUrl'] = f"Videos/{uid}/{uid}/Subtitles/{stream['Index']}/Stream.vtt?{params}"
    default_audio = next((t['index'] for t in tracks if t['type'] == 'audio' and t['default']),
                         next((t['index'] for t in tracks if t['type'] == 'audio'), -1))
    # Describe the backing file, not the HTTP transport of the separate HLS URL.
    # Android TV rejects remote sources before it considers SupportsTranscoding.
    source = source_dto(uid, item)
    source.update(MediaStreams=streams, TranscodingUrl=f'Videos/{uid}/master.m3u8?{params}',
                  DefaultAudioStreamIndex=audio_index if audio_index >= 0 else default_audio,
                  DefaultSubtitleStreamIndex=subtitle_index)
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


async def persist_progress(request, auth, db, record, body):
    ticks = body_value(body, 'PositionTicks')
    if ticks is None:
        return
    try:
        position = float(ticks) / 10000000
        native.PlaybackProgress(position=position)
    except (TypeError, ValueError):
        raise HTTPException(400, 'Invalid playback position')
    await media_call(request, auth, db, f"/{record['library_id']}/progress/{record['native_id']}",
                     'POST', {'position': position})


@router.post('/Sessions/Playing', status_code=204)
@router.post('/Sessions/Playing/Progress', status_code=204)
async def progress(request: Request, body: dict = Body(default={}), auth=Depends(authenticate), db=Depends(get_db)):
    record = play_record(auth, body_value(body, 'PlaySessionId', ''))
    await persist_progress(request, auth, db, record, body)
    return Response(status_code=204)


@router.post('/Sessions/Playing/Stopped', status_code=204)
async def stopped(request: Request, body: dict = Body(default={}), auth=Depends(authenticate), db=Depends(get_db)):
    play_id = body_value(body, 'PlaySessionId', '')
    record = play_record(auth, play_id)
    await persist_progress(request, auth, db, record, body)
    ticket = parse_qs(urlsplit(record['url']).query)['ticket'][0]
    await media_call(request, auth, db, '/sessions/stop', 'POST', {'ticket': ticket})
    _plays.pop(play_id, None)
    return Response(status_code=204)


@router.delete('/Videos/ActiveEncodings', status_code=204)
async def stop_encoding(request: Request, auth=Depends(authenticate), db=Depends(get_db)):
    play_id = query(request, 'PlaySessionId', '')
    record = _plays.get(play_id)
    # Native clients can send cleanup twice, or after Stopped. Never release a
    # different app token's session, even when it belongs to the same Nostr user.
    if record and record['token'] == digest(auth.token):
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


def display_defaults(preference_id, client):
    return {'Id': preference_id, 'ViewType': 'Poster', 'SortBy': 'SortName', 'SortOrder': 'Ascending',
            'IndexBy': 'None', 'RememberIndexing': False, 'RememberSorting': False, 'CustomPrefs': {},
            'PrimaryImageHeight': 250, 'PrimaryImageWidth': 250, 'ScrollDirection': 'Horizontal',
            'ShowBackdrop': True, 'ShowSidebar': False, 'Client': client}


def display_key(preference_id, request):
    client = query(request, 'client', 'emby')
    if len(preference_id) > 128 or len(client) > 128:
        raise HTTPException(400, 'Invalid preference identifier')
    return 'display:' + digest(json.dumps([preference_id, client])), client


@router.get('/DisplayPreferences/{preference_id}')
async def display_preferences(preference_id: str, request: Request, auth=Depends(authenticate)):
    key, client = display_key(preference_id, request)
    saved = await media.read('jellyfin-preferences:' + auth.uid) or {}
    return {**display_defaults(preference_id, client), **saved.get(key, {})}


@router.post('/DisplayPreferences/{preference_id}', status_code=204)
async def update_display_preferences(preference_id: str, request: Request, body: dict = Body(...), auth=Depends(authenticate)):
    key, client = display_key(preference_id, request)
    defaults = display_defaults(preference_id, client)
    allowed = {key.casefold(): key for key in defaults if key not in ('Id', 'Client')}
    update = {}
    for name, value in body.items():
        canonical = allowed.get(name.casefold())
        if canonical is None:
            continue
        if type(value) is not type(defaults[canonical]):
            raise HTTPException(400, 'Invalid display preference type')
        if canonical == 'CustomPrefs' and any(not isinstance(v, str) for v in value.values()):
            raise HTTPException(400, 'Custom preferences must be strings')
        if canonical == 'SortOrder' and value not in ('Ascending', 'Descending'):
            raise HTTPException(400, 'Invalid sort order')
        if canonical == 'ScrollDirection' and value not in ('Horizontal', 'Vertical'):
            raise HTTPException(400, 'Invalid scroll direction')
        update[canonical] = value
    if len(json.dumps(update).encode()) > 8192:
        raise HTTPException(400, 'Display preferences are too large')
    await save_preferences(auth.uid, key, update)
    return Response(status_code=204)


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
