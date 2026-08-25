import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.routers import office


class Upload:
    def __init__(self, name, data): self.filename, self.data = name, data
    async def read(self, _limit): return self.data


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "_ROOT", Path(tmp_path))
    monkeypatch.setenv("POSTERCHANAI_OFFICE", "1")

    async def action(_ext, _mode):
        return "http://office:9980/browser/hash/cool.html?"

    monkeypatch.setattr(office, "_action_url", action)
    return Request({"type": "http", "method": "POST", "path": "/", "scheme": "http",
                    "headers": [(b"host", b"local")], "server": ("local", 80)})


def test_session_is_capability_protected_and_uses_public_proxy(tmp_path, monkeypatch):
    request = _setup(tmp_path, monkeypatch)
    request = Request({**request.scope, "scheme":"https",
        "headers":[(b"host",b"local"),(b"x-forwarded-proto",b"https"),(b"x-forwarded-host",b"cloud.example")]})
    upload = Upload("report.docx", b"original")
    session = asyncio.run(office.create_session(request, upload, "edit"))
    assert session["editor_url"].startswith("https://cloud.example/office-code/browser/")
    with pytest.raises(HTTPException) as bad:
        office.check_file_info(session["id"], request, "wrong")
    assert bad.value.status_code == 401
    info = office.check_file_info(session["id"], request, session["token"])
    assert info["BaseFileName"] == "report.docx"
    assert info["UserCanWrite"] is True


def test_wopi_lock_save_and_download(tmp_path, monkeypatch):
    request = _setup(tmp_path, monkeypatch)
    session = asyncio.run(office.create_session(request, Upload("sheet.xlsx", b"v1"), "edit"))
    assert office.file_operation(session["id"], session["token"], "LOCK", "a", "").status_code == 200
    conflict = office.file_operation(session["id"], session["token"], "LOCK", "b", "")
    assert conflict.status_code == 409
    assert conflict.headers["x-wopi-lock"] == "a"
    class Body:
        async def body(self): return b"v2"
    saved = asyncio.run(office.put_file(session["id"], Body(), session["token"], "a"))
    assert saved.status_code == 200
    assert (Path(tmp_path) / session["id"] / "document").read_bytes() == b"v2"


def test_disabled_and_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "_ROOT", Path(tmp_path))
    monkeypatch.setenv("POSTERCHANAI_OFFICE", "0")
    request = Request({"type":"http","method":"POST","path":"/","scheme":"http","headers":[],"server":("local",80)})
    with pytest.raises(HTTPException) as off:
        asyncio.run(office.create_session(request, Upload("a.docx", b"x"), "edit"))
    assert off.value.status_code == 404
    monkeypatch.setenv("POSTERCHANAI_OFFICE", "1")
    monkeypatch.setattr(office, "_MAX", 2)
    with pytest.raises(HTTPException) as big:
        asyncio.run(office.create_session(request, Upload("a.docx", b"xxx"), "edit"))
    assert big.value.status_code == 413


def test_distribution_wiring_is_present():
    root = Path(__file__).parents[1]
    compose = (root / "docker-compose.yml").read_text()
    install = (root / "install.sh").read_text()
    nginx = (root / "nginx/posterchanai.conf.example").read_text()
    client = (root / "static/js/client/app.js").read_text()
    # NO PROFILE. The office editor is part of the normal bring-up now, the same way it is part of
    # the normal ./install.sh — behind a profile it was opt-in twice over (you had to know the
    # profile existed AND set POSTERCHANAI_OFFICE), and the 📝 button is hidden when the editor is
    # absent, so a stack without it simply had no office and nothing said why.
    assert 'profiles: ["office"]' not in compose, (
        "the office service is profile-gated again, so a normal `docker compose up` brings up "
        "everything except the editor")
    assert 'collabora/code:' in compose
    assert 'POSTERCHANAI_OFFICE=${POSTERCHANAI_OFFICE:-1}' in compose, (
        "the container runs but the client is told the editor is off")
    assert '"--office"' in install
    # …and it also runs in the DEFAULT install, not only behind that flag.
    assert 'setup_office_server ||' in install, (
        "office is no longer installed by a plain ./install.sh, so a fresh node has no editor")
    assert "location ^~ /office-code/" in nginx
    assert "openOfficeFile" in client and "Save to Files" in client


