"""THE WOPI BOUNDARY, ASKED OVER HTTP THE WAY COLLABORA ASKS IT.

app/routers/office.py is the storage host Collabora (CODE) loads and saves documents through: CheckFileInfo
(GET /wopi/files/<id>), GetFile (GET .../contents), PutFile (POST .../contents) and the lock operations (POST
/wopi/files/<id> + X-WOPI-Override). The only thing that authorises any of them is the `access_token` the app issued
for ONE session -- CODE itself has no idea who anybody is. So every route is tried here with: no token, a forged
token, an expired one, a token whose expiry was edited to live longer, another person's file, the same person's OTHER
file, a closed session, and an owner who is no longer a member. Each must be refused AND leave the stored document
exactly as it was.

Save As / Export / Rename from INSIDE the editor (WOPI PutRelativeFile, RenameFile) are not storage operations this
host has: the drive is encrypted in the browser, so a new or renamed drive file can only be made there (the editor's
own "Save As..." button does exactly that). CheckFileInfo therefore tells CODE so -- UserCanNotWriteRelative, no
SupportsRename/UserCanRename -- and the two operations are refused with every kind of token and target, writing nothing.

These need no CODE and no browser; they run everywhere (the deploy gate and the desktop CI's required list). The real
Collabora round trip is tests/client/test_office_real_collabora.py.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app import auth as app_auth
from app.routers import office
from tests.member_fixtures import allow_keys

ALICE = SimpleNamespace(id=1, nostr_npub='a1' * 32, username='alice', is_admin=False)
BOB = SimpleNamespace(id=2, nostr_npub='b2' * 32, username='bob', is_admin=False)
USERS = {'alice': ALICE, 'bob': BOB}


@pytest.fixture
def node(tmp_path, monkeypatch):
    monkeypatch.setattr(office, '_ROOT', Path(tmp_path))
    monkeypatch.setenv('POSTERCHANAI_OFFICE', '1')
    office._LOCKS.clear()

    async def action(_ext, _mode):              # discovery is CODE's; this file tests the storage host only
        return 'http://127.0.0.1:9983/office-code/browser/h/cool.html?'

    async def exts():
        return frozenset(office._EXTS_FALLBACK)
    monkeypatch.setattr(office, '_action_url', action)
    monkeypatch.setattr(office, '_accepted_exts', exts)
    allow_keys(monkeypatch, ALICE.nostr_npub, BOB.nostr_npub)
    app = FastAPI()
    app.include_router(office.router)

    def who(request: Request):
        u = USERS.get(request.headers.get('x-test-user', ''))
        if not u:
            raise HTTPException(401, 'not signed in')
        return u
    app.dependency_overrides[app_auth.get_current_user] = who
    with TestClient(app) as c:
        yield SimpleNamespace(c=c, root=Path(tmp_path), mp=monkeypatch)
    office._LOCKS.clear()


def open_doc(n, user, name='report.docx', data=b'original bytes', mode='edit'):
    r = n.c.post('/client/office/session', headers={'x-test-user': user},
                 files={'file': (name, data, 'application/octet-stream')}, data={'mode': mode})
    assert r.status_code == 200, r.text
    return r.json()


def stored(n, s):
    return (n.root / s['id'] / 'document').read_bytes()


def version(n, s):
    return n.c.get('/wopi/files/%s' % s['id'], params={'access_token': s['token']}).json()['Version']


def every_route(n, file_id, params, headers=None):
    """(name, response) for every WOPI and session route, called the way CODE / the client call them."""
    h = dict(headers or {})
    u = '/wopi/files/%s' % file_id
    return [
        ('CheckFileInfo', n.c.get(u, params=params, headers=h)),
        ('GetFile', n.c.get(u + '/contents', params=params, headers=h)),
        ('PutFile', n.c.post(u + '/contents', params=params, content=b'ATTACKER BYTES',
                             headers={**h, 'X-WOPI-Override': 'PUT'})),
        ('Lock', n.c.post(u, params=params, headers={**h, 'X-WOPI-Override': 'LOCK', 'X-WOPI-Lock': 'evil'})),
        ('GetLock', n.c.post(u, params=params, headers={**h, 'X-WOPI-Override': 'GET_LOCK'})),
        ('PutRelativeFile', n.c.post(u, params=params, content=b'ATTACKER BYTES',
                                     headers={**h, 'X-WOPI-Override': 'PUT_RELATIVE',
                                              'X-WOPI-SuggestedTarget': 'copy.docx', 'X-WOPI-Size': '14'})),
        ('RenameFile', n.c.post(u, params=params, headers={**h, 'X-WOPI-Override': 'RENAME_FILE',
                                                           'X-WOPI-RequestedName': 'renamed'})),
        ('session contents', n.c.get('/client/office/session/%s/contents' % file_id, params=params, headers=h)),
        ('session export', n.c.get('/client/office/session/%s/export/pdf' % file_id, params=params, headers=h)),
        ('session close', n.c.delete('/client/office/session/%s' % file_id, params=params, headers=h)),
    ]


def assert_all_refused(results, allowed=(401, 403, 404), why=''):
    bad = [(name, r.status_code, r.text[:80]) for name, r in results if r.status_code not in allowed]
    assert not bad, ('%s: these routes did not refuse' % why, bad)
    leaked = [name for name, r in results if b'original bytes' in r.content or b'bob secret' in r.content]
    assert not leaked, ('%s: a refusal carried the document' % why, leaked)


def test_a_valid_token_works_on_every_route(node):
    """The control: without it every refusal below could be a route that never works at all."""
    s = open_doc(node, 'alice')
    p = {'access_token': s['token']}
    assert node.c.get('/wopi/files/%s' % s['id'], params=p).json()['BaseFileName'] == 'report.docx'
    assert node.c.get('/wopi/files/%s/contents' % s['id'], params=p).content == b'original bytes'
    assert node.c.post('/wopi/files/%s/contents' % s['id'], params=p, content=b'v2',
                       headers={'X-WOPI-Override': 'PUT'}).status_code == 200
    assert stored(node, s) == b'v2'
    assert version(node, s) == '2'


def test_no_token_is_401_on_every_route(node):
    """WOPI's answer for a missing token is 401 (CODE maps it to "unauthorized"); it was FastAPI's 422 validation
    error -- a refusal, but one that reads as a malformed request and names the parameter it wanted."""
    s = open_doc(node, 'alice')
    results = every_route(node, s['id'], {})
    assert_all_refused(results, (401,), 'no token')
    assert stored(node, s) == b'original bytes'


def test_a_forged_token_is_refused(node):
    s = open_doc(node, 'alice')
    expires = s['token'].split('.')[0]
    for forged in ('x', '', '.', expires + '.' + '0' * 64, expires + '.' + s['token'].split('.')[1][::-1],
                   'notanumber.' + s['token'].split('.')[1]):
        assert_all_refused(every_route(node, s['id'], {'access_token': forged}), (401,), 'forged %r' % forged)
    assert stored(node, s) == b'original bytes'


def test_an_expired_token_is_refused_even_with_a_valid_signature(node):
    s = open_doc(node, 'alice')
    old = office._token(s['id'], int(time.time()) - 5)             # signed by the server, just out of date
    assert_all_refused(every_route(node, s['id'], {'access_token': old}), (401,), 'expired')
    assert stored(node, s) == b'original bytes'


def test_an_expiry_edited_to_live_longer_is_refused(node):
    """The expiry is in the clear; it must be covered by the signature or anyone could extend a session."""
    s = open_doc(node, 'alice')
    exp, sig = s['token'].split('.', 1)
    longer = '%d.%s' % (int(exp) + 10 ** 6, sig)
    assert_all_refused(every_route(node, s['id'], {'access_token': longer}), (401,), 'extended')


def test_another_persons_file_cannot_be_read_or_written_with_your_token(node):
    """Bob holds a perfectly valid token -- for HIS document. Pointed at Alice's file id it opens nothing."""
    alice = open_doc(node, 'alice', 'alice.docx', b'original bytes')
    bob = open_doc(node, 'bob', 'bob.docx', b'bob secret')
    results = every_route(node, alice['id'], {'access_token': bob['token']}, {'x-test-user': 'bob'})
    assert_all_refused(results, (401, 403, 404), "bob's token on alice's file")
    assert stored(node, alice) == b'original bytes' and version(node, alice) == '1'
    assert stored(node, bob) == b'bob secret'


