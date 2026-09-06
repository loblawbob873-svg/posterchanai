from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts/check_installed_desktop_account.py").read_text(encoding="utf-8")
RUNNER = (ROOT / "scripts/run_installed_desktop_account.sh").read_text(encoding="utf-8")


def test_installed_account_gate_uses_loopback_cdp_and_requires_authentication():
    assert "http://127.0.0.1:{PORT}" in SCRIPT
    assert "__PC.me && __PC.me()" in SCRIPT
    assert "no authenticated installed PosterChan page" in SCRIPT
    assert "SKIP installed Electron is not attached" in SCRIPT
    assert "sys.exit(2)" in SCRIPT
    assert "for _ in range(100)" in SCRIPT
    assert "await asyncio.sleep(0.2)" in SCRIPT


def test_installed_account_runner_is_headless_isolated_and_bounded():
    assert "WLR_BACKENDS=headless" in RUNNER
    assert "WLR_HEADLESS_OUTPUTS=1" in RUNNER
    assert "PC_DIAGNOSTIC_TOKEN" in RUNNER
    # THE COMPOSITOR IS THE ONE THIS OS RUNS. It started `sway -c /dev/null`, and sway is not
    # installed on PosterChanOS any more -- so the verifier account could not come up at all, and
    # every gate behind it reported a SKIP about CDP rather than about a compositor that never
    # started. Wayfire needs a real config too: no `ipc` plugin, no socket to drive it through.
    assert "nohup wayfire -c" in RUNNER
    assert "plugins = ipc" in RUNNER
    assert "wayfire-*.socket" in RUNNER
    assert "WAYFIRE_SOCKET=" in RUNNER
    assert "--pc-diagnostic-socket" in RUNNER
    # The CODE, not the prose: the comment above the launch names the command it replaced.
    code = "\n".join(l for l in RUNNER.splitlines() if not l.lstrip().startswith("#"))
    assert "sway" not in code, code
    assert "--remote-debugging-address=127.0.0.1" in RUNNER
    assert "/tmp/pc-installed-diagnostic.installedacct12" in RUNNER
    assert "refusing cleanup outside the fixed diagnostic domain" in RUNNER
    assert "cp -a" in RUNNER and "$source/." in RUNNER and "$profile/" in RUNNER
    assert "-maxdepth 1 -name 'Singleton*' -delete" in RUNNER
    assert 'PC_INSTALLED_FIXTURE_DIR="$fixture"' in RUNNER
    assert "PC_INSTALLED_CODE_ROOT=\"$code_root\"" in RUNNER
    assert "check_installed_code_focus.py" in RUNNER


def test_office_only_runtime_uses_a_real_throwaway_login_and_fails_closed():
    assert "PC_INSTALLED_TEST_NSEC_FILE" in SCRIPT
    assert r"/tmp/pc-installed-diagnostic\.[a-z0-9]{12,64}/test\.nsec" in SCRIPT
    assert "TEST_LOGIN" in SCRIPT
    assert "__PC.signAuth('login')" in SCRIPT
    assert "'/api/auth/nostr-login'" in SCRIPT
    assert "office-only mode requires the bounded throwaway diagnostic account" in SCRIPT
    assert "auth bypass" in SCRIPT  # explanatory invariant: setup is an ordinary signed login


def test_throwaway_login_proves_installed_identity_mutation_is_disabled_first():
    assert "DIAGNOSTIC_IDENTITY_GUARD" in SCRIPT
    assert "pcOS.switch('diagnostic-probe',{})" in SCRIPT
    assert "pcOS.provision('diagnostic-probe')" in SCRIPT
    assert "disabled in diagnostics" in SCRIPT
    assert "installed diagnostic lacks the host identity guard; refusing login" in SCRIPT
    assert SCRIPT.index("cdp.eval(DIAGNOSTIC_IDENTITY_GUARD)") < SCRIPT.index("cdp.eval(TEST_LOGIN")


def test_installed_account_gate_checks_real_blossom_render_without_reading_names():
    assert "__PC.switchView('blossom')" in SCRIPT
    assert "folderTiles:q('.fx-home-tile')" in SCRIPT
    assert 'files["folderTiles"] + files["folderChips"] > 0' in SCRIPT
    assert "const idx=__PC.filesIdx()" in SCRIPT
    assert "const pullOk=await idx.ensure()" in SCRIPT
    assert "'/client/files-index'" in SCRIPT
    assert 'files["clientFiles"] == files["serverFiles"]' in SCRIPT
    assert "syncedRoots:q('.syncroot')" in SCRIPT
    assert "PCSync.docs.state(key)" in SCRIPT
    assert "pcFs.scan(f.id,{excludes:f.excludes||[]})" in SCRIPT
    assert 'row["server"] == row["manifest"]' in SCRIPT
    assert 'row["expected"] == row["local"]' in SCRIPT
    assert 'row["skipped"] == 0' in SCRIPT
    assert "syncAudit.push({key" not in SCRIPT
    assert "textContent" not in SCRIPT
    # The diagnostics may count private index entries, but must never serialize the index itself.
    assert '"files": files' not in SCRIPT
    assert '"index":' not in SCRIPT


def test_installed_account_gate_clicks_disposable_native_files_and_cleans_them():
    assert 'TemporaryDirectory(prefix="posterchan-installed-files-")' in SCRIPT
    assert 'PCHostFiles.enter(PATH)' in SCRIPT
    assert "document.querySelector('.fx-home-tile[data-hosthome]')" in SCRIPT
    assert "||document.querySelector('.folder-chip[data-host]')" in SCRIPT
    assert "posterchan-installed.conf" in SCRIPT
    assert '"code" in native_files["confChoices"]' in SCRIPT
    assert '"host" in native_files["confChoices"]' in SCRIPT
    assert "posterchan-installed.svg" in SCRIPT
    assert 'native_files["preview"]' in SCRIPT


def test_installed_account_gate_can_use_a_bounded_fixture_on_the_cdp_machine():
    assert 'os.environ.get("PC_INSTALLED_FIXTURE_DIR"' in SCRIPT
    assert 'startswith("/tmp/posterchan-installed-files-")' in SCRIPT
    assert "contextlib.nullcontext(supplied_fixture)" in SCRIPT


def test_installed_account_gate_uses_and_deletes_a_temporary_office_session():
    assert "posterchan-office-smoke.odt" in SCRIPT
    assert "'/wopi/files/'" in SCRIPT
    assert "application/vnd.oasis.opendocument.text" in SCRIPT
    assert '"/office-code/browser/"' in SCRIPT
    assert "{method:'DELETE'}" in SCRIPT
    assert "finally" in SCRIPT


def test_installed_office_gate_attaches_to_the_real_editor_and_requires_controls():
    # A successful cool.html response is not an interactive editor. Collabora is an
    # out-of-process iframe in Electron, so inspect its own CDP target.
    assert 'Target.getTargets' in SCRIPT
    assert 'Target.attachToTarget' in SCRIPT
    assert 'target.get("type") == "iframe"' in SCRIPT
    assert 'canvas,#document-container,#toolbar-up,.leaflet-container' in SCRIPT
    assert 'editor["workspace"] and editor["controls"] > 0' in SCRIPT
    assert 'not editor["readonly"]' in SCRIPT
    assert "Input.insertText" in SCRIPT
    assert "Input.dispatchKeyEvent" in SCRIPT
    assert 'archive.read("content.xml")' in SCRIPT
    assert 'b"office interactive smoke"' in SCRIPT
    assert '"officeInteractive": True' in SCRIPT
    assert '"officeEditorSaved": True' in SCRIPT
