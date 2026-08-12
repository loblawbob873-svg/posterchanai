"""Deploying the webxdc sandbox ORIGIN — the installer's refusals and its rendering, actually run.

    venv-unified/bin/python -m unittest tests.test_webxdc_deploy

WHAT THIS EXISTS FOR. Everything about mini apps ships in the code except the second hostname,
`xdc.<instance>`, and missing it is the project's favourite failure mode: the composer offers
"🎮 Mini app", the post publishes, the cartridge renders, Play opens a window that stays blank
forever, and NOTHING is requested from the server, so there is nothing in any log. `./install.sh
--webxdc` is the fix, which makes the installer itself the thing that must not be subtly wrong.

It is EXECUTED under bash rather than read, with `nginx` stubbed and `WEBXDC_DRY_RUN=1`, because the
two properties that matter are shell behaviour, not text:

  * it REFUSES, having written nothing, when the DNS record does not exist yet — a vhost for a name
    that does not resolve is exactly as invisible as no vhost at all, and an operator staring at a
    blank game window cannot tell those two apart;
  * what it renders is the reviewed `nginx/webxdc-sandbox.conf.example` with the hostname, upstream
    and certificate path substituted — not a second copy of that config living in a shell script,
    which is how the comments recording two measured dead ends (a wildcard, a port) would rot.

The label check at the bottom is the other silent one: the client hardcodes `xdc.`, the app gates
`/sw.js` on `xdc.`, and the installer provisions `xdc.`. Three files, no setting between them (which
is the design — nothing to configure is nothing to get out of step), so a rename in one of them is
a feature that stops working with no error anywhere.
"""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "install" / "webxdc.sh"
EXAMPLE = ROOT / "nginx" / "webxdc-sandbox.conf.example"
PROXY_CONF = ROOT / "docker" / "proxy" / "posterchanai.conf"
PROXY_ENTRY = ROOT / "docker" / "proxy" / "entrypoint.sh"


def run_installer(env=None, domain="example.org", nginx_dir=None, timeout=60):
    """Run `setup_webxdc_sandbox` for real, in a dry run, with a stub nginx on PATH."""
    with tempfile.TemporaryDirectory() as td:
        bindir = Path(td) / "bin"
        bindir.mkdir()
        # A stub nginx: the installer refuses outright on a host without one, and `nginx -t` is
        # skipped by the dry run — so this only has to exist and succeed.
        (bindir / "nginx").write_text("#!/usr/bin/env bash\nexit 0\n")
        (bindir / "nginx").chmod(0o755)
        conf_dir = Path(nginx_dir) if nginx_dir else Path(td) / "nginx"
        conf_dir.mkdir(parents=True, exist_ok=True)
        e = {
            "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
            "HOME": td,
            "WEBXDC_DRY_RUN": "1",
            "WEBXDC_DOMAIN": domain,
            "WEBXDC_NGINX_DIR": str(conf_dir),
            "WEBXDC_SKIP_CERTBOT": "1",
        }
        e.update(env or {})
        p = subprocess.run(
            ["bash", "-c", f'source "{MODULE}"; setup_webxdc_sandbox'],
            capture_output=True, text=True, env=e, timeout=timeout, cwd=str(ROOT))
        wrote = sorted(x.name for x in conf_dir.iterdir())
        return p, wrote


