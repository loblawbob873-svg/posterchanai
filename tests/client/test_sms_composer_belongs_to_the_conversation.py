"""The Texts composer belongs to the CONVERSATION — not to the render, and not to the module.

Reported as the text input field carrying the message of a different, incoming conversation.

`paint()` replaces `#feed` wholesale, and it runs on every incoming message, receipt, contact
refresh and relay event. The composer had one piece of state on each side of that line and each was
wrong in the opposite direction:

  * the staged FILE was module-wide (`S.attach`). Every deliberate way of leaving a thread called
    `clearAttachment()` by hand — the thread-list click, Back, composeNew, the contact landing — but
    `land()`, the notification for an incoming message, did not. So a photo staged for one person
    turned up in somebody else's composer already labelled "ready to send", and Send would put it on
    the carrier to THEM. Measured before the fix: `private-photo.jpg · ready to send` visible under
    the second conversation's message list.

  * the typed TEXT had no state at all — it lived only in the DOM — so the repaint that somebody
    ELSE caused by texting you threw away a half-typed reply. Measured before the fix: the input was
    empty after an unrelated message arrived.

The fix is the shape `S.scroll` and `S.sending` already use: one entry per thread key. Nothing has
to REMEMBER to clear anything on the way out, which is what made the old design fail at the one exit
nobody wrote a clear for.

These assertions are about the RENDERED composer after a real sequence of events — the shipped
sms.js, real DOM, real handlers, the real live-subscription callback and the real notification
`onClick`. They pin the rule, not the spelling: an implementation that keeps drafts some other way
passes as long as no conversation is ever shown a draft that was not typed in it.
"""
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import tempfile
import threading
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SMS = os.path.join(REPO, "static", "js", "client", "sms.js")

