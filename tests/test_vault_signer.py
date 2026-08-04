"""The NIP-07 signer in the browser extension.

Every test here is a defect that was found in review before this shipped, and every one of them is
the same shape: the APPROVAL the user gave and the ACTION that happens are allowed to disagree. That
is the only thing a signer has to get right — it holds a key that can replace a contact list, empty
a wallet or post as the user, and the prompt is the whole of the user's control over it.

These run the real extension files under node, so a rewrite that keeps the comments and loses the
guard still fails.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXT = os.path.join(ROOT, "extension")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _node(script):
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, cwd=EXT, timeout=60)
    assert r.returncode == 0, r.stderr or r.stdout
    return r.stdout


def _src(name):
    with open(os.path.join(EXT, name), encoding="utf-8") as f:
        return f.read()


# --------------------------------------------------------------- the permission key

def _harness():
    """Just the permission-key machinery, lifted out of background.js by name.

    Extracted rather than reimplemented: a copy of _permKey in the test would pass forever while the
    real one collided.
    """
    src = _src("background.js")
    out = []
    for fn in ("_permKey", "_kindOf", "_cleanTags"):
        i = src.index("function %s(" % fn)
        depth, j = 0, src.index("{", i)
        k = j
        while True:
            if src[k] == "{":
                depth += 1
            elif src[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        out.append(src[i:k + 1])
    methods = src[src.index("const NOSTR_METHODS"):]
    methods = methods[:methods.index("]);") + 3]
    return "\n".join(out) + "\n" + methods + "\n"


def test_method_is_vetted_before_it_can_write_a_permission_key():
    """The one that turns a gibberish prompt into permanent silent contact-list signing.

    The key is `origin|method|kind`, so a page-supplied method containing a `|` writes an entry it
    does not own: approving "signEvent|3" — which the window describes as nothing in particular, and
    which fails as an unsupported call — stores exactly the permission that authorises every real
    kind-3 signature from then on, with no window ever again.
    """
    out = _node(_harness() + """
      const forged = _permKey('https://evil.com', 'signEvent|3', null);
      const real   = _permKey('https://evil.com', 'signEvent', 3);
      console.log(JSON.stringify({
        collides: forged === real,
        vetted: NOSTR_METHODS.has('signEvent|3'),
        known: NOSTR_METHODS.has('signEvent') && NOSTR_METHODS.has('nip44.decrypt'),
      }));
    """)
    r = json.loads(out)
    assert r["known"], "the real methods must still be allowed"
    # The collision itself is inherent to a flat key; what stops it is the method never getting there.
    assert not r["vetted"], "a forged method must not survive the allowlist"
    assert "if(!NOSTR_METHODS.has(method))" in _src("background.js"), \
        "the allowlist must be checked in handleNostr BEFORE _ask stores anything"


def test_the_allowlist_is_checked_before_the_prompt_opens():
    """Order matters: vetting after _ask still lets the forged key be written."""
    src = _src("background.js")
    body = src[src.index("async function handleNostr"):src.index("function _skBytes")]
    assert body.index("NOSTR_METHODS.has(method)") < body.index("await _ask("), \
        "an unknown method must be refused before a window can store a permission for it"


# --------------------------------------------------------------- the kind

@pytest.mark.parametrize("kind,ok", [
    (0, True), (1, True), (3, True), (30078, True),
    ("3", False),        # a string indexes KINDS the same as 3 but fails HEAVY.has
    ("0x3", False),      # reads as an unfamiliar number, `|0`s to 3 = contact list
    (3.5, False), (-1, False), (None, False), (True, False),
    ({}, False), ([3], False), (2 ** 32, False), ("constructor", False),
])
def test_only_a_real_integer_kind_is_accepted(kind, ok):
    """The prompt names a kind and the signer signs one; they must be the same value.

    `kind: "0x3"` showed "sign an event of kind 0x3" — an unfamiliar number a user might wave
    through — and then signed kind 3, replacing their contact list.
    """
    out = _node(_harness() + "console.log(JSON.stringify({k: _kindOf({kind: %s})}));"
                % json.dumps(kind))
    got = json.loads(out)["k"]
    assert (got is not None) == ok, "kind %r -> %r" % (kind, got)
    if ok:
        assert got == kind


def test_the_approval_window_agrees_with_itself_about_the_kind():
    """KINDS is an object (`"3"` finds it) and HEAVY is a Set (`"3"` does not).

    That disagreement made the window shout REPLACE YOUR CONTACT LIST while leaving the warning
    blank and "Remember this answer" ticked — one click, permanent silent access.
    """
    src = _src("approve.js")
    assert "Number.isInteger(req.kind) ? req.kind : -1" in src
    assert "HEAVY.has(kind)" in src and "HEAVY.has(req.kind)" not in src
    assert "hasOwnProperty.call(KINDS, kind)" in src, \
        "a kind of 'constructor' must not describe itself as native code"


# --------------------------------------------------------------- what gets signed

def test_a_signed_event_carries_only_the_canonical_fields():
    src = _src("background.js")
    body = src[src.index("case 'signEvent':"):src.index("case 'nip04.encrypt'")]
    assert "Object.assign" not in body, \
        "copy-and-delete keeps whatever else the site put in the object; build it field by field"
    for f in ("kind", "created_at", "tags", "content"):
        assert f in body
    assert "params.event.pubkey" not in body and "src.pubkey" not in body


def test_created_at_is_clamped_not_merely_defaulted():
    """A replaceable event dated in 2038 outranks every genuine update the user makes afterwards.

    With one remembered kind-3 approval that is a contact list frozen forever, and nothing in the
    app can fix it — a later, honest event has the lower created_at.
    """
    src = _src("background.js")
    body = src[src.index("case 'signEvent':"):src.index("case 'nip04.encrypt'")]
    assert "Math.abs(at - now) > 900" in body


def test_tags_are_strings_and_bounded():
    out = _node(_harness() + """
      console.log(JSON.stringify(_cleanTags([
        ['e', 1, null, {a:1}],
        'not-an-array',
        [],
        ['p', 'abc'],
      ])));
    """)
    assert json.loads(out) == [["e", "1", "", "[object Object]"], ["p", "abc"]]


# --------------------------------------------------------------- the prompt

def test_the_preview_shows_tags_because_the_dangerous_kinds_have_no_content():
    """A zap request's amount and payee, a deletion's targets and a follow list are all tags.

    Showing `content` alone means the prompt for "REQUEST A PAYMENT from your wallet" is an empty
    box — a rubber stamp with extra steps.
    """
    src = _src("background.js")
    body = src[src.index("function _preview("):]
    body = body[:body.index("\n}")]
    assert "_cleanTags(ev.tags)" in body
    assert "'amount'" in body and "'relay'" in body


# --------------------------------------------------------------- reachability

def test_the_page_cannot_reach_the_approval_flow():
    """The bridge and the approval messages share one onMessage listener.

    content.js hardcodes type:'nostr', so a page cannot name another case today — but the guard is
    what keeps a future edit to the bridge from turning "a site asked" into "the user allowed".
    """
    src = _src("background.js")
    assert "function _fromApproval(sender)" in src
    assert "sender.url.startsWith(B.runtime.getURL('approve.html'))" in src
    for case in ("_pendingApproval(msg.id, sender)", "_answerApproval(msg, sender)"):
        assert case in src, "the approval handlers must see the sender"
    # The popup-only handlers must refuse a tab too.
    perms = src[src.index("case 'nostr-perms':"):src.index("case 'approve-ask':")]
    assert perms.count("sender.tab") == 2


def test_the_content_script_relays_nothing_but_a_nostr_call():
    src = _src("content.js")
    bridge = src[src.index("__pcnostr !== 'req'"):src.index("const PW_SEL")]
    assert "type:'nostr'" in bridge
    assert "d.type" not in bridge, "the page must not choose which handler it reaches"


def test_the_provider_is_injected_inline_so_the_install_uuid_stays_private():
    """`moz-extension://<uuid>/inject.js` in the page DOM is a cross-site supercookie.

    The UUID is per-install and stable; any page can read it off the injected node and use it to
    recognise this browser everywhere, and to know the extension is installed.
    """
    assert "web_accessible_resources" not in json.loads(_src("manifest.json")), \
        "nothing needs to be page-reachable once the provider is injected inline"
    src = _src("content.js")
    assert "getURL('inject.js')" not in src
    assert "'(' + __pcNostrProvider + ')();'" in src


def test_the_provider_defines_window_nostr_and_holds_no_key():
    """inject.js runs in the page's world, so it is the only part of the extension a site can touch."""
    out = _node("""
      const fs = require('fs');
      eval(fs.readFileSync('inject.js', 'utf8'));
      const src = '(' + __pcNostrProvider + ')();';
      const posted = [];
      global.window = {
        addEventListener(){},
        postMessage(m){ posted.push(m); },
        // stand in for the page: no defineProperty support needed beyond the real one
      };
      global.setTimeout = () => 0;
      global.Object.defineProperty(global.window, 'nostr', {value: undefined, writable: true,
                                                            configurable: true});
      (0, eval)(src.replace('window.addEventListener', 'global.window.addEventListener'));
      const n = global.window.nostr;
      n.signEvent({kind: 1, content: 'hi'});
      console.log(JSON.stringify({
        methods: Object.keys(n).sort(),
        nip: Object.keys(n.nip44).sort(),
        posted: posted[0],
        leaksKey: /\\bsk\\b|nsec|privateKey/.test(src),
      }));
    """)
    r = json.loads(out)
    assert r["methods"] == ["getPublicKey", "getRelays", "nip04", "nip44", "signEvent"]
    assert r["nip"] == ["decrypt", "encrypt"]
    assert r["posted"]["__pcnostr"] == "req" and r["posted"]["method"] == "signEvent"
    assert not r["leaksKey"], "the page-world half must never see a key"


