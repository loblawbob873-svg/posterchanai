"""Which site the phone thinks it is on, and which logins it offers there.

This is the rule that decides whether a saved password is handed to the right site, and on Android
it had no test at all — it lived inside a recursive walk over AssistStructure, which cannot be built
outside a device. VaultMatch is the same rule with the Android taken out, so javac can run it.

The bug that prompted this: the walk kept the FIRST webDomain it met in tree order, and tree order
is decided by a page's third-party iframes. A site whose analytics frame sorted ahead of its login
form convinced the phone it was on the analytics domain — no match, nothing offered, and it read as
"autofill just doesn't work on that one site" while the same login filled fine on the desktop.
"""
import os
import re
import shutil
import subprocess
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java",
                   "place", "poster", "app", "vault", "VaultMatch.java")

pytestmark = pytest.mark.skipif(shutil.which("javac") is None, reason="no JDK")


_RUNS = [0]


def _run(body, tmp_path):
    """Compile the real VaultMatch alongside a harness, run it, return stdout.

    Each call gets its own directory: a test that calls this more than once (several field shapes in
    one assertion) would otherwise collide on mkdir.
    """
    _RUNS[0] += 1
    tmp_path = tmp_path / ("r%d" % _RUNS[0])
    pkg = tmp_path / "place" / "poster" / "app" / "vault"
    pkg.mkdir(parents=True)
    shutil.copy(SRC, pkg / "VaultMatch.java")
    harness = tmp_path / "Harness.java"
    harness.write_text(
        "import place.poster.app.vault.VaultMatch;\n"
        "import java.util.*;\n"
        "public class Harness {\n"
        "  static List<String> L(String... a){ return Arrays.asList(a); }\n"
        "  public static void main(String[] a) throws Exception {\n"
        + textwrap.indent(textwrap.dedent(body), "    ")
        + "\n  }\n}\n")
    r = subprocess.run(["javac", "-d", str(tmp_path / "out"),
                        str(pkg / "VaultMatch.java"), str(harness)],
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr
    r = subprocess.run(["java", "-cp", str(tmp_path / "out"), "Harness"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_a_third_party_iframe_does_not_decide_which_site_you_are_on(tmp_path):
    """The page's own domain must survive an iframe that reported first.

    fieldDomain is tried first because a login really can live in an SSO iframe — but a MISS there
    has to fall through to the page, not end the search.
    """
    out = _run("""
        // the captcha frame enclosed the field; the address bar says blackhillsenergy.com
        List<String> c = VaultMatch.hostCandidates("google.com", "www.blackhillsenergy.com");
        System.out.println(c);
        System.out.println(VaultMatch.bestRank(L("blackhillsenergy.com"), L("blackhillsenergy.com"), c));
    """, tmp_path)
    assert out.splitlines()[0] == "[google.com, blackhillsenergy.com]"
    assert out.splitlines()[1] == "2", "the page's own exact match must still win"


def test_the_field_domain_is_preferred_when_it_matches(tmp_path):
    """A login genuinely hosted on an identity provider fills with the IdP's saved entry."""
    out = _run("""
        List<String> c = VaultMatch.hostCandidates("login.microsoftonline.com", "contoso.com");
        System.out.println(VaultMatch.rank(L("login.microsoftonline.com"), L("microsoftonline.com"), c.get(0)));
        System.out.println(c.get(0));
    """, tmp_path)
    assert out.splitlines() == ["2", "login.microsoftonline.com"]


def test_no_domain_at_all_offers_nothing(tmp_path):
    """A native app hands a package name, never a URL — and a guess would render as a real match."""
    out = _run("""
        List<String> c = VaultMatch.hostCandidates("", "");
        System.out.println(c.size());
        System.out.println(VaultMatch.bestRank(L("paypal.com"), L("paypal.com"), c));
    """, tmp_path)
    assert out.splitlines() == ["0", "0"]


@pytest.mark.parametrize("host,stored,want", [
    ("blackhillsenergy.com", "blackhillsenergy.com", 2),
    ("www.blackhillsenergy.com", "blackhillsenergy.com", 2),      # www is stripped
    ("myaccount.blackhillsenergy.com", "blackhillsenergy.com", 1),  # same domain, offered not silent
    ("blackhillsenergy.com.evil.net", "blackhillsenergy.com", 0),
    ("notpaypal.com", "paypal.com", 0),                            # the missing-dot lookalike
    ("login.hsbc.co.uk", "hsbc.co.uk", 1),
])
def test_ranking(host, stored, want, tmp_path):
    out = _run("""
        String host = VaultMatch.normHost("%s");
        System.out.println(VaultMatch.rank(L("%s"), L("%s"), host));
    """ % (host, stored, stored), tmp_path)
    assert int(out) == want, "%s vs stored %s" % (host, stored)


@pytest.mark.parametrize("raw,want", [
    ("https://www.example.com/login?x=1", "example.com"),
    ("example.com:8443", "example.com"),
    ("  WWW.Example.COM  ", "example.com"),
    ("example.com", "example.com"),
    (None, ""),
])
def test_norm_host(raw, want, tmp_path):
    arg = "null" if raw is None else '"%s"' % raw
    out = _run('System.out.println("[" + VaultMatch.normHost(%s) + "]");' % arg, tmp_path)
    assert out == "[%s]" % want


def test_the_service_asks_for_both_domains(tmp_path):
    """The walk must pass the enclosing domain DOWN, not latch the first one it sees globally."""
    svc = os.path.join(os.path.dirname(SRC), "PosterChanAutofillService.java")
    with open(svc, encoding="utf-8") as f:
        s = f.read()
    assert "VaultMatch.hostCandidates(parsed.fieldDomain, parsed.pageDomain)" in s
    assert "parse(node.getChildAt(i), out, inherited, depth + 1)" in s, \
        "the enclosing domain must be inherited by children"
    assert "out.webDomain" not in s, "the single latched domain is what caused the bug"


def test_an_androidapp_uri_names_a_package(tmp_path):
    out = _run("""
        for (String u : new String[]{"androidapp://com.chase.sig.android",
                                     "android://com.example", "ANDROIDAPP://Com.Example/x",
                                     "https://chase.com", "com.chase", ""})
          System.out.println("[" + VaultMatch.packageOfUri(u) + "]");
    """, tmp_path)
    # The scheme folds; the package does not — see the case-sensitivity test below.
    assert out.splitlines() == ["[com.chase.sig.android]", "[com.example]", "[Com.Example]",
                                "[]", "[]", "[]"]


def test_an_association_is_an_exact_match(tmp_path):
    """The user (or their Bitwarden import) said this entry belongs to this app."""
    out = _run("""
        List<String> uris = L("https://chase.com", "androidapp://com.chase.sig.android");
        System.out.println(VaultMatch.appMatches(uris, "com.chase.sig.android"));
        System.out.println(VaultMatch.appMatches(uris, "com.evil.chase"));
        System.out.println(VaultMatch.appMatches(L("https://chase.com"), "com.chase.sig.android"));
    """, tmp_path)
    assert out.splitlines() == ["true", "false", "false"]


@pytest.mark.parametrize("pkg,host,title,floor", [
    ("com.chase.sig.android", "chase.com", "Chase", 3),
    ("com.wellsfargo.mobile", "wellsfargo.com", "Wells Fargo", 3),
    ("com.paypal.android.p2pmobile", "paypal.com", "PayPal", 3),
    ("com.amazon.mShop.android.shopping", "amazon.com", "Amazon", 3),
    ("com.chase.sig.android", "example.com", "Chase Bank", 1),   # title only
    ("com.chase.sig.android", "example.com", "Netflix", 0),      # nothing at all
])
def test_suggestions_are_ranked_by_the_package_name(pkg, host, title, floor, tmp_path):
    """ORDERING ONLY. Nothing fills on this — the user reads the entry name and picks it."""
    out = _run('System.out.println(VaultMatch.appRank("%s", L("%s"), L("%s"), "%s"));'
               % (pkg, host, host, title), tmp_path)
    assert int(out) >= floor
    if floor == 0:
        assert int(out) == 0


def test_noise_segments_do_not_score(tmp_path):
    """`com`/`android`/`app` are in every package and identify nobody."""
    out = _run('System.out.println(VaultMatch.appRank("com.android.app", L("com.com"), L("com.com"), "x"));',
               tmp_path)
    assert int(out) == 0


def test_the_service_offers_the_vault_to_a_native_app(tmp_path):
    svc = os.path.join(os.path.dirname(SRC), "PosterChanAutofillService.java")
    with open(svc, encoding="utf-8") as f:
        s = f.read()
    assert "matchApp(snapshot, pkg, claimedWeb)" in s, \
        "a native app must reach the package path, not silence"
    assert "VaultStore.noteApp(this, pkg)" in s, "the asking app must be recorded for association"
    assert '"suggested  ·  " + label' in s, \
        ("the marker must LEAD: the dataset row is singleLine+ellipsize, so a trailing one is the "
         "first thing truncated — on exactly the rows whose safety is that they look different")
    assert "if (!BROWSERS.contains(pkg)) VaultStore.noteApp(this, pkg);" in s, \
        "associating a browser makes one entry an unlabelled match on every unreadable page"
    assert "VaultMatch.appMatches(uris, pkg)" in s
    # A non-browser's claimed webDomain is DISCARDED, not merely distrusted — but the request then
    # continues down the package path, which never reads that domain. Refusing outright meant an app
    # rendering its own login in its own WebView (Sam's Club) got silence on its own login screen.
    head = s[s.index("String pkg = packageOf(request);"):s.index("List<String> candidates")]
    assert 'parsed.fieldDomain = "";' in head and 'parsed.pageDomain = "";' in head
    assert "callback.onSuccess(null);" not in head, \
        "an app's own login screen must not be answered with silence"
    assert "BROWSERS.contains(pkg)" in head, "the claim itself must still be refused"


def test_an_app_webview_falls_through_to_the_package_path(tmp_path):
    """Sam's Club renders its login in its own WebView, so it reports a web domain as a matter of
    course — and got nothing at all, on a screen where it is plainly the app asking for its own
    password. The claim is discarded; the app is then treated as any other app."""
    svc = os.path.join(os.path.dirname(SRC), "PosterChanAutofillService.java")
    with open(svc, encoding="utf-8") as f:
        s = f.read()
    assert "boolean fromPackage = candidates.isEmpty();" in s, \
        "clearing the claimed domains is what routes it to the package path"
    assert "if (!BROWSERS.contains(pkg)) VaultStore.noteApp(this, pkg);" in s


def test_package_names_are_compared_case_sensitively(tmp_path):
    """Android package names are case-sensitive and uppercase is legal.

    Folding lets a sideloaded `Com.Chase` inherit the association its owner made for `com.chase` —
    and inherit it at association grade, which fills with no "suggested" marker at all.
    """
    out = _run("""
        System.out.println(VaultMatch.appMatches(L("androidapp://com.chase"), "Com.Chase"));
        System.out.println(VaultMatch.appMatches(L("androidapp://com.chase"), "com.chase"));
        System.out.println("[" + VaultMatch.packageOfUri("ANDROIDAPP://Com.Chase") + "]");
    """, tmp_path)
    assert out.splitlines() == ["false", "true", "[Com.Chase]"]


def test_a_noise_host_label_does_not_score(tmp_path):
    """`com` as a host label substring-matches every package on earth.

    It arrives whenever an `androidapp://com.…` URI leaks into the host list — which is fixed at the
    source in _syncAndroid, but the ranker must not be one leak away from putting a Gmail entry
    above the real Comcast one.
    """
    out = _run("""
        System.out.println(VaultMatch.appRank("com.comcast.xfinity",
            L("mail.google.com", "com.google.android.gm"), L("google.com", "android.gm"), "Gmail"));
    """, tmp_path)
    assert int(out) == 0


def test_app_associations_never_become_web_host_keys(tmp_path):
    """`androidapp://com.google.android.gm` has a registrable domain: `android.gm`, and .gm is real.

    Register it, serve a login page at com.google.android.gm, and the Gmail credential is an EXACT
    unlabelled match in the browser. The association belongs in `uris` and nowhere else.
    """
    vault = os.path.join(ROOT, "static", "js", "client", "vault.js")
    with open(vault, encoding="utf-8") as f:
        s = f.read()
    assert "const web = rules.filter(r => !/^androidapps?:|^android:/i.test(r.uri || ''));" in s
    assert "const hosts = web.map(r => V().hostOf(r.uri)).filter(Boolean);" in s
    assert "const wide = web.filter(" in s


def test_an_association_survives_editing_the_entry(tmp_path):
    """The Websites box rebuilds `it.uris` wholesale and never showed the app associations.

    Add an app, type one more character, and the debounced save wrote the list back without it —
    chip still on screen, association gone, snapshot re-pushed without it.
    """
    vault = os.path.join(ROOT, "static", "js", "client", "vault.js")
    with open(vault, encoding="utf-8") as f:
        s = f.read()
    assert "const apps = V().itemUriRules(it).filter(r => /^androidapps?:|^android:/i.test(r.uri || ''));" in s
    assert ".concat(apps);" in s


# --------------------------------------------------------------- which field is which

def _fields(*specs):
    """specs: (hints, realPassword, visiblePassword, text) tuples → Java list-building source."""
    out = ["List<VaultMatch.FieldInfo> f = new ArrayList<>();"]
    for h, real, vis, text in specs:
        out.append('f.add(new VaultMatch.FieldInfo("%s", %s, %s, "%s"));'
                   % (h, str(real).lower(), str(vis).lower(), text))
    return "\n".join(out)


def _pick(specs, tmp_path):
    body = _fields(*specs) + """
        int[] p = VaultMatch.pickFields(f);
        System.out.println(p[0] + " " + p[1]);
    """
    return tuple(int(x) for x in _run(body, tmp_path).split())


def test_wells_fargo_visible_password_on_the_username_field(tmp_path):
    """THE BUG: the password was typed into the username box, on screen, in a banking app.

    `textVisiblePassword` does not mean "this is a password" — it means "no suggestions, no
    autocorrect, do not learn what is typed here", which is exactly what a bank wants on a customer
    ID. Counting it as a password picked the username box as the password slot; the real password
    box was then skipped because something had already been chosen.
    """
    # field 0: username box with textVisiblePassword and a neutral id. field 1: the real password.
    assert _pick(((" ", False, True, "userid"), (" ", True, False, "password")), tmp_path) == (0, 1)


def test_visible_password_alone_is_a_password_only_when_the_label_says_so(tmp_path):
    """A one-time-code box: `textVisiblePassword` plus a label that positively says "code". That IS
    a secret, and scoring it below every real signal must not mean ignoring it."""
    assert _pick(((" ", False, True, "enter code"),), tmp_path) == (-1, 0)


def test_the_lone_unlabelled_visible_password_box_is_the_username(tmp_path):
    """THE WELLS FARGO BUG, second round — and why scoring it low never fixed it.

    Wells Fargo's real password box does not reach the service at all: it is not in the structure
    the platform hands over. So the customer-ID box (textVisiblePassword, neutral id) is the ONLY
    field on the screen, and "scored below every real signal" is meaningless when there is no other
    signal — score 1 is simultaneously the lowest and the highest, so it won the password slot
    unopposed. The password was typed into the box the user can read, and the field they could not
    see got nothing. That is the exact symptom, reported twice after two fixes that both only
    RE-RANKED this evidence instead of rejecting it.

    So the lone neutral box is the username box, which is what it actually is — the bank pattern is
    "no keyboard suggestions on the customer ID". The screen that used to be filled catastrophically
    wrong now fills correctly.
    """
    assert _pick(((" ", False, True, "userid"),), tmp_path) == (0, -1)
    assert _pick(((" ", False, True, ""),), tmp_path) == (0, -1)


def test_a_declared_hint_beats_every_inference(tmp_path):
    """An autofillHint is the app telling us; everything else here is us guessing."""
    assert _pick(((("username"), False, True, "password"),
                  ("password", False, False, "user")), tmp_path) == (0, 1)


def test_a_real_password_field_wins_over_a_visible_one(tmp_path):
    assert _pick(((" ", False, True, "aaa"), (" ", True, False, "bbb")), tmp_path) == (0, 1)


def test_one_field_is_never_both(tmp_path):
    """A single box labelled 'user password' must not be handed both values."""
    u, p = _pick(((" ", True, False, "user password"),), tmp_path)
    assert not (u == p and u >= 0)


def test_the_box_above_the_password_is_the_username(tmp_path):
    """Two unlabelled boxes: the one before a REAL password field is where a username goes."""
    assert _pick(((" ", False, False, ""), (" ", True, False, "")), tmp_path) == (0, 1)


def test_an_unlabelled_visible_password_box_is_never_handed_the_password(tmp_path):
    """Two unlabelled boxes, the second `textVisiblePassword`. This used to fill the PASSWORD into
    box 1 on that evidence alone.

    It no longer fills anything, and that asymmetry is the point. Typing a secret into a readable
    box the app then submits as a username is unrecoverable — it is in their logs before the user
    can react — while filling nothing costs one typed password. `textVisiblePassword` means "no
    suggestions, no autocorrect, do not learn what is typed here", which a bank wants on an account
    number every bit as much as on a password; it is not evidence of a secret and is no longer
    treated as any. Nor is a username inferred beside it: that would be two guesses stacked.

    A genuinely unlabelled two-box login still fills — via the REAL password inputType and the
    positional rule above, which is the case that has actual evidence behind it.
    """
    assert _pick(((" ", False, False, ""), (" ", False, True, "")), tmp_path) == (-1, -1)


def test_a_labelled_username_is_found_anywhere_on_the_screen(tmp_path):
    assert _pick(((" ", True, False, "password"), (" ", False, False, "email address")),
                 tmp_path) == (1, 0)


def test_the_service_decides_over_the_whole_screen(tmp_path):
    svc = os.path.join(os.path.dirname(SRC), "PosterChanAutofillService.java")
    with open(svc, encoding="utf-8") as f:
        s = f.read()
    assert "VaultMatch.pickFields(out.fields)" in s
    assert "isVisiblePassword" in s and "isRealPassword" in s, \
        "the two input types must be distinguishable, not merged"
    assert "looksLikeUsername" not in s, "first-wins over the tree is what poisoned the other slot"
    body = s[s.index("private static boolean isRealPassword"):s.index("private static boolean isVisiblePassword")]
    assert "VISIBLE_PASSWORD" not in body, "a visible-password field is not a real password field"


def test_a_phone_field_is_not_a_username(tmp_path):
    """A checkout or shipping-address screen declares phone hints and has no login on it.

    Scoring it made the vault drop down over a phone-number box, fill an email into it, and arm the
    "save this login?" prompt on an address form.
    """
    assert _pick((("phone", False, False, "phone number"),), tmp_path) == (-1, -1)


def test_the_positional_fallback_skips_a_search_box(tmp_path):
    """"The field before the password" is a guess about LAYOUT, and every WebView app has a toolbar
    search input ahead of the login card in tree order."""
    assert _pick(((" ", False, False, "search"), (" ", True, False, "")), tmp_path) == (-1, 1)
    assert _pick(((" ", False, False, "amount"), (" ", True, False, "")), tmp_path) == (-1, 1)
    # ...but a genuinely unlabelled box before the password still counts.
    assert _pick(((" ", False, False, ""), (" ", True, False, "")), tmp_path) == (0, 1)


def test_webview_html_inputs_are_read(tmp_path):
    """A page inside an app's own WebView carries no autofillHints, no idEntry and no Android input
    type — only HTML. Without reading it, such a login screen looks like a screen with no fields on
    it, which is 'autofill does nothing at all in this app'."""
    svc = os.path.join(os.path.dirname(SRC), "PosterChanAutofillService.java")
    with open(svc, encoding="utf-8") as f:
        s = f.read()
    assert "node.getHtmlInfo()" in s
    assert 'if (itype.equals("password")) htmlPassword = true;' in s
    assert 'k.equals("autocomplete")' in s, "autocomplete=current-password is the standard signal"
    assert "skip = true;" in s and "if (!skip) {" in s, \
        "a non-login input must be skipped, not returned from — that abandons the subtree"
    assert "node.getVisibility() == View.VISIBLE" in s, \
        "a hidden honeypot input must not claim the username slot"


def test_an_untrusted_web_surface_is_not_offered_the_whole_vault(tmp_path):
    """An in-app browser can be pointed at a phishing page; 'here is your entire vault' one tap from
    a convincing fake is a worse offer than nothing. An app showing its OWN login ranks against its
    own package and never needs that fallback."""
    svc = os.path.join(os.path.dirname(SRC), "PosterChanAutofillService.java")
    with open(svc, encoding="utf-8") as f:
        s = f.read()
    assert "if (out.isEmpty() && !claimedWeb) out.addAll(ranked.get(0));" in s
    assert "matchApp(snapshot, pkg, claimedWeb)" in s


# --------------------------------------------------------------- the fill diagnostic

def test_every_exit_from_a_fill_request_is_recorded():
    """The service has no UI and runs in its own process, so a wrong pick left nothing to look at
    short of `adb logcat` — a computer, a cable and developer mode, for a bug that only happens on a
    phone in someone's hand. "The password went into the username box" was fixed twice from a
    description and came back both times, because a description cannot say which fields the screen
    offered. Every way out of onFillRequest must say what it saw, INCLUDING the ones that fill
    nothing: "no field found" and "nothing matched" are exactly the outcomes that look identical to
    autofill not being installed at all.
    """
    svc = os.path.join(os.path.dirname(SRC), "PosterChanAutofillService.java")
    with open(svc, encoding="utf-8") as f:
        s = f.read()
    body = s[s.index("public void onFillRequest"):s.index("private Dataset dataset(")]
    # Each early return pairs an onSuccess(null) with a note(); the success path notes before it.
    assert body.count("callback.onSuccess(null)") == body.count("note(parsed,"), \
        "an exit that records nothing is the exit you cannot diagnose"
    assert 'note(parsed, pkg, claimedWeb, candidates, "offered "' in s, \
        "the path that DOES fill has to be reportable too"
    assert "out.pickUser = pick[0];" in s and "out.pickPass = pick[1];" in s, \
        "the report is useless without which field it chose"


def test_the_diagnostic_carries_nothing_from_the_vault():
    """It exists to be read and pasted into a bug report, so what it may hold is the whole design:
    the shape of the screen, and nothing else. Not the entry, not the username, not the password,
    not what was typed, and not how many entries matched."""
    svc = os.path.join(os.path.dirname(SRC), "PosterChanAutofillService.java")
    with open(svc, encoding="utf-8") as f:
        s = f.read()
    note = s[s.index("private void note(Parsed p"):s.index("private static String clip(")]
    for banned in ("optString(\"username\"", "optString(\"password\"", "optString(\"title\"",
                   "matches", "snapshot", "getText("):
        assert banned not in note, f"the fill diagnostic must not carry {banned}"
    # Only these keys, so adding one is a deliberate act with this test in the way.
    keys = set(re.findall(r'\.put\("([a-zA-Z]+)"', note))
    assert keys == {"at", "pkg", "outcome", "claimedWeb", "hosts", "pickUser", "pickPass",
                    "fields", "fieldCount", "hints", "realPw", "visPw", "text"}, keys
    store = os.path.join(os.path.dirname(SRC), "VaultStore.java")
    with open(store, encoding="utf-8") as f:
        st = f.read()
    assert "remove(KEY_LASTFILL)" in st, "signing out must not leave the last screen behind"
