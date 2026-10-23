"""CalDAV/CardDAV as a PHONE speaks it, against the node's REAL Radicale mount.

  The server  app/services/caldav/server.build_app() -- the shipped Radicale with the shipped auth and storage
              plugins -- behind a2wsgi at /caldav, gated exactly as app/main.py gates it; the storage plugin mirrors
              to the REAL caldav_store -> nostr_store -> the shipped relay (realnode_harness.Home). The web
              client's routers (calendar.py, contacts.py) answer /api/calendar and /api/contacts on the same app,
              over the same relay, as on a node.
  Chosen      the account table (user_table.by_username/by_email), the per-user settings rows (where the CalDAV
              app password's PBKDF2 hash lives), the storage key, and the relay port. Nothing else.

  Phone       raw WebDAV with the request bodies DAVx5 (and Thunderbird's CardBook/Lightning, which use the same
              RFC 4791/6352/6578 reports) send: current-user-principal discovery, the home-set PROPFIND, the
              Depth:1 collection listing with getctag + sync-token, `sync-collection` (RFC 6578) for the delta, a
              `calendar-multiget` / `addressbook-multiget` for the bodies, and PUT / DELETE with If-None-Match /
              If-Match. It keeps what a phone keeps between syncs -- the sync-token and each href's etag -- and
              `sync()` answers what changed.
"""

import asyncio
import base64
import hashlib
import json
import re
import tempfile
import threading
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from urllib.parse import unquote

NS = {'d': 'DAV:', 'c': 'urn:ietf:params:xml:ns:caldav', 'card': 'urn:ietf:params:xml:ns:carddav',
      'cs': 'http://calendarserver.org/ns/', 'a': 'http://apple.com/ns/ical/'}

# --- the bodies DAVx5 sends (DavResourceFinder / SyncManager), namespaces as it declares them -------------------
PRINCIPAL = ('<?xml version="1.0" encoding="UTF-8"?><propfind xmlns="DAV:"><prop><current-user-principal/>'
             '</prop></propfind>')
HOMES = ('<?xml version="1.0" encoding="UTF-8"?><propfind xmlns="DAV:" xmlns:CAL="urn:ietf:params:xml:ns:caldav" '
         'xmlns:CARD="urn:ietf:params:xml:ns:carddav"><prop><CAL:calendar-home-set/><CARD:addressbook-home-set/>'
         '<current-user-principal/><displayname/></prop></propfind>')
COLLECTIONS = ('<?xml version="1.0" encoding="UTF-8"?><propfind xmlns="DAV:" xmlns:CAL="urn:ietf:params:xml:ns:caldav" '
               'xmlns:CARD="urn:ietf:params:xml:ns:carddav" xmlns:CS="http://calendarserver.org/ns/" '
               'xmlns:ICAL="http://apple.com/ns/ical/"><prop><resourcetype/><displayname/><ICAL:calendar-color/>'
               '<CAL:supported-calendar-component-set/><CS:getctag/><sync-token/><current-user-privilege-set/>'
               '<CARD:addressbook-description/><CAL:calendar-description/></prop></propfind>')
CTAG = ('<?xml version="1.0" encoding="UTF-8"?><propfind xmlns="DAV:" xmlns:CS="http://calendarserver.org/ns/">'
        '<prop><CS:getctag/><sync-token/></prop></propfind>')
SYNC = ('<?xml version="1.0" encoding="UTF-8"?><sync-collection xmlns="DAV:"><sync-token>{token}</sync-token>'
        '<sync-level>1</sync-level><prop><getetag/></prop></sync-collection>')
ETAGS = ('<?xml version="1.0" encoding="UTF-8"?><propfind xmlns="DAV:"><prop><resourcetype/><getetag/></prop>'
         '</propfind>')
CAL_MULTIGET = ('<?xml version="1.0" encoding="UTF-8"?><CAL:calendar-multiget xmlns="DAV:" '
                'xmlns:CAL="urn:ietf:params:xml:ns:caldav"><prop><getcontenttype/><getetag/><CAL:calendar-data/>'
                '</prop>{hrefs}</CAL:calendar-multiget>')