# ---------------------------------------------------------------------------------------------
# THE SERVICE ROOT, which three separate files have to agree about.
#
# "opening office document is whiote screen" + four console lines of the shape
#   Loading failed for the <script> with source "https://poster.place/browser/<hash>/global.js"
#
# coolwsd writes its OWN absolute URLs: the <script src> tags inside cool.html (via %SERVICE_ROOT%),
# the editing WebSocket, and the discovery `urlsrc`. Told nothing, it writes them at the site ROOT.
# nginx was stripping /office-code on the way in, so cool.html was served perfectly and then asked
# for /browser/<hash>/global.js at the top level — which is not a route this site has. A white
# document frame, four 404s in the console, and nothing server-side to say so.
#
# So all three halves have to say the same thing, and nothing else checks that they do.

def _read(rel):
    return (Path(__file__).resolve().parent.parent / rel).read_text(encoding="utf-8")


def test_coolwsd_is_told_the_sub_path_it_is_published_under():
    """Without this, every URL CODE writes about itself points at the site root."""
    want = f"--o:net.service_root={office._SERVICE_ROOT}"
    for rel in ("posterchanai-office.service", "scripts/install/office.sh"):
        assert want in _read(rel), f"{rel} starts CODE without a service root"


def test_nginx_passes_the_prefix_through_instead_of_stripping_it():
    """The other half. With service_root set, CODE both serves AND advertises under the prefix, so
    a proxy that rewrites it away turns every one of its own URLs into a 404."""
    for rel in ("nginx/posterchanai.conf.example", "docker/proxy/posterchanai.conf"):
        conf = _read(rel)
        i = conf.index("location ^~ /office-code/")
        block = conf[i:conf.index("\n    }", i)]
        assert "rewrite" not in block, (
            f"{rel} still strips the prefix CODE now writes into its own asset URLs")
        assert "proxy_pass" in block


def test_office_can_be_embedded_only_by_our_web_and_packaged_clients():
    """Collabora knows the public host but not Electron/Capacitor's local origins. Appending another
    policy cannot loosen CSP—the browser intersects both—so the upstream header must be hidden and
    replaced with an explicit ancestor list. Never reflect Origin here: that makes WOPI clickjacking
    protection equivalent to no protection at all."""
    for rel in ("nginx/posterchanai.conf.example", "docker/proxy/posterchanai.conf"):
        conf = _read(rel)
        i = conf.index("location ^~ /office-code/")
        block = conf[i:conf.index("\n    }", i)]
        assert "proxy_hide_header Content-Security-Policy" in block
        policy = block.split('add_header Content-Security-Policy "', 1)[1].split('" always;', 1)[0]
        ancestors = policy.split("frame-ancestors ", 1)[1]
        for origin in ("'self'", "app:", "capacitor:", "https://localhost", "https://localhost:*",
                       "http://localhost"):
            assert origin in ancestors, f"{rel} excludes {origin}"
        assert "$http_origin" not in block
        assert "frame-ancestors *" not in policy
        assert "script-src 'self' 'unsafe-eval'" in policy


def test_the_service_root_is_joined_exactly_once(tmp_path, monkeypatch):
    """Doubling it produces /office-code/office-code/browser/… — a 404 that looks nothing like its
    cause. Adding it when CODE did not is what keeps this working against an office server that has
    not been restarted yet, so BOTH shapes must land on the same URL."""
    root = office._SERVICE_ROOT
    for advertised in (f"http://office:9980{root}/browser/hash/cool.html?",   # service_root set
                       "http://office:9980/browser/hash/cool.html?"):         # not yet restarted
        request = _setup(tmp_path, monkeypatch)

        async def action(_ext, _mode, _u=advertised):
            return _u

        monkeypatch.setattr(office, "_action_url", action)
        out = asyncio.run(office.create_session(request, Upload("a.docx", b"x"), "edit"))
        url = out["editor_url"]
        assert f"{root}/browser/hash/cool.html" in url, url
        assert root * 2 not in url, f"the service root was added twice: {url}"


