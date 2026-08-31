"""Sent must mean the server's Sent mailbox and must catch up when opened."""
import unittest
from pathlib import Path


APP = (Path(__file__).resolve().parents[2] / "static/js/client/app.js").read_text(encoding="utf-8")
SYNC = (Path(__file__).resolve().parents[2] / "app/services/mail_sync.py").read_text(encoding="utf-8")
ROUTER = (Path(__file__).resolve().parents[2] / "app/routers/mail.py").read_text(encoding="utf-8")


class SentFolderFreshness(unittest.TestCase):
    def test_server_sent_mapping_replaces_bootstrap_placeholder(self):
        block = APP[APP.index("    async loadFolders(){"):APP.index("    async selectFolder(f){")]
        self.assertIn("const resolvedSent=r.sent||''", block)
        self.assertIn("this.folder=resolvedSent", block)
        self.assertIn("this.refreshFolder(resolvedSent)", block)

    def test_opening_every_remote_folder_refreshes_it(self):
        block = APP[APP.index("    async selectFolder(f){"):APP.index("    refreshFolder(f){")]
        self.assertIn("if(f!=='Drafts') this.refreshFolder(f)", block)
        self.assertNotIn("!['INBOX','Sent','Drafts'].includes", block)

    def test_logical_sent_is_resolved_at_server_boundary_too(self):
        block = SYNC[SYNC.index("async def sync_one("):SYNC.index("async def sync_all(")]
        self.assertIn('{"Sent": "sent"', block)
        self.assertIn("real = (meta.get(role) if role else None) or folder", block)
        self.assertIn("_sync_folder(db, user, seckey, owner_pk, acc, real", block)

    def test_unified_sent_refreshes_every_accounts_real_sent_folder(self):
        start = ROUTER.index("async def mail_sync_folder(")
        block = ROUTER[start:ROUTER.index("def _normsubj(", start)]
        self.assertIn('if d.get("account") == "__all"', block)
        self.assertIn("for item in (get_user_mail_accounts", block)
        self.assertIn("mail_sync.sync_one(", block)
        client = APP[APP.index("    refreshFolder(f){"):APP.index("    /* A one-line status")]
        self.assertNotIn("if(this.acct==='__all')", client)
        self.assertIn("body:JSON.stringify({account,folder:f})", client)

    def test_late_search_cannot_overwrite_a_folder_click(self):
        block = APP[APP.index("    async loadList(){"):APP.index("    /* The IMAP name of the Sent folder")]
        self.assertIn("const seq=++this._listSeq", block)
        self.assertIn("seq!==this._listSeq", block)
        self.assertIn("query!==this.q", block)

    def test_unified_sent_rows_show_the_recipient(self):
        block = APP[APP.index("    drawList(){"):APP.index("    updateBulk(){")]
        self.assertIn("this.folder==='Sent'||this.folderLabels[this.folder]==='📤 Sent'", block)
        self.assertIn("'To: '+(m.to||'')", block)

    def test_background_poll_is_prompt_and_visibility_uses_same_freshness_rule(self):
        self.assertIn("POLL_MS: 2 * 60 * 1000", APP)
        vis = APP[APP.index("document.addEventListener('visibilitychange'"):]
        vis = vis[:vis.index("    stopPolling()")]
        self.assertIn("this.refreshIfStale()", vis)


if __name__ == "__main__":
    unittest.main()