class TheInstallerRefusesRatherThanHalfDoingIt(unittest.TestCase):
    def test_it_refuses_and_writes_nothing_when_the_dns_record_is_missing(self):
        # RFC 2606 reserves .invalid precisely so this can never accidentally resolve.
        p, wrote = run_installer(domain="posterchanai-webxdc-test.invalid")
        self.assertNotEqual(p.returncode, 0, "a missing DNS record must be a refusal")
        self.assertEqual(wrote, [], "nothing may be written before the hostname exists")
        out = p.stdout + p.stderr
        self.assertIn("does not resolve", out)
        self.assertIn("Nothing has been changed", out)

    def test_the_refusal_prints_the_exact_dns_record_to_add(self):
        p, _ = run_installer(domain="posterchanai-webxdc-test.invalid")
        out = p.stdout + p.stderr
        self.assertIn("CNAME", out)
        self.assertIn("xdc.posterchanai-webxdc-test.invalid", out)
        # The operator has to know it can sit behind the CDN; the alternative (a port) was measured
        # NOT to survive Cloudflare, so this is the sentence that stops them re-trying it.
        self.assertIn("Cloudflare", out)

    def test_it_refuses_the_sandbox_hostname_as_the_instance_hostname(self):
        p, wrote = run_installer(domain="xdc.example.org", env={"WEBXDC_SKIP_DNS": "1"})
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(wrote, [])

    def test_it_refuses_to_guess_the_hostname_with_nobody_there_to_correct_it(self):
        # A node commonly answers on several names that all proxy to :3051 (ai., news., relay.) and
        # the client is served from exactly one of them. Guessed wrong and unattended, the result is
        # a vhost for a hostname nobody visits — which looks exactly like not installing one.
        p, wrote = run_installer(domain="", env={"WEBXDC_SKIP_DNS": "1"})
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(wrote, [])
        self.assertIn("WEBXDC_DOMAIN=", p.stdout + p.stderr)

    def test_it_does_not_run_certbot_unattended(self):
        # certbot with no tty needs --agree-tos and an email it cannot obtain, so running it blind
        # fails in a way that reads as a bug in this installer.
        p, _ = run_installer(domain="example.org",
                             env={"WEBXDC_SKIP_DNS": "1", "WEBXDC_SKIP_CERTBOT": ""})
        out = p.stdout + p.stderr
        self.assertIn("sudo certbot --nginx -d xdc.example.org", out)
        self.assertNotIn("[dry-run] certbot", out)

    def test_it_refuses_a_hostname_that_is_not_one(self):
        p, wrote = run_installer(domain="localhost", env={"WEBXDC_SKIP_DNS": "1"})
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(wrote, [])


class WhatItWouldWrite(unittest.TestCase):
    """The rendered vhost, from a run that is allowed to get that far."""

    @classmethod
    def setUpClass(cls):
        cls.proc, _ = run_installer(
            domain="example.org",
            env={"WEBXDC_SKIP_DNS": "1", "WEBXDC_UPSTREAM": "10.9.8.7:3051"})
        cls.out = cls.proc.stdout + cls.proc.stderr
        # The dry run echoes the file it would write, one `    | ` per line.
        cls.conf = "\n".join(
            l[6:] for l in cls.out.splitlines() if l.startswith("    | "))

    def test_the_run_gets_as_far_as_rendering_the_vhost(self):
        self.assertTrue(self.conf.strip(), f"no config rendered:\n{self.out[-2000:]}")

    def test_it_serves_the_sandbox_hostname(self):
        self.assertIn("server_name xdc.example.org;", self.conf)
        self.assertNotIn("poster.place", self.conf, "the template's own domain leaked through")

    def test_it_proxies_the_two_paths_the_app_answers_and_nothing_else(self):
        self.assertIn("location = /sw.js", self.conf)
        self.assertIn("location /__sandbox__", self.conf)
        self.assertIn("http://10.9.8.7:3051", self.conf)
        # Every proxy_pass in the file must be the app; a second front door to the instance from an
        # origin untrusted code runs on is the one thing this vhost exists to prevent.
        for target in re.findall(r"proxy_pass\s+(\S+);", self.conf):
            self.assertEqual(target, "http://10.9.8.7:3051", f"unexpected upstream {target}")

    def test_the_certificate_is_the_sandbox_hostname_s_own(self):
        self.assertIn("/etc/letsencrypt/live/xdc.example.org/fullchain.pem", self.conf)

    def test_it_never_touches_the_instance_certificate(self):
        # `--expand -d <instance> -d xdc.<instance>` rewrites the PRODUCTION certificate to add a
        # game feature: a failed issuance takes the whole instance off TLS. So the sandbox host gets
        # a lineage of its own, and --expand appears nowhere except in the sentence saying not to.
        self.assertIn("sudo certbot --nginx -d xdc.example.org", self.conf)
        for line in (self.conf + "\n" + MODULE.read_text()).splitlines():
            if "--expand" in line:
                self.assertIn("NOT", line, f"--expand offered as an option: {line.strip()}")
        self.assertFalse(
            [l for l in MODULE.read_text().splitlines()
             if re.search(r"^\s*_wx_sudo certbot\b", l) and "--expand" in l],
            "the installer must never run certbot --expand")

    def test_it_does_not_set_service_worker_allowed(self):
        # The APP sets it. Set in both places, fetch combines the duplicates into "/, /" — not a
        # valid scope prefix — and Firefox refuses the registration with `SecurityError: The
        # operation is insecure`, which reads as a platform limit and is not one.
        self.assertNotIn("add_header Service-Worker-Allowed", self.conf)

    def test_it_is_the_reviewed_template_and_not_a_second_copy(self):
        """Rendering must be a substitution of the shipped example, comments and all."""
        marker = "MEASURED, it does not survive Cloudflare"
        self.assertIn(marker, EXAMPLE.read_text())
        self.assertIn(marker, self.conf)