def test_a_token_for_one_file_does_not_open_another_file_of_the_same_person(node):
    a = open_doc(node, 'alice', 'one.docx', b'original bytes')
    b = open_doc(node, 'alice', 'two.docx', b'second document')
    assert_all_refused(every_route(node, b['id'], {'access_token': a['token']}), (401,), 'token of one on two')
    assert stored(node, b) == b'second document'
    assert stored(node, a) == b'original bytes'


def test_a_file_id_that_is_not_a_session_is_refused(node):
    s = open_doc(node, 'alice')
    for fid in ('0' * 32, '..', '%2e%2e%2fmeta.json', s['id'].upper(), s['id'] + 'x'):
        tok = office._token(fid, int(time.time()) + 600)      # even a token the server itself signed for it
        assert_all_refused(every_route(node, fid, {'access_token': tok}), (401, 404, 405), 'id %r' % fid)


def test_a_closed_session_is_gone(node):
    s = open_doc(node, 'alice')
    assert node.c.delete('/client/office/session/%s' % s['id'], params={'access_token': s['token']}).status_code == 200
    assert_all_refused(every_route(node, s['id'], {'access_token': s['token']}), (404,), 'closed')


def test_an_owner_who_is_no_longer_a_member_is_refused(node):
    s = open_doc(node, 'alice')
    allow_keys(node.mp, BOB.nostr_npub)                        # Alice's membership is revoked mid-session
    assert_all_refused(every_route(node, s['id'], {'access_token': s['token']}), (403,), 'not a member')
    allow_keys(node.mp, ALICE.nostr_npub, BOB.nostr_npub)
    assert stored(node, s) == b'original bytes'


