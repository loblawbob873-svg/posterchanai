"""The Electron native bridge belongs only to the bundled app and shipped helper documents."""
import json
import os
import subprocess


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRUST = os.path.join(ROOT, "desktop", "page-trust.js")


def trusted(url):
    script = (
        f"const t=require({json.dumps(TRUST)});"
        f"process.stdout.write(JSON.stringify(t.isTrustedPage({json.dumps(url)},"
        f"{json.dumps(os.path.join(ROOT, 'desktop'))})));"
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_bundled_app_routes_keep_the_bridge():
    assert trusted("app://posterchan/index.html")
    assert trusted("app://posterchan/client?view=settings#network")


def test_only_the_three_shipped_file_pages_keep_the_bridge():
    for name in ("boot.html", "shell.html", "picker.html"):
        assert trusted("file://" + os.path.join(ROOT, "desktop", name) + "?state=1#ready")


def test_arbitrary_and_lookalike_local_html_is_denied():
    for url in (
        "file:///tmp/attack.html",
        "file://" + os.path.join(ROOT, "desktop", "shell.html.evil"),
        "file://" + os.path.join(ROOT, "desktop", "subdir", "..", "attack.html"),
        "file://remotehost/" + os.path.join(ROOT, "desktop", "shell.html"),
    ):
        assert not trusted(url), url


def test_app_origin_lookalikes_are_denied():
    for url in ("app://posterchan.evil/index.html", "app://other/index.html", "https://posterchan/"):
        assert not trusted(url), url


def test_preload_and_main_share_the_same_trust_predicate():
    preload = open(os.path.join(ROOT, "desktop", "preload.js"), encoding="utf-8").read()
    main = open(os.path.join(ROOT, "desktop", "main.js"), encoding="utf-8").read()
    assert "location.protocol === 'file:'" not in preload
    assert "from.startsWith('file://')" not in main
    assert "isTrustedPreloadPage(location.href, preloadDir)" in preload
    assert "return isTrustedPage(from, __dirname)" in main


def test_sandboxed_preload_never_requires_a_relative_module():
    """Electron's sandboxed preload loader rejects ./page-trust even when it exists in app.asar."""
    preload = open(os.path.join(ROOT, "desktop", "preload.js"), encoding="utf-8").read()
    assert "require('./" not in preload
    assert 'require("./' not in preload
    assert "__dirname" not in preload
    assert "--pc-preload-dir=" in preload


def test_screen_source_listing_checks_the_ipc_sender():
    main = open(os.path.join(ROOT, "desktop", "main.js"), encoding="utf-8").read()
    start = main.index("ipcMain.handle('pc:screen:list'")
    handler = main[start:start + 180]
    assert "(e) =>" in handler
    assert "fsGuard(e)" in handler
