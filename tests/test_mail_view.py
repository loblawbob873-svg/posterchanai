"""Email is its own view — and its notifications still work.

Run: venv-unified/bin/python -m unittest tests.test_mail_view

Email used to be the second TAB of Messages. The two share nothing but a metaphor: DMs are NIP-17
events on relays that this client decrypts, mail is IMAP through the instance. As a tab it could not
be opened from the phone's ☰ More sheet (that sheet switches VIEWS, and no view named the tab), it
could not be a window of its own in desktop mode (the launcher reads the sidebar's `data-view`
entries), and its tab bar cost a row of every phone screen to whichever half you were not in.

THE PART THAT MUST NOT BREAK is the notification path, which is the only way anyone learns mail has
arrived while they are elsewhere: the sync poller raises a card, an OS notification and a badge, and
each of those has to LAND on the mail view now rather than on the Messages tab that no longer
exists. A dead click target here is silent — the card still appears, it just takes you to DMs.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static" / "js" / "client" / "app.js").read_text()
OS_JS = (ROOT / "static" / "js" / "client" / "os.js").read_text()
SHELL = (ROOT / "templates" / "client.html").read_text()
CSS = (ROOT / "static" / "css" / "client.css").read_text()


class ViewTests(unittest.TestCase):
    def test_mail_is_a_view_the_router_knows(self):
        self.assertIn("if (VIEW==='mail') return renderMailView();", APP)
        self.assertIn("function renderMailView()", APP)
        self.assertIn("mail:'Email ✉️'", APP, "the view has no title in the header map")

    def test_it_gets_the_full_height_layout(self):
        """A mail client has its own scrolling panes; inside the timeline's scroll container it
        would grow the page instead of filling it.

        WHAT MATTERS IS THAT `mail` IS IN THE TOGGLE, not the exact spelling of the line. This
        asserted the whole literal, so adding another view to the same toggle (`concord`) broke it —
        reported as Email losing its layout when nothing about Email had changed."""
        import re
        m = re.search(r"feed\.classList\.toggle\('feed-dm',([^)]*)\)", APP)
        self.assertIsNotNone(m, "the feed-dm toggle moved — re-point this test")
        self.assertIn("VIEW==='mail'", m.group(1).replace(" ", "").replace('"', "'"),
                      "mail is not in the feed-dm toggle, so it renders inside the timeline's "
                      "scroll container and grows the page instead of filling it")

    def test_the_mount_guard_survived_the_move(self):
        """Remounting restarts a full IMAP sync. That render loop hammering /sync is the documented
        cause of the incident this feature was once removed for."""
        fn = APP[APP.index("function renderMailView()"):]
        fn = fn[:fn.index("\n  }")]
        self.assertIn("if(mounted && Mail.root===mounted) return;", fn)

    def test_messages_is_dms_only_now(self):
        for gone in ("_msgTab", "_msgTabBar", "_bindMsgTabs", "_msgShowing"):
            self.assertNotIn(gone, APP, f"{gone} is left over from the Email tab")
        self.assertNotIn("msg-tabs", CSS, "the tab bar's CSS outlived the tab bar")

    def test_the_mail_keys_are_released_when_the_view_is_left(self):
        """j/k/Enter act on the mail list. Left bound they drive a screen that is gone — the tab
        used to release them, and the tab is what was removed."""
        self.assertIn("if(VIEW!=='mail' && _mailKeysOff) _mailKeysOff();", APP)

    def test_email_needs_an_instance(self):
        """IMAP/SMTP run on the node. In a server-less bundle the screen would have nothing behind
        it, so it is gated with the rest of the instance features."""
        block = APP[APP.index("const INSTANCE_VIEWS"):APP.index("const INSTANCE_SETTINGS_TABS")]
        self.assertIn("'mail'", block)


class NotificationTests(unittest.TestCase):
    """The whole point of the feature: learning that mail arrived while you were somewhere else."""

    def _sync(self):
        i = APP.index("    async sync(manual){")
        return APP[i:APP.index("\n    },", i)]

    def _login_sync(self):
        i = APP.index("    async loginSync(){")
        return APP[i:APP.index("\n    },", i)]

    def test_the_poller_still_raises_a_card(self):
        for body, name in ((self._sync(), "sync"), (self._login_sync(), "loginSync")):
            self.assertIn("new email", body, f"{name} no longer notifies")
            self.assertIn("notifToast", body, f"{name}'s notification is not a notification card")

    def test_every_card_opens_the_mail_view(self):
        for body, name in ((self._sync(), "sync"), (self._login_sync(), "loginSync")):
            self.assertIn("switchView('mail')", body,
                          f"{name}'s notification does not open Email — it lands on DMs")
            self.assertNotIn("switchView('messages')", body,
                             f"{name} still opens Messages, where there is no mail")

    def test_new_mail_raises_an_os_notification_too(self):
        """A toast inside the app cannot reach someone whose window is behind another one — which is
        the case the notification exists for. A DM already does this; mail did not."""
        body = self._sync()
        self.assertIn("osNotify('📧 New email'", body)
        self.assertIn("tag:'pc-mail'", body)
        self.assertIn("onClick:()=>switchView('mail')", body)

    def test_the_badge_is_emails_own(self):
        """It used to bump the DM badge, which said "you have messages" and then showed a screen
        with no new messages on it."""
        self.assertIn("function bumpMail()", APP)
        self.assertIn("#mail-badge", APP)
        self.assertNotIn("if(_msgTab==='email') {} else bumpDm();", APP)

    def test_opening_email_clears_it(self):
        fn = APP[APP.index("function renderMailView()"):]
        fn = fn[:fn.index("\n  }")]
        self.assertIn("Mail.unread=0; bumpMail();", fn)

    def test_polling_is_untouched(self):
        """The count only exists because something keeps asking. Both entry points are wired at
        LOGIN, not by opening a view, so separating the view must not have moved them."""
        self.assertIn("Mail.loginSync();", APP)
        self.assertIn("Mail.startPolling();", APP)


class SurfaceTests(unittest.TestCase):
    def test_classic_mode_lists_email(self):
        """Email is a row of its own in the sidebar, and it is inside the OFFICE group.

        It used to have to sit directly under Messages, on the reasoning that the two are one
        metaphor. That was superseded deliberately: Contacts, Calendar and Email are one thing to
        reach for and were three rows apart, so they were grouped. The rule that remains — and the
        one the original test was really protecting — is that Email is its own row and not a tab
        inside Messages, which is how it was invisible on a phone and could not be a window of its
        own in desktop mode.

        The pattern allows `nav-item sub` because a grouped row carries it."""
        nav = [m.group(1) for m in re.finditer(r'class="nav-item[^"]*"[^>]*data-view="([a-z0-9]+)"',
                                               SHELL)]
        self.assertIn("mail", nav)
        self.assertIn("messages", nav)
        office = SHELL[SHELL.index('id="office-sub"'):]
        office = office[:office.index("</div>")]
        for v in ("contacts", "calendar", "mail"):
            self.assertIn('data-view="%s"' % v, office,
                          "%s belongs in the Office group" % v)

    def test_the_two_do_not_share_an_icon(self):
        """They did — both were the envelope — which is confusing the moment they are two rows."""
        msg = re.search(r'data-view="messages"><svg class="ic"><use href="#(i-[a-z0-9-]+)"', SHELL)
        mail = re.search(r'data-view="mail"><svg class="ic"><use href="#(i-[a-z0-9-]+)"', SHELL)
        self.assertTrue(msg and mail)
        self.assertNotEqual(msg.group(1), mail.group(1))

    def test_email_has_its_own_badge_element(self):
        self.assertIn('id="mail-badge"', SHELL)

    def test_mobile_more_sheet_offers_it(self):
        items = APP[APP.index("    const items=[['ai','ai','PosterChan AI']"):]
        items = items[:items.index("\n")]
        self.assertIn("['mail','mail','Email']", items)

    def test_the_more_sheet_shows_the_count(self):
        self.assertIn("counts={drafts:dn, mail:(Number(Mail && Mail.unread)||0)}", APP)

    def test_nostr_only_hides_it(self):
        """No instance, no IMAP. Hidden in the template AND filtered out of the phone sheet."""
        # `nav-item sub` since Email joined the Office group — the gate is what this asserts.
        self.assertIn('{% if not nostr_only %}<button class="nav-item sub" data-view="mail"', SHELL)
        self.assertIn("!(window.PC_NOSTR_ONLY && v==='mail')", APP)

    def test_desktop_mode_reaches_the_mail_app(self):
        """Desktop mode builds its launcher FROM the sidebar, so the icon is free — but the two
        places that used to open the mailbox by opening Messages are not."""
        self.assertIn("openApp('mail')", OS_JS)
        executable = re.sub(r"/\*.*?\*/|//[^\n]*", "", OS_JS, flags=re.S)
        self.assertNotIn("openApp('messages')", executable)
        self.assertIn("if(view === 'mail'){", OS_JS,
                      "the tray's unread-mail acknowledgement still keys on Messages")


if __name__ == "__main__":
    unittest.main()
