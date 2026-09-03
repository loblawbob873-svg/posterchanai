"""A sent copy must appear in the conversation that is already open."""
import asyncio
from pathlib import Path

from app.routers import mail as mail_router


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
ROUTER = (ROOT / "app/routers/mail.py").read_text(encoding="utf-8")


def test_mailbox_write_invalidates_account_and_unified_thread_snapshots():
    mail_router._THREAD_SCAN.clear()
    mail_router._THREAD_SCAN_GEN.clear()
    mail_router._THREAD_SCAN[(7, "me@example.com")] = (1, ["old account thread"])
    mail_router._THREAD_SCAN[(7, "*")] = (1, ["old unified thread"])
    mail_router._THREAD_SCAN[(8, "*")] = (1, ["another user"])
    mail_router._invalidate_thread_scan(7, "me@example.com")
    assert (7, "me@example.com") not in mail_router._THREAD_SCAN
    assert (7, "*") not in mail_router._THREAD_SCAN
    assert mail_router._THREAD_SCAN[(8, "*")] == (1, ["another user"])


def test_an_inflight_old_scan_cannot_restore_stale_mail(monkeypatch):
    mail_router._THREAD_SCAN.clear()
    mail_router._THREAD_SCAN_GEN.clear()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def scan(sk, account, folder):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
            return ["before send"]
        return ["before send", "my sent reply"]

    async def run():
        monkeypatch.setattr(mail_router.mail_store, "list_all_messages", scan)
        task = asyncio.create_task(mail_router._thread_scan(b"key", "me@example.com", 7))
        await started.wait()
        mail_router._invalidate_thread_scan(7, "me@example.com")
        release.set()
        return await task

    assert asyncio.run(run()) == ["before send", "my sent reply"]
    assert calls == 2


def test_folder_sync_always_invalidates_even_if_another_poll_stored_the_mail_first():
    body = ROUTER.split("async def mail_sync_folder(", 1)[1].split("import re as _re", 1)[0]
    assert "_invalidate_thread_scan(current_user.id, acc.email)" in body
    assert "if n:" not in body
    sync = ROUTER.split("async def mail_do_sync(", 1)[1].split("def _warm_thread_scan", 1)[0]
    assert "_invalidate_thread_scan(current_user.id, acc.email)" in sync


def test_sent_sync_reopens_the_current_conversation_after_the_copy_arrives():
    body = APP.split("    async syncSent(account){", 1)[1].split("    _key(m){", 1)[0]
    assert "await this.api('/sync-folder'" in body
    assert "this.root && this.root.isConnected" in body
    assert "this.open(this.openUid, this.openFolder" in body
    assert body.index("await this.api('/sync-folder'") < body.index("this.open(this.openUid")


def test_open_remembers_the_exact_seed_location_for_the_refresh():
    body = APP.split("    async open(uid, folder, account){", 1)[1][:1200]
    assert "this.openFolder=folder" in body
    assert "this.openAccount=acct" in body
