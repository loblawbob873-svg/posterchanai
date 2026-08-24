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