CARD_MULTIGET = ('<?xml version="1.0" encoding="UTF-8"?><CARD:addressbook-multiget xmlns="DAV:" '
                 'xmlns:CARD="urn:ietf:params:xml:ns:carddav"><prop><getcontenttype/><getetag/>'
                 '<CARD:address-data/></prop>{hrefs}</CARD:addressbook-multiget>')


def storage_sk(u) -> bytes:
    return hashlib.sha256(b'pc-dav-wire:' + str(u.username).encode()).digest()


class Node:
    """The app pieces a phone and the web client reach, on one ASGI app, run on its own loop thread."""

    def __init__(self, mp, relay_port, users, password='phone-app-pw', routers=True):
        from app.services import caldav_store, nostr_store, relay_blocklist, user_settings_table, user_table
        from app.services.caldav import auth as caldav_auth
        from app.services.caldav import server as caldav_server
        from app.services.caldav import storage as caldav_storage
        self.users = {u.username: u for u in users}
        self.password = password
        self.rows = {}
        self.tmp = tempfile.mkdtemp(prefix='pc-davwire-')
        mp.setenv('POSTERCHANAI_CALDAV_DIR', self.tmp)
        mp.setattr(caldav_server, '_app', None)              # built fresh, on this working directory
        caldav_storage._hydrated.clear()
        caldav_storage._ours.clear()
        mp.setattr(caldav_store, 'enabled', lambda: True)
        mp.setattr(caldav_store, '_port', lambda db=None: relay_port)
        mp.setattr(caldav_store, 'user_storage_seckey', lambda db, u: storage_sk(u))
        mp.setattr(nostr_store, 'user_storage_seckey', lambda db, u: storage_sk(u))
        mp.setattr(user_table, 'by_username', lambda db, name: self.users.get(name))
        mp.setattr(user_table, 'by_email', lambda db, e: next((u for u in self.users.values() if u.email == e), None))
        mp.setattr(relay_blocklist, 'is_user_blocked', lambda u: False)

        def get(db, uid, key, default=None):
            return self.rows.get((uid, key), default)

        async def aget(db, uid, key, default=None):
            return get(db, uid, key, default)

        async def aset(db, uid, key, value):
            self.rows[(uid, key)] = value

        async def ahas(db, uid, key):
            return (uid, key) in self.rows
        for name, fn in (('get', get), ('aget', aget), ('aset', aset), ('ahas', ahas)):
            mp.setattr(user_settings_table, name, fn)
        for u in users:
            self.rows[(u.id, caldav_auth.SETTING_KEY)] = caldav_auth.hash_password(password)
        self.app = self._build(routers)
        ready = threading.Event()

        def run():
            import httpx
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='https://node',
                                            timeout=300)
            ready.set()
            self.loop.run_forever()
        threading.Thread(target=run, daemon=True).start()
        ready.wait(10)

    def _build(self, routers):
        """app/main.py's /caldav mount (same gate, same a2wsgi), plus the web routers."""
        from a2wsgi import WSGIMiddleware
        from fastapi import FastAPI, HTTPException, Request
        from starlette.responses import PlainTextResponse
        from app import auth as app_auth
        from app.database import get_db
        from app.services import caldav_store
        from app.services.caldav.server import build_app
        app = FastAPI()
        dav = WSGIMiddleware(build_app())

        async def entry(scope, receive, send):
            if not caldav_store.enabled():
                await PlainTextResponse('Calendar server is off', status_code=404)(scope, receive, send)
                return
            await dav(scope, receive, send)
        app.mount('/caldav', entry)
        if routers:
            from app.routers import calendar, contacts
            app.include_router(calendar.router)
            app.include_router(contacts.router)

            def who(request: Request):
                name = (request.headers.get('authorization') or '').removeprefix('Bearer ').strip()
                if name not in self.users:
                    raise HTTPException(401, 'not signed in')
                return self.users[name]
            app.dependency_overrides[app_auth.get_current_user] = who
            app.dependency_overrides[get_db] = lambda: None
        return app

    def request(self, method, path, headers=None, content=None):
        async def go():
            return await self.client.request(method, path, headers=headers or {}, content=content)
        return asyncio.run_coroutine_threadsafe(go(), self.loop).result(300)

    def web(self, user, method, path, body=None):
        """The web client's own call (what calendar.js / contacts.js send)."""
        r = self.request(method, path, {'Authorization': 'Bearer ' + user.username,
                                        'Content-Type': 'application/json'},
                         json.dumps(body).encode() if body is not None else None)
        return r.status_code, (r.json() if r.headers.get('content-type', '').startswith('application/json') else r.text)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