# --------------------------------------------------------------- windows

def test_android_gets_a_prompt_at_all():
    """Firefox for Android has no `windows` API, and this extension declares Android support.

    Without the tab fallback, windows.create throws, the catch returns 'deny', and every NIP-07 call
    on a phone is refused with no prompt ever appearing — indistinguishable from a broken signer.
    """
    src = _src("background.js")
    body = src[src.index("async function _ask("):src.index("function _fromApproval")]
    assert "B.windows && B.windows.create" in body
    assert "B.tabs.create({ url })" in body
    assert "gecko_android" in json.loads(_src("manifest.json")).get("browser_specific_settings", {})


def test_prompts_are_capped_per_origin():
    """`for(;;) window.nostr.signEvent(...)` is otherwise a browser-window flood, no gesture needed."""
    body = _src("background.js")
    body = body[body.index("async function _ask("):body.index("function _fromApproval")]
    assert "_inflight" in body and "return 'deny'" in body


def test_a_timed_out_prompt_closes_itself():
    """A window that outlived its request looks live and answers nothing."""
    body = _src("background.js")
    body = body[body.index("async function _ask("):body.index("function _fromApproval")]
    assert "shut()" in body, "the abandoned window must be removed, not left on screen"
    assert "clearTimeout(timer)" in body


def test_the_signer_refuses_a_read_only_pairing():
    src = _src("background.js")
    assert "cfg.mode === 'full' && cfg.sk" in src


def test_permissions_can_be_reviewed_and_revoked():
    """A remembered allow signs with no window forever; unpairing must not be the only way out."""
    assert "case 'nostr-forget'" in _src("background.js")
    assert "paintSites" in _src("popup.js")
    assert 'id="pane-sites"' in _src("popup.html")


def test_the_new_files_are_in_the_build():
    build = _src("build.sh")
    for f in ("inject.js", "approve.html", "approve.js"):
        assert f in build, "%s would be missing from the packaged extension" % f
