"""EVERY COLD LOAD PAYS FOR client.html's SCRIPT TAGS — this bounds that bill.

Written after going looking for fat in the service worker's precache and finding it in the wrong
place. The measurement, so nobody repeats the hour:

  * SHELL in sw.js is 9.70 MB, and 7.88 MB of that is simply the boot payload — the scripts and
    stylesheets client.html loads on every cold start. The precache MIRRORS the page by design
    (tests/test_client_offline_shell.py enforces exactly that), so it is not the thing to trim.
  * Of the 1.84 MB precached but NOT in the page, 1.47 MB is PDF.js, and it is there deliberately:
    the comment beside it records a real report where a cached, decrypted PDF failed offline
    because preview.js was cached without its renderer. Removing it re-opens a fixed bug.
  * The obvious win looked like the thirteen modules that ALREADY have a working on-demand loader
    (`renderModuleView` → `_withModule`) and are hard-linked in the page anyway, which makes that
    loader dead code: sync, concord, webxdc, vault, term, contacts, calendar, code, budget, news,
    websearch, monero-wallet, preview — 1.12 MB. Twelve of the thirteen are referenced from OTHER
    modules (app.js holds 44 references to PCSync alone; os.js holds 17 to PCTerm), so pulling the
    tag does not make them lazy, it makes those references `undefined` at whatever moment they
    fire. Only code.js is view-only, and 64 KB is not worth a refactor.

Doing it properly is an await-at-the-door change per module — the app.js split that was already
looked at and called off. So this file does not slim anything. It turns the measurement into a
ceiling, because the failure mode is not one bad decision, it is drift: nothing anywhere counted
this, and a new 1 MB library added to the page would have cost every cold load on every phone with
nothing to say so.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "templates/client.html").read_text(encoding="utf-8")

#: Headroom over the measured 7.88 MB. A ceiling people can live under, not a straitjacket.
BUDGET_MB = 8.6
#: No single asset should be a surprise. app.js is 2.55 MB and is the reason this is not lower.
BIGGEST_SINGLE_MB = 2.8


def boot_assets():
    """(bytes, path) for every same-origin script/stylesheet client.html pulls in."""
    out = []
    for m in re.finditer(r'<(?:script|link)[^>]*(?:src|href)="(/static/[^"?]+)', PAGE):
        f = ROOT / m.group(1).lstrip("/")
        if f.is_file():
            out.append((f.stat().st_size, m.group(1)))
    return out


def test_the_page_still_loads_what_this_file_thinks_it_does():
    """The check before the check. If the tags change shape this reads zero assets and every
    budget below passes vacuously — which is how a size guard quietly stops guarding."""
    assets = boot_assets()
    assert len(assets) >= 40, f"only found {len(assets)} boot assets — the tag shapes have changed"
    assert any(p.endswith("/app.js") for _, p in assets)


def test_the_cold_load_stays_within_budget():
    """The bill for opening the app on a phone that has never opened it."""
    assets = boot_assets()
    total = sum(s for s, _ in assets) / 1024 / 1024
    worst = sorted(assets, reverse=True)[:6]
    assert total <= BUDGET_MB, (
        f"client.html now costs {total:.2f} MB on a cold load (budget {BUDGET_MB} MB). "
        f"Heaviest: " + ", ".join(f"{p.rsplit('/', 1)[-1]} {s // 1024}KB" for s, p in worst)
        + ". A module with a `renderModuleView` entry does not need a <script> tag here — but "
          "check who else reads its global first, because most of them are read from other modules.")


def test_no_single_asset_quietly_becomes_the_whole_payload():
    for size, path in boot_assets():
        assert size / 1024 / 1024 <= BIGGEST_SINGLE_MB, (
            f"{path} is {size / 1024 / 1024:.2f} MB on its own — if this is a library rather than "
            f"our own code it belongs behind the on-demand loader, not in the page")


def test_the_on_demand_loader_still_exists():
    """The escape hatch the budget message points at. If this goes away, so does the only cheap
    answer to a failing budget, and the message above is advice nobody can take."""
    app = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
    assert "function renderModuleView(" in app
    assert "_withModule(" in app


def test_pdfjs_stays_precached_even_though_the_page_does_not_load_it():
    """Guards the one deliberate exception, by name. Preview.js loads PDF.js only when a PDF is
    opened, so it is easy to read as pre-warm and delete — it is not. A decrypted document on a
    phone with no network needs the renderer that is already on the device."""
    sw = (ROOT / "static/js/client/sw.js").read_text(encoding="utf-8")
    shell = sw[sw.index("const SHELL = ["):]
    shell = shell[:shell.index("\n];")]
    for needed in ("/static/vendor/pdfjs/pdf.min.js", "/static/vendor/pdfjs/pdf.worker.min.js"):
        assert needed in shell, (
            f"{needed} left the precache — that is the offline-PDF failure the comment beside it "
            f"records, back again")