def user(i):
    return SimpleNamespace(id=5000 + i, username='dav%d' % i, nostr_npub=('%02x' % (0x40 + i)) * 32,
                           is_admin=False, email='dav%d@example.invalid' % i)


# ------------------------------------------------------------------------------------------------ the phone
class Response:
    def __init__(self, el):
        self.href = unquote(el.findtext('d:href', namespaces=NS) or '')
        self.status = el.findtext('d:status', namespaces=NS) or ''
        self.props = {}
        for ps in el.findall('d:propstat', NS):
            ok = ' 200 ' in (ps.findtext('d:status', namespaces=NS) or '')
            for p in ps.find('d:prop', NS) or []:
                if ok:
                    self.props[p.tag] = p


def multistatus(text) -> list:
    root = ET.fromstring(text)
    return [Response(r) for r in root.findall('d:response', NS)]


def clark(prefix, name):
    return '{%s}%s' % (NS[prefix], name)


class Phone:
    """A CalDAV/CardDAV client: one account, HTTP Basic with the app password, DAVx5's requests."""

    def __init__(self, node: Node, username, password=None, ua='DAVx5/4.4.3-ose (2024/09/20; dav4jvm; okhttp/4.12.0) Android/14'):
        self.node, self.username = node, username
        self.auth = 'Basic ' + base64.b64encode(('%s:%s' % (username, password or node.password)).encode()).decode()
        self.ua = ua
        self.state = {}          # collection href -> {'token': sync-token, 'etags': {href: etag}, 'data': {href: text}}
        self.log = []            # (method, path, status)

    def req(self, method, path, body=None, headers=None):
        h = {'Authorization': self.auth, 'User-Agent': self.ua}
        if body is not None:
            h['Content-Type'] = 'application/xml; charset=utf-8' if body.lstrip().startswith('<') else \
                'text/calendar; charset=utf-8'
        h.update(headers or {})
        r = self.node.request(method, path, h, body.encode() if isinstance(body, str) else body)
        self.log.append((method, path, r.status_code))
        return r

    # -- discovery (DavResourceFinder) --------------------------------------------------------------------------
    def discover(self, base='/caldav/'):
        r = self.req('PROPFIND', base, PRINCIPAL, {'Depth': '0'})
        assert r.status_code == 207, (r.status_code, r.text[:300])
        principal = next(x.props[clark('d', 'current-user-principal')].findtext('d:href', namespaces=NS)
                         for x in multistatus(r.text) if clark('d', 'current-user-principal') in x.props)
        r = self.req('PROPFIND', principal, HOMES, {'Depth': '0'})
        assert r.status_code == 207, (r.status_code, r.text[:300])
        x = multistatus(r.text)[0]
        homes = {k: x.props[clark(*k)].findtext('d:href', namespaces=NS)
                 for k in (('c', 'calendar-home-set'), ('card', 'addressbook-home-set')) if clark(*k) in x.props}
        return principal, homes

    def collections(self, home):
        """{href: {'type': 'calendar'|'addressbook', 'name', 'ctag', 'token', 'components'}} -- the Depth:1 listing."""
        r = self.req('PROPFIND', home, COLLECTIONS, {'Depth': '1'})
        assert r.status_code == 207, (r.status_code, r.text[:300])
        out = {}
        for x in multistatus(r.text):
            rt = x.props.get(clark('d', 'resourcetype'))
            if rt is None:
                continue
            kind = ('calendar' if rt.find('c:calendar', NS) is not None else
                    'addressbook' if rt.find('card:addressbook', NS) is not None else None)
            if not kind:
                continue
            comps = x.props.get(clark('c', 'supported-calendar-component-set'))
            out[x.href] = {'type': kind, 'name': x.props.get(clark('d', 'displayname')).text
                           if clark('d', 'displayname') in x.props else '',
                           'ctag': x.props[clark('cs', 'getctag')].text if clark('cs', 'getctag') in x.props else None,
                           'token': x.props[clark('d', 'sync-token')].text if clark('d', 'sync-token') in x.props else None,
                           'components': [c.get('name') for c in comps] if comps is not None else []}
        return out

    def ctag(self, coll):
        r = self.req('PROPFIND', coll, CTAG, {'Depth': '0'})
        assert r.status_code == 207, r.text[:300]
        x = multistatus(r.text)[0]
        return x.props[clark('cs', 'getctag')].text, x.props[clark('d', 'sync-token')].text

    # -- sync (SyncManager: sync-collection, then multiget for what changed) -----------------------------------
    def sync(self, coll):
        """One sync of `coll`. Returns {'changed': {href: data}, 'deleted': [href], 'full': bool}."""
        st = self.state.setdefault(coll, {'token': '', 'etags': {}, 'data': {}})
        r = self.req('REPORT', coll, SYNC.format(token=st['token']), {'Depth': '1'})
        full = False
        if r.status_code in (403, 409) and st['token']:
            # The token is no longer valid (RFC 6578 3.2: DAV:valid-sync-token) -> start over.
            st['token'] = ''
            full = True
            r = self.req('REPORT', coll, SYNC.format(token=''), {'Depth': '1'})
        assert r.status_code == 207, (r.status_code, r.text[:500])
        root = ET.fromstring(r.text)
        changed, deleted = [], []
        for x in multistatus(r.text):
            if x.href.rstrip('/') == coll.rstrip('/'):
                continue
            if ' 404 ' in x.status:
                deleted.append(x.href)
                st['etags'].pop(x.href, None)
                st['data'].pop(x.href, None)
                continue
            et = x.props.get(clark('d', 'getetag'))
            et = et.text if et is not None else None
            if st['etags'].get(x.href) != et:
                changed.append(x.href)
        st['token'] = root.findtext('d:sync-token', namespaces=NS) or st['token']
        got = self.multiget(coll, changed) if changed else {}
        for href, (etag, data) in got.items():
            st['etags'][href] = etag
            st['data'][href] = data
        return {'changed': {h: d for h, (e, d) in got.items()}, 'deleted': deleted, 'full': full}

    def multiget(self, coll, hrefs):
        book = coll in self.state and any(h.endswith('.vcf') for h in hrefs) or any(h.endswith('.vcf') for h in hrefs)
        tpl, tag = (CARD_MULTIGET, clark('card', 'address-data')) if book else (CAL_MULTIGET, clark('c', 'calendar-data'))
        body = tpl.format(hrefs=''.join('<href>%s</href>' % h for h in hrefs))
        r = self.req('REPORT', coll, body, {'Depth': '1'})
        assert r.status_code == 207, (r.status_code, r.text[:500])
        out = {}
        for x in multistatus(r.text):
            if tag in x.props:
                out[x.href] = (x.props[clark('d', 'getetag')].text, x.props[tag].text or '')
        return out

    # -- writes ------------------------------------------------------------------------------------------------
    def put(self, href, text, etag=None, create=False):
        h = {'Content-Type': ('text/vcard; charset=utf-8' if href.endswith('.vcf') else 'text/calendar; charset=utf-8')}
        if create:
            h['If-None-Match'] = '*'
        elif etag:
            h['If-Match'] = etag
        return self.req('PUT', href, text, h)

    def delete(self, href, etag=None):
        return self.req('DELETE', href, None, {'If-Match': etag} if etag else {})

    def get(self, href):
        return self.req('GET', href)


def unfold(text: str) -> str:
    return re.sub(r'\r?\n[ \t]', '', text or '').replace('\r\n', '\n')


def lines(text: str) -> list:
    return [l for l in unfold(text).split('\n') if l]
