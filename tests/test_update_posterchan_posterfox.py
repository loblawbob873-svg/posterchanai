"""update-posterchan keeps PosterFox current, and GIVES it to a machine installed before it existed.

"make sure all the apps get updated", then "i don't see posterfox in gentoo yet" (2026-10-29): gentoo.sh writes the
keyword, the signed [posterchan] binrepo and its key on a FRESH install only, and the updater only ever upgraded a
PosterFox that was already there -- so every earlier machine kept firefox-bin for ever, while the session's mimeapps
already sent every link to posterfox.desktop. Never by compiling Firefox (hours, on the user's machine), and a missing
binary never stops the PosterChan update. The updater's own functions, RUN under bash against a temp /etc/portage with
`qlist`, `emerge`, `gpg` and `getuto` stubbed.
"""
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPD = ROOT / "os/bin/update-posterchan"
GENTOO = ROOT / "os/gentoo.sh"


def _functions():
    s = UPD.read_text()
    a = s.index('POSTERCHAN_BINPKG_FPR=')
    return s[a:s.index("\n}\n", s.index("_pc_posterfox() {")) + 3]


BINARY_PLAN = ("[ebuild  N    ~] dev-libs/nss-3.132 \n[ebuild  N     ] x11-misc/xdg-utils-1.2.1-r11 \n"
               "[binary  N g  ~] www-client/posterfox-159.0-r9-1 \n")   # measured on a desktop/systemd stage3


def _run(tmp, installed, emerge_rc=0, keyring=True, key_known=False, plan=BINARY_PLAN, after_sync=None):
    bindir, etc = tmp / "bin", tmp / "etc"
    bindir.mkdir()
    (etc / "package.accept_keywords").mkdir(parents=True)
    (etc / "package.accept_keywords" / "00-local").write_text("dev-util/other ~amd64\n")
    if keyring:
        (etc / "gnupg").mkdir()
    (bindir / "qlist").write_text("#!/bin/sh\n" + ("echo www-client/posterfox\n" if installed else "") + "exit 0\n")
    (tmp / "plan").write_text(plan)
    (tmp / "plan-synced").write_text(plan if after_sync is None else after_sync)
    # The plan answers from the tree: before `emaint sync -r gentoo` one plan, after it the other.
    (bindir / "emerge").write_text(f'#!/bin/sh\ncase " $* " in *" -p "*) echo "$@" >> {tmp}/plans.log;'
                                   f' if [ -e {tmp}/synced ]; then cat {tmp}/plan-synced; else cat {tmp}/plan; fi; exit 0;; esac\n'
                                   f'echo "$@" >> {tmp}/emerge.log\nexit {emerge_rc}\n')
    (bindir / "emaint").write_text(f'#!/bin/sh\necho "$@" >> {tmp}/emaint.log\ntouch {tmp}/synced\n')
    (bindir / "gpg").write_text(f'#!/bin/sh\necho "$@" >> {tmp}/gpg.log\n'
                                f'case "$*" in *--list-keys*) {"echo pub:f:255:22; exit 0" if key_known else "exit 2"};; esac\nexit 0\n')
    (bindir / "getuto").write_text(f"#!/bin/sh\nexit 1\n")
    for f in bindir.iterdir():
        f.chmod(0o755)
    script = "CYA=; OFF=; YEL=\n" + _functions() + "_pc_posterfox\necho AFTER\n"
    out = subprocess.run([shutil.which("bash"), "-c", script], capture_output=True, text=True, timeout=20,
                         env={"PATH": f"{bindir}:/usr/bin:/bin", "PC_PORTAGE_ETC": str(etc)})
    read = lambda n: (tmp / n).read_text() if (tmp / n).exists() else ""
    return out, read("emerge.log"), read("gpg.log"), etc


UPGRADE_PLAN = "[binary     U  ~] www-client/posterfox-160.0-r1-1 [159.0-r9]\n"


def test_an_installed_posterfox_is_updated_from_its_binary_only(tmp_path):
    out, log, _, _ = _run(tmp_path, installed=True, plan=UPGRADE_PLAN)
    args = log.split()
    assert "www-client/posterfox" in args and "-u" in args and "--usepkg" in args, log
    assert "AFTER" in out.stdout


def test_a_current_posterfox_is_left_alone(tmp_path):
    out, log, _, _ = _run(tmp_path, installed=True, plan="Total: 0 packages, Size of downloads: 0 KiB\n")
    assert log == "" and "keeping" not in out.stdout, (log, out.stdout)


def test_a_newer_posterfox_with_no_binary_yet_is_not_compiled(tmp_path):
    out, log, _, _ = _run(tmp_path, installed=True, plan="[ebuild     U  ~] www-client/posterfox-160.0 [159.0-r9]\n")
    assert log == "", ("an upgrade would have compiled Firefox", log)
    assert "keeping the browser it has" in out.stdout and "AFTER" in out.stdout


STALE_TREE = ('emerge: there are no ebuilds to satisfy ">=dev-libs/nss-3.132".\n'
              '(dependency required by "www-client/posterfox-159.0-r9::posterchan" [binary])\n')   # the laptop, 10-29


def test_a_stale_gentoo_tree_is_synced_once_and_the_install_goes_ahead(tmp_path):
    out, log, _, _ = _run(tmp_path, installed=False, plan=STALE_TREE,
                          after_sync="[ebuild     U ~] dev-libs/nss-3.132 [3.131]\n[binary  N g  ~] www-client/posterfox-159.0-r9-1\n")
    assert (tmp_path / "emaint.log").read_text().split() == ["sync", "-r", "gentoo"]
    assert "www-client/posterfox" in log.split(), ("PosterFox was not installed after the tree caught up", out.stdout)