def test_discovery_is_asked_under_the_service_root_first():
    """A configured service_root moves discovery too, including on loopback. Asking the bare path
    first would take the 4xx as 'office unavailable' on a correctly configured node."""
    src = _read("app/routers/office.py")
    body = src[src.index("async def _discover("):src.index("async def _action_url(")]
    assert "_SERVICE_ROOT" in body, "discovery ignores the service root"
    assert 'for root in (_SERVICE_ROOT, "")' in body, (
        "the bare path is tried first, so a correctly configured node's 4xx under the root would "
        "be reported as 'office unavailable'")


def test_a_pdf_is_accepted():
    """"pdf: unsuported office document" - refused by a gate in front of a server that opens PDFs
    in Draw, with a message that blamed the document."""
    assert "pdf" in office._EXTS_FALLBACK


def test_the_client_never_offers_what_the_server_would_refuse():
    """THE BUG, GENERALISED. Two hand-written lists - one in the page, one in the router - and the
    page's had five extensions the router did not. Every one of them is a button that produces an
    error naming the file. The page's list must be a SUBSET: the server may accept more (it asks
    CODE, which advertises ninety-odd), never fewer."""
    import re as _re
    app = _read("static/js/client/app.js")
    m = _re.search(r"const _OFFICE_EXT = /\\\.\(([^)]*)\)\$/i;", app)
    assert m, "_OFFICE_EXT moved - re-point this test"
    offered = set(m.group(1).split("|"))
    extra = offered - office._EXTS_FALLBACK
    assert not extra, f"the page offers the office editor for {sorted(extra)}, which it would refuse"


def test_the_accepted_set_is_asked_of_code_not_kept_here():
    """The static set is the fallback for a CODE that cannot be reached, not the answer."""
    src = _read("app/routers/office.py")
    body = src[src.index("async def _accepted_exts("):src.index("def enabled(")]
    assert "_discover(" in body, "the accepted set is not asked of CODE"
    assert "_EXTS_FALLBACK" in body, "there is no fallback for an unreachable CODE"
    assert "if found:" in body, "an empty discovery would be taken as 'nothing is supported'"


def test_a_format_advertised_only_under_view_comment_still_opens(monkeypatch):
    """CODE advertises pdf under exactly ONE action - `view_comment`, its annotation mode. Asking
    for "edit" and falling back to "view" found neither and reported "CODE does not advertise
    support for .pdf" about a format sitting right there in the discovery document."""
    disco = (b'<?xml version="1.0"?><wopi-discovery><net-zone>'
             b'<app name="Capabilities"><action ext="pdf" name="view_comment" '
             b'urlsrc="http://code/browser/h/cool.html?"/></app>'
             b'</net-zone></wopi-discovery>')

    class R:
        content = disco
        def raise_for_status(self): pass

    async def disc(_client): return R()

    monkeypatch.setattr(office, "_discover", disc)
    url = asyncio.run(office._action_url("pdf", "edit"))
    assert url == "http://code/browser/h/cool.html?"


def test_edit_still_wins_where_a_format_offers_it(monkeypatch):
    """The chain must not demote a .docx into a viewer just because the list grew."""
    disco = (b'<?xml version="1.0"?><wopi-discovery><net-zone><app name="x">'
             b'<action ext="docx" name="view" urlsrc="http://code/view?"/>'
             b'<action ext="docx" name="edit" urlsrc="http://code/edit?"/>'
             b'</app></net-zone></wopi-discovery>')

    class R:
        content = disco
        def raise_for_status(self): pass

    async def disc(_client): return R()

    monkeypatch.setattr(office, "_discover", disc)
    assert asyncio.run(office._action_url("docx", "edit")) == "http://code/edit?"
    assert asyncio.run(office._action_url("docx", "view")) == "http://code/view?"


def test_mobile_office_is_an_edge_to_edge_workspace():
    css = _read("static/css/client.css")
    block = css[css.index("@media(max-width:900px)"):]
    assert ".modal.office-modal{position:fixed;inset:0" in block
    assert "width:100vw" in block and "height:100dvh" in block
    assert "safe-area-inset-bottom" in block