def test_a_read_only_session_cannot_be_written(node):
    s = open_doc(node, 'alice', mode='view')
    p = {'access_token': s['token']}
    assert node.c.get('/wopi/files/%s' % s['id'], params=p).json()['UserCanWrite'] is False
    r = node.c.post('/wopi/files/%s/contents' % s['id'], params=p, content=b'x', headers={'X-WOPI-Override': 'PUT'})
    assert r.status_code == 403
    assert stored(node, s) == b'original bytes' and version(node, s) == '1'


def test_a_put_under_somebody_elses_lock_is_refused_and_keeps_the_document(node):
    s = open_doc(node, 'alice')
    p = {'access_token': s['token']}
    assert node.c.post('/wopi/files/%s' % s['id'], params=p,
                       headers={'X-WOPI-Override': 'LOCK', 'X-WOPI-Lock': 'editor-1'}).status_code == 200
    r = node.c.post('/wopi/files/%s/contents' % s['id'], params=p, content=b'clobber',
                    headers={'X-WOPI-Override': 'PUT', 'X-WOPI-Lock': 'editor-2'})
    assert r.status_code == 409 and r.headers['x-wopi-lock'] == 'editor-1'
    assert stored(node, s) == b'original bytes' and version(node, s) == '1'


def test_a_put_the_storage_refuses_keeps_the_old_version(node):
    """Flow 4 at the server: a save the host cannot take answers an error status CODE reports as "not saved", and
    the document and its version are the old ones -- never a half-written file."""
    s = open_doc(node, 'alice')
    p = {'access_token': s['token']}
    node.mp.setattr(office, '_MAX', 8)
    r = node.c.post('/wopi/files/%s/contents' % s['id'], params=p, content=b'far too many bytes',
                    headers={'X-WOPI-Override': 'PUT'})
    assert r.status_code == 413
    assert stored(node, s) == b'original bytes' and version(node, s) == '1'
    assert sorted(x.name for x in (node.root / s['id']).iterdir()) == ['document', 'meta.json'], \
        'a refused save left a temporary file behind'


# --------------------------------------------------------------------------------------------- Save As / Rename
def snapshot(root):
    """Every file under the sessions directory and its bytes: 'nothing written' means this did not change."""
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob('*')) if p.is_file()}


@pytest.mark.parametrize('mode', ['edit', 'view'])
def test_check_file_info_tells_collabora_there_is_no_save_as_export_or_rename(node, mode):
    """WHAT CODE OFFERS IS DECIDED BY THESE FLAGS, measured with the shipped CODE 26.04.4 in
    tests/client/test_office_real_collabora.py: UserCanNotWriteRelative=false puts File > Save As, Export as and an
    editable document-name box on screen, and the name box (and the conflict dialog's "Save to new file") call
    PutRelativeFile directly -- storage this host does not have. SupportsRename+UserCanRename add File > Rename
    (RenameFile). With the fields ABSENT, coolwsd happened to default to the safe side; this states them instead of
    depending on a default CODE's own browser code reads the opposite way (`!!n.UserCanNotWriteRelative`)."""
    s = open_doc(node, 'alice', mode=mode)
    info = node.c.get('/wopi/files/%s' % s['id'], params={'access_token': s['token']}).json()
    assert info.get('UserCanNotWriteRelative') is True, info
    assert info.get('SupportsRename') is False and info.get('UserCanRename') is False, info


