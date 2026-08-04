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
import shutil
import subprocess
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java",
                   "place", "poster", "app", "vault", "VaultMatch.java")

pytestmark = pytest.mark.skipif(shutil.which("javac") is None, reason="no JDK")


def _run(body, tmp_path):
    """Compile the real VaultMatch alongside a harness, run it, return stdout."""
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
    assert "matchApp(snapshot, pkg)" in s, "a native app must reach the package path, not silence"
    assert "VaultStore.noteApp(this, pkg)" in s, "the asking app must be recorded for association"
    assert '"suggested  ·  " + label' in s, \
        ("the marker must LEAD: the dataset row is singleLine+ellipsize, so a trailing one is the "
         "first thing truncated — on exactly the rows whose safety is that they look different")
    assert "if (!BROWSERS.contains(pkg)) VaultStore.noteApp(this, pkg);" in s, \
        "associating a browser makes one entry an unlabelled match on every unreadable page"
    assert "VaultMatch.appMatches(uris, pkg)" in s
    # The browser-trust refusal must NOT fall through to the package shortlist: an app that lies
    # about a web domain would be rewarded with one.
    head = s[s.index("String pkg = packageOf(request);"):s.index("List<String> candidates")]
    assert "callback.onSuccess(null);" in head and "return;" in head


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