def test_a_tree_that_stays_stale_keeps_the_browser_and_says_why(tmp_path):
    out, log, _, _ = _run(tmp_path, installed=False, plan=STALE_TREE)
    assert log == "" and "no usable package" in out.stdout and "nss-3.132" in out.stdout, out.stdout
    assert "AFTER" in out.stdout


def test_no_tree_sync_when_the_plan_is_fine(tmp_path):
    _run(tmp_path, installed=False)
    assert not (tmp_path / "emaint.log").exists()


def test_a_machine_from_before_posterfox_is_given_it_from_the_signed_binhost(tmp_path):
    out, log, gpg, etc = _run(tmp_path, installed=False)
    args = log.split()
    assert "www-client/posterfox" in args and "--usepkg" in args and "--getbinpkg" in args, ("PosterFox was never installed", log)
    kw = "".join(p.read_text() for p in (etc / "package.accept_keywords").iterdir())
    assert "www-client/posterfox ~amd64" in kw and "dev-libs/nss ~amd64" in kw and "dev-util/other ~amd64" in kw, kw
    repo = (etc / "binrepos.conf" / "posterchan.conf").read_text()
    assert "https://gentoo.poster.place/posterchan-binpkgs" in repo and "verify-signature = true" in repo, repo
    assert "--import" in gpg and "--quick-lsign-key 7B690AA70C09AD1AD7748CF5B6F8DA9DDF2EF936" in gpg, gpg
    # Portage verifies as a user that cannot rewrite the trustdb: refreshed here, or the key reads TRUST_UNDEFINED.
    assert gpg.index("--quick-lsign-key") < gpg.index("--check-trustdb"), gpg
    assert "AFTER" in out.stdout


def test_adoption_is_idempotent_and_keeps_what_is_there(tmp_path):
    etc = tmp_path / "etc"
    (etc / "binrepos.conf").mkdir(parents=True)
    (etc / "binrepos.conf" / "mine.conf").write_text("[mine]\nsync-uri = https://gentoo.poster.place/posterchan-binpkgs\n")
    out, log, gpg, etc = _run(tmp_path, installed=False, key_known=True)
    assert not (etc / "binrepos.conf" / "posterchan.conf").exists(), "a second binrepo entry for the same binhost"
    assert "--import" not in gpg, "re-imported a key the keyring already trusts"
    assert "www-client/posterfox" in log


def test_no_keyring_keeps_firefox_and_never_compiles(tmp_path):
    out, log, _, _ = _run(tmp_path, installed=False, keyring=False)
    assert log == "", ("emerged PosterFox it could not verify", log)
    assert "keeping the browser it has" in out.stdout and "AFTER" in out.stdout


def test_a_failed_install_is_not_fatal(tmp_path):
    out, _, _, _ = _run(tmp_path, installed=False, emerge_rc=1)
    assert "keeping the browser it has" in out.stdout and "AFTER" in out.stdout


def test_posterfox_itself_is_never_compiled(tmp_path):
    out, log, _, _ = _run(tmp_path, installed=False, plan="[ebuild  N    ~] www-client/posterfox-159.0-r9 \n")
    assert log == "", ("PosterFox would have been built from source", log)
    assert "no binary package" in out.stdout and "AFTER" in out.stdout


def test_a_toolchain_is_never_compiled_to_get_it(tmp_path):
    plan = "[ebuild  N     ] dev-lang/rust-1.91.0 \n" + BINARY_PLAN
    out, log, _, _ = _run(tmp_path, installed=False, plan=plan)
    assert log == "", ("a Rust toolchain would have been built for the browser", log)
    assert "toolchain" in out.stdout and "AFTER" in out.stdout


def test_gentoo_sh_refreshes_the_trustdb_too():
    t = GENTOO.read_text()
    body = t[t.index("trustPosterchanBinhost() {"):t.index("\n}\n", t.index("trustPosterchanBinhost() {"))]
    assert body.index("--quick-lsign-key") < body.index("--check-trustdb"), "a fresh install's key can read as untrusted"


def test_the_key_is_the_one_gentoo_sh_pins():
    def key(text):
        a = text.index("-----BEGIN PGP PUBLIC KEY BLOCK-----")
        return text[a:text.index("-----END PGP PUBLIC KEY BLOCK-----", a)]
    assert key(UPD.read_text()) == key(GENTOO.read_text())
    assert 'POSTERCHAN_BINPKG_FPR="7B690AA70C09AD1AD7748CF5B6F8DA9DDF2EF936"' in GENTOO.read_text()


def test_it_runs_after_the_overlay_sync_and_before_any_already_up_to_date_exit():
    s = UPD.read_text()
    call = s.index("\t_pc_posterfox\n")
    assert s.index("emaint sync -r posterchan") < call < s.index("Already up to date.${OFF}"), \
        "the ebuild is not visible yet, or an update that found nothing new would never check the browser"


def test_links_fall_back_to_firefox_until_posterfox_lands():
    mime = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/posterchanos-mimeapps.list").read_text()
    for t in ("x-scheme-handler/http", "x-scheme-handler/https", "text/html"):
        assert f"\n{t}=posterfox.desktop;firefox-bin.desktop;\n" in mime, t