def relative_attempts():
    """(label, headers) of every PutRelativeFile / RenameFile CODE could send, hostile targets included."""
    put = {'X-WOPI-Override': 'PUT_RELATIVE', 'X-WOPI-Size': '14'}
    out = [('save as', {**put, 'X-WOPI-SuggestedTarget': 'Copy of report.docx'}),
           ('export pdf', {**put, 'X-WOPI-SuggestedTarget': '.pdf'}),
           ('exact target', {**put, 'X-WOPI-RelativeTarget': 'report.docx', 'X-WOPI-OverwriteRelativeTarget': 'true'})]
    for bad in ('../x.docx', '../../escaped.docx', '/tmp/pc-office-escaped.docx', '..\\x.docx', 'a/../../b.docx',
                '%2e%2e%2fx.docx', 'meta.json', 'x' * 5000 + '.docx', '.', '..', ''):
        out.append(('suggested %r' % bad[:40], {**put, 'X-WOPI-SuggestedTarget': bad}))
        out.append(('relative %r' % bad[:40], {**put, 'X-WOPI-RelativeTarget': bad}))
        out.append(('rename %r' % bad[:40], {'X-WOPI-Override': 'RENAME_FILE', 'X-WOPI-RequestedName': bad}))
    out.append(('rename', {'X-WOPI-Override': 'RENAME_FILE', 'X-WOPI-RequestedName': 'Renamed report'}))
    return out


def test_put_relative_and_rename_write_nothing_whatever_the_token_or_target(node, tmp_path):
    """Every PutRelativeFile / RenameFile -- with no token, a forged one, an expired one, another person's, and the
    RIGHT one with every target from an ordinary copy to '../x', an absolute path and a 5000-character name -- is
    refused, and afterwards the sessions directory is byte-for-byte what it was: no new session, no stray file, the
    document, its name and its version untouched, no file outside the directory, the lock still held."""
    alice = open_doc(node, 'alice', 'report.docx', b'original bytes')
    bob = open_doc(node, 'bob', 'bob.docx', b'bob secret')
    p = {'access_token': alice['token']}
    assert node.c.post('/wopi/files/%s' % alice['id'], params=p,
                       headers={'X-WOPI-Override': 'LOCK', 'X-WOPI-Lock': 'editor-1'}).status_code == 200
    before = snapshot(node.root)
    tokens = [('valid', alice['token'], 501), ('none', None, 401), ('forged', alice['token'][:-4] + '0000', 401),
              ('expired', office._token(alice['id'], int(time.time()) - 5), 401),
              ("bob's", bob['token'], 401)]
    answered = []
    for tname, tok, want in tokens:
        params = {'access_token': tok} if tok is not None else {}
        for label, headers in relative_attempts():
            r = node.c.post('/wopi/files/%s' % alice['id'], params=params, content=b'ATTACKER BYTES',
                            headers={**headers, 'X-WOPI-Lock': 'editor-1'})
            answered.append((tname, label, r.status_code))
            assert r.status_code == want, (tname, label, r.status_code, r.text[:120])
            assert b'original bytes' not in r.content and b'bob secret' not in r.content, (tname, label)
            assert 'Url' not in r.text and 'HostEditUrl' not in r.text, ('a refusal handed out a document URL',
                                                                         tname, label, r.text[:120])
    assert snapshot(node.root) == before, 'a refused Save As / Rename changed the sessions directory'
    for escaped in (tmp_path.parent / 'escaped.docx', tmp_path.parent / 'x.docx', Path('/tmp/pc-office-escaped.docx')):
        assert not escaped.exists(), ('a relative target escaped the sessions directory', escaped)
    info = node.c.get('/wopi/files/%s' % alice['id'], params=p).json()
    assert info['BaseFileName'] == 'report.docx' and info['Version'] == '1', info
    assert node.c.post('/wopi/files/%s' % alice['id'], params=p,
                       headers={'X-WOPI-Override': 'GET_LOCK'}).headers['x-wopi-lock'] == 'editor-1'
    assert len(answered) == len(tokens) * len(relative_attempts())