class ReRunningIsSafe(unittest.TestCase):
    """Not a dry run: the file is really written, twice, with the system tools stubbed.

    Every step here either edits /etc or reloads a service, so the dry run above is the only way
    most of it can be reviewed — and a dry run writes nothing, so it can say nothing about the
    SECOND run, which is the one an operator actually does (a re-run after adding the DNS record,
    after certbot, after an upgrade). This runs it for real under `bash -e` against a stubbed
    sudo/nginx/systemctl/certbot, the same shape as tests/test_logs_scheduler.py: the properties
    are shell behaviour — the file lands, the re-run recognises it, and the installer exits 0 both
    times rather than ending somewhere in the middle and reporting success by falling off the end.
    """

    def run_for_real(self, td, domain="example.org", with_cert=True):
        td = Path(td)
        bindir, conf_dir, le = td / "bin", td / "nginx", td / "letsencrypt"
        for d in (bindir, conf_dir, le / "live" / f"xdc.{domain}"):
            d.mkdir(parents=True, exist_ok=True)
        cert = le / "live" / f"xdc.{domain}" / "fullchain.pem"
        if with_cert:
            cert.write_text("x")
        elif cert.exists():
            cert.unlink()
        # `sudo` that just runs the command: the destination here is a temp directory, so the
        # installer's writes must actually land for the second run to have anything to compare.
        (bindir / "sudo").write_text('#!/usr/bin/env bash\nexec "$@"\n')
        for name in ("nginx", "systemctl", "curl", "certbot"):
            (bindir / name).write_text("#!/usr/bin/env bash\nexit 0\n")
        for f in bindir.iterdir():
            f.chmod(0o755)
        env = {
            "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
            "HOME": str(td),
            "WEBXDC_DOMAIN": domain,
            "WEBXDC_NGINX_DIR": str(conf_dir),
            "WEBXDC_LE_DIR": str(le),
            "WEBXDC_SKIP_DNS": "1",
        }
        p = subprocess.run(
            ["bash", "-e", "-c", f'source "{MODULE}"; setup_webxdc_sandbox'],
            capture_output=True, text=True, env=env, timeout=60, cwd=str(ROOT))
        return p, conf_dir / "webxdc-sandbox.conf"

    def test_it_writes_the_vhost_and_a_second_run_leaves_it_alone(self):
        with tempfile.TemporaryDirectory() as td:
            first, dest = self.run_for_real(td)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertTrue(dest.exists(), "no vhost written")
            body = dest.read_text()
            self.assertIn("server_name xdc.example.org;", body)

            second, _ = self.run_for_real(td)
            self.assertEqual(second.returncode, 0,
                             "a re-run must not fail\n" + second.stdout + second.stderr)
            self.assertIn("already current", second.stdout + second.stderr)
            self.assertEqual(body, dest.read_text(), "a re-run rewrote the config")

    def test_a_re_run_that_cannot_see_the_certificate_does_not_downgrade_the_vhost(self):
        """`/etc/letsencrypt/live` is 0700 root, and this tool is usually run as an ordinary user.

        Reading "I can't stat fullchain.pem" as "there is no certificate" sends a working node back
        to the HTTP-only stage — mini apps broken by re-running the tool that installs them, with a
        cheerful "http://… is served by this nginx" as the only sign.
        """
        with tempfile.TemporaryDirectory() as td:
            first, dest = self.run_for_real(td)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            tls = dest.read_text()
            self.assertIn("listen 443 ssl;", tls)

            # Now the certificate is invisible, exactly as a non-root run sees a 0700 live/.
            second, _ = self.run_for_real(td, with_cert=False)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn("listen 443 ssl;", dest.read_text(), "the vhost was downgraded to HTTP")
            self.assertEqual(tls, dest.read_text())

    def test_a_second_dry_run_renders_the_same_thing(self):
        with tempfile.TemporaryDirectory() as td:
            env = {"WEBXDC_SKIP_DNS": "1"}
            a, _ = run_installer(domain="example.org", env=env, nginx_dir=td)
            b, _ = run_installer(domain="example.org", env=env, nginx_dir=td)
            self.assertEqual(a.returncode, b.returncode)
            self.assertEqual(a.stdout, b.stdout)