# Served over http://127.0.0.1 rather than file://, because a file page is not a secure context and
# the archive's document ids come from crypto.subtle — absent there, absorb() throws and the whole
# screen is empty for a reason that has nothing to do with the composer.
PAGE = r"""<!doctype html><meta charset="utf-8"><title>texts composer</title>
<div id="feed"></div><pre id="out">RUNNING</pre>
<script>
window.ICO = (n, cls) => '<svg class="ic ' + (cls||'') + '"></svg>';
const notifications = [];
const escapeHtml = s => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
window.Store = { query: () => [], put(){} };
let liveSub = null;
window.Relay = {
  query: async () => [],
  subscribe: (f, o) => { liveSub = o; return { close(){} }; },
  ready: async () => true,
  publish: async () => ({ok:true}),
};
window.__PC = {
  VIEW: 'texts',
  ME: { pubkey: 'me' },
  $: sel => document.querySelector(sel),
  enc: escapeHtml,
  toast(){}, notifToast(){},
  uiConfirm: async () => true,
  uiPrompt: async () => '',
  switchView(){},
  capPlugin: () => null,                    // a laptop: no handset plugin, so notifications fire
  osNotify: (title, body, opt) => { notifications.push({title, body, opt}); },
  nip44enc: async (pk, s) => 'enc:' + s,
  nip44dec: async (pk, ct) => String(ct).slice(4),
  publish: async () => ({ok:true}),
  gifEnabled: () => false,
  openEmojiPopover(){}, copyValue(){}, modal(){}, closeModal(){},
  filesIdx: () => null,
  encFileUrl: async sha => 'blob:' + sha,
  uploadEncFile: async () => 'a'.repeat(64),
  uploadSharedEnc: async () => 'https://example.invalid/blob',
};
</script>
<script src="/sms.js"></script>
<script>
const out = document.getElementById('out');
const log = {steps: []};
window.onerror = (m, src, line) => { log.steps.push('ONERROR ' + m + ' @' + line); };
const say = n => { log.steps.push(n); out.textContent = JSON.stringify(log, null, 1); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const DRAFT = 'the pin is 4821, do not tell anyone';
const chip = () => (document.querySelector('.sms-attachment-draft b') || {}).textContent || '';
const box  = () => document.querySelector('#sms-in').value;
const title = () => document.querySelector('.sms-title span').textContent;

function archived(d, addr, body, dateMs, incoming){
  return { id: d, kind: 30078, pubkey: 'me', created_at: Math.floor(dateMs/1000),
           tags: [['d', d], ['l', 'pcai-sms']],
           content: 'enc:' + JSON.stringify({address: addr, body, date: dateMs, incoming, name: ''}) };
}
function openThread(fragment){
  [...document.querySelectorAll('.sms-thread')].find(b => b.dataset.k.includes(fragment)).click();
}

(async () => {
  try{
    say('render'); await PCSms.render();          // the real cold load: watch() + load() + paint()
    const S = PCSms._state();
    const A = '+15550111', B = '+15550222', now = Date.now();
    await PCSms._absorb([ archived('pcai:sms:aaaaaaaaaaaaaaaaaaaaaaaa', A, 'hey', now - 60000, true),
                          archived('pcai:sms:bbbbbbbbbbbbbbbbbbbbbbbb', B, 'yo',  now - 50000, true) ]);
    PCSms.refreshNames();                          // rebuild + paint
    openThread('5550111');
    log.opened = title();

    /* ---- a draft in A: a photo staged through the real hidden input, and typed text ---------- */
    const dt = new DataTransfer();
    dt.items.add(new File([new Uint8Array([1,2,3])], 'private-photo.jpg', {type:'image/jpeg'}));
    const pick = document.querySelector('#sms-file');
    pick.files = dt.files; pick.dispatchEvent(new Event('change'));
    log.staged = chip();
    document.querySelector('#sms-in').value = DRAFT;
    document.querySelector('#sms-in').dispatchEvent(new Event('input', {bubbles:true}));

    /* ---- 1. an ordinary repaint of the SAME conversation ------------------------------------- */
    PCSms.refreshNames();                          // a contact refresh: one of paint()'s own causes
    log.repaint_text = box();
    log.repaint_chip = chip();

    /* ---- 2. a message arrives in a DIFFERENT conversation ------------------------------------ */
    log.live = !!liveSub;
    await liveSub.onEvent(archived('pcai:sms:cccccccccccccccccccccccc', B, 'are you there?', now, true));
    await sleep(600);                              // paintSoon's 250ms trailing coalesce
    log.incoming_text = box();
    log.incoming_chip = chip();
    log.incoming_thread = title();

    /* ---- 3. they tap the notification for that message ---------------------------------------- */
    const last = notifications[notifications.length - 1];
    log.notified = notifications.map(n => n.title + ' | ' + n.body);
    log.land_bound = !!(last && last.opt && last.opt.onClick);
    last.opt.onClick();
    await sleep(50);
    log.land_thread = title();
    log.land_text = box();
    log.land_chip = chip();

    /* ---- 4. and back to the conversation the draft was typed in ------------------------------ */
    document.querySelector('#sms-back').click();
    openThread('5550111');
    log.back_text = box();
    log.back_chip = chip();
    log.draft_keys = Object.keys(S.draft || {});
  }catch(e){ log.ERROR = String((e && e.stack) || e); }
  out.textContent = JSON.stringify(log, null, 1);
})();
</script>"""


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/sms.js"):
            body = open(SMS, "rb").read()
            kind = "application/javascript"
        else:
            body = PAGE.encode()
            kind = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SmsComposerBelongsToTheConversation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        chrome = (shutil.which("google-chrome-stable") or shutil.which("chromium")
                  or shutil.which("google-chrome") or shutil.which("chrome"))
        if not chrome:
            raise unittest.SkipTest("no chrome — these assertions are about the rendered composer")
        # Port 0 and a throwaway profile: this suite runs concurrently with the browser checks.
        socketserver.TCPServer.allow_reuse_address = True
        server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        profile = tempfile.mkdtemp(prefix="pcsmsdraft-")
        try:
            res = subprocess.run(
                [chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
                 "--user-data-dir=" + profile, "--virtual-time-budget=20000", "--dump-dom",
                 "http://127.0.0.1:%d/harness.html" % port],
                capture_output=True, text=True, timeout=300).stdout
        finally:
            server.shutdown()
            shutil.rmtree(profile, ignore_errors=True)
        m = re.search(r'<pre id="out">(.*?)</pre>', res, re.S)
        if not m or not m.group(1).strip():
            raise unittest.SkipTest("page did not evaluate")
        raw = m.group(1)
        for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
            raw = raw.replace(a, b)
        cls.r = json.loads(raw)
        assert "ERROR" not in cls.r, cls.r

    def test_the_scenario_actually_ran(self):
        """Every assertion below is vacuous if the conversation never opened or nothing was staged."""
        self.assertEqual(self.r["opened"], "+15550111", self.r)
        self.assertEqual(self.r["staged"], "private-photo.jpg",
                         "the photo was never staged, so nothing below is being measured: %r" % (self.r,))
        self.assertTrue(self.r["live"], "no live subscription — the incoming message is not real")
        self.assertTrue(self.r["land_bound"],
                        "the notification carries no onClick, so the tap is not being measured")

    def test_a_repaint_does_not_throw_away_what_you_are_typing(self):
        self.assertEqual(self.r["repaint_text"], "the pin is 4821, do not tell anyone",
                         "a repaint emptied the composer — paint() runs on every receipt and "
                         "contact refresh, so this is a draft lost to somebody else's event")
        self.assertEqual(self.r["repaint_chip"], "private-photo.jpg",
                         "the staged photo did not survive a repaint of its own conversation")

    def test_a_message_in_another_conversation_does_not_empty_this_one(self):
        self.assertEqual(self.r["incoming_thread"], "+15550111",
                         "an incoming message moved the screen off the open conversation")
        self.assertEqual(self.r["incoming_text"], "the pin is 4821, do not tell anyone",
                         "somebody else texting you deleted the reply you were typing")
        self.assertEqual(self.r["incoming_chip"], "private-photo.jpg")

    def test_the_draft_never_appears_against_another_conversation(self):
        """The report, exactly: the composer of the conversation you LAND on must be empty."""
        self.assertEqual(self.r["land_thread"], "+15550222",
                         "the notification did not open the conversation it was about")
        self.assertEqual(self.r["land_text"], "",
                         "the message typed for one person is sitting in another person's "
                         "composer — pressing Send there sends it to them")
        self.assertEqual(self.r["land_chip"], "",
                         "a photo staged for one person is armed and labelled 'ready to send' in "
                         "another person's conversation")

    def test_the_draft_is_still_waiting_where_it_was_typed(self):
        self.assertEqual(self.r["back_text"], "the pin is 4821, do not tell anyone")
        self.assertEqual(self.r["back_chip"], "private-photo.jpg")

    def test_drafts_are_kept_per_conversation_and_only_where_there_is_one(self):
        """One entry, for the one conversation something was typed in.

        Keyed state is what makes the leak impossible rather than merely absent: a module-wide draft
        that happens to be cleared at every exit somebody remembered is one new exit away from this
        bug, which is how it shipped.
        """
        self.assertEqual(len(self.r["draft_keys"]), 1, self.r["draft_keys"])
        self.assertIn("5550111", self.r["draft_keys"][0])


class TheComposerHasNoModuleWideDraft(unittest.TestCase):
    """The source half: cheap, and it says WHY the browser half above can pass.

    Pinned as a shape and not as a call: any module-wide composer value would have to be read
    somewhere, and reading one is the defect.
    """

    def test_no_module_wide_staged_file(self):
        src = open(SMS, encoding="utf-8").read()
        # assertFalse on a bool, never assertNotIn on the source: a failure there prints the whole
        # 200 KB file and buries the six real failures above it.
        self.assertFalse("S.attach" in src,
                         "the staged file is module-wide again — it will follow the reader into "
                         "whatever conversation they open next")
        self.assertTrue("draft: Object.create(null)" in src,
                        "the composer's state is no longer keyed by conversation")


if __name__ == "__main__":
    unittest.main()