class TheDockerProxyCarriesTheSameHostname(unittest.TestCase):
    """A compose deployment gets it from the seeded proxy config; nothing else would."""

    def test_the_seeded_config_has_the_sandbox_vhost(self):
        conf = PROXY_CONF.read_text()
        self.assertIn("server_name xdc.example.com;", conf)
        self.assertIn("location /__sandbox__", conf)
        self.assertIn("location = /sw.js", conf)
        # example.com is what the entrypoint sed-substitutes for POSTERCHANAI_DOMAIN on first boot,
        # so the sandbox name has to be spelled that way or it never becomes the operator's domain.
        self.assertIn("xdc.example.com", conf)

    def test_the_sandbox_block_does_not_set_service_worker_allowed(self):
        self.assertNotIn("Service-Worker-Allowed \"/\"", PROXY_CONF.read_text())

    def test_the_self_signed_certificate_covers_the_sandbox_name(self):
        # Without the SAN, mini apps are the ONE feature a self-signed deployment cannot click
        # through: the browser's warning is per-origin and the app frame cannot show one, so it
        # dies at service-worker registration with a blank window and nothing logged.
        self.assertIn("DNS:xdc.${DOMAIN}", PROXY_ENTRY.read_text())


class TheLabelIsTheSameInEveryHalf(unittest.TestCase):
    def test_client_app_and_installer_agree(self):
        client = (ROOT / "static" / "js" / "client" / "webxdc.js").read_text()
        self.assertIn("const SANDBOX_LABEL = 'xdc'", client)
        main = (ROOT / "app" / "main.py").read_text()
        self.assertIn('WEBXDC_SANDBOX_LABEL = "xdc"', main)
        self.assertIn('WEBXDC_LABEL="xdc"', MODULE.read_text())
        from app.services.webxdc_service import SANDBOX_LABEL
        self.assertEqual(SANDBOX_LABEL, "xdc")


class TheStartupCheckCannotBreakABoot(unittest.TestCase):
    def test_a_node_with_no_public_hostname_says_nothing(self):
        from app.services import webxdc_service as w
        for h in ("", "localhost", "nas.lan", "192.168.0.2", "box.local", "example.com"):
            self.assertFalse(w._is_public_hostname(w._host_of(h)), h)
        self.assertTrue(w._is_public_hostname(w._host_of("https://poster.place/blossom")))
        self.assertTrue(w._is_public_hostname(w._host_of("poster.place")))
        # Already the sandbox: deriving xdc.xdc.<host> from it would warn about a name that should
        # not exist.
        self.assertFalse(w._is_public_hostname(w._host_of("https://xdc.poster.place")))

    def test_an_unresolvable_host_warns_once_and_returns(self):
        import asyncio
        import logging
        from unittest import mock
        from app.services import webxdc_service as w
        with mock.patch.object(w, "instance_host", return_value="posterchanai-webxdc-test.invalid"):
            with self.assertLogs(level=logging.WARNING) as cm:
                asyncio.run(w.check_sandbox_host(delay=0))
        joined = "\n".join(cm.output)
        self.assertEqual(len([l for l in cm.output if "[webxdc]" in l]), 1)
        self.assertIn("./install.sh --webxdc", joined)
        self.assertIn("docs/WEBXDC.md", joined)

    def test_it_swallows_everything(self):
        """A diagnostic that can raise is worse than no diagnostic."""
        import asyncio
        from unittest import mock
        from app.services import webxdc_service as w
        with mock.patch.object(w, "instance_host", side_effect=RuntimeError("boom")):
            asyncio.run(w.check_sandbox_host(delay=0))    # must not raise


class TheInstallerIsWiredIn(unittest.TestCase):
    def test_install_sh_parses_and_offers_the_flag(self):
        subprocess.run(["bash", "-n", str(ROOT / "install.sh")], check=True)
        subprocess.run(["bash", "-n", str(MODULE)], check=True)
        src = (ROOT / "install.sh").read_text()
        self.assertIn('source "$INSTALL_DIR/webxdc.sh"', src)
        self.assertIn('if [ "$1" = "--webxdc" ]; then', src)
        self.assertIn("--webxdc", (ROOT / "scripts" / "install" / "utils.sh").read_text())


if __name__ == "__main__":
    unittest.main()
