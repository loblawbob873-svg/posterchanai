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
    fire. Only code.js was view-only.

  * AND CODE.JS IS NOT VIEW-ONLY ANY MORE (checked 2026-09-10, when this ceiling was first hit).
    The git web UI's file viewer reads `window.PCCodeHL` from it — deliberately, so there is ONE
    node-tested highlighter and not a second copy. Dropping its tag would leave every file in the
    repo browser unhighlighted, which is the same failure as the twelve above. So the last named
    reclaim is gone: there is nothing cheap left to remove, and the next person to hit this ceiling
    should not spend the hour re-discovering that.

Doing it properly is an await-at-the-door change per module — the app.js split that was already
looked at and called off. (It was DONE on 2026-09-22: see the note on BUDGET_MB.) So this file does not slim anything. It turns the measurement into a
ceiling, because the failure mode is not one bad decision, it is drift: nothing anywhere counted
this, and a new 1 MB library added to the page would have cost every cold load on every phone with
nothing to say so.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "templates/client.html").read_text(encoding="utf-8")

#: Headroom over the measured boot payload. A ceiling people can live under, not a straitjacket.
#:
#: RAISED 8.6 -> 8.8 on 2026-09-10, deliberately and with the arithmetic written down, because that
#: is the whole point of the guard: it stopped a change and made somebody account for the growth.
#: The payload was 8.573 MB before that day's work and 8.602 MB after it — a batch of fixes to the
#: windowed desktop, Concord's scroll and mention picker, and the DM inbox lookup, +29 KB across
#: app.js/os.js/concord.js/sync.js and two stylesheets. It crossed a ceiling it had been creeping
#: toward for months rather than blowing through one.
#:
#: Raising it is the honest option ONLY because the reclaim the docstring used to name is gone (see
#: code.js above). If this is hit again, the answer is the await-at-the-door module split, not
#: another 0.2 MB — and the number below is the record of how many times that has been deferred.
#:
#: RAISED 8.8 -> 9.2 on 2026-09-16 — deferred a second time, and this is the arithmetic. 8.602 MB at
#: a9fc0680a, 8.983 MB at fa5b9bea3, crossing 8.8 at 2bf5cd4e7. Nothing was added carelessly; it is
#: drift over ten commits: Concord's CORD-02..08 protocol work (2bf5cd4e7 membership fragments,
#: cfc67765d rekeys, 9786b2837 refounding, 19d144f4e staff grants + pin lists: ~+63 KB between
#: concord.js and cord-reader.js), the Folder Sync owner routing (a7c71d7a7, +14 KB), and the fixes
#: of 09-11/09-12 (32808997a, 46235fe6e, 2741df9f3, 32dbd43a9: +85 KB, which includes two new page
#: tags — sms-reactions.js in 32808997a and the self-attaching torrents.js, 25 KB, in 32dbd43a9).
#: Measured by summing `git ls-tree -l` over the page's tags at every commit that touched them.
#:
#: THE NEXT RECLAIM HAS A NAME NOW, so the next person need not spend the hour: cord-reader.js is
#: 1.05 MB (12% of the whole payload) and app.js ALREADY loads it on demand
#: (`_withModule('cord-reader.js','PosterCordReader')`, for direct invites and invite links). Its
#: other reader is concord.js, which reads `window.PosterCordReader` synchronously in ~38 places, so
#: dropping the tag is an await at Concord's door, not a tag deletion — the same shape the docstring
#: describes, but for ONE module that is worth a megabyte.
#: THE SPLIT HAPPENED (2026-09-22), and this is what it was worth: app.js was ONE 2.85 MB IIFE and
#: is now 1.37 MB plus twenty factory modules, fifteen of which are fetched on first use. The boot
#: payload measured by this file went 9.17 -> 8.04 MB without removing a single feature, so the
#: ceilings below are no longer the ones being pressed against — the next person to raise one should
#: re-baseline them DOWN to just above what is measured, the way the notes above did on the way up.
#:
#: LOWERED 9.2 -> 8.15 on 2026-10-23: THE CORD-READER RECLAIM NAMED ABOVE, DONE. The ceiling had been
#: reached again (9,648,782 B = 9.20 MB at 8999c0c1b, ~+40 KB of a night's bug fixes over a dozen
#: files), and the answer was this reclaim, not a third raise. cord-reader.js (1,127,446 B) left the
#: page: concord.js loads it at its doors (`cordReaderReady` -- the Communities screen, the background
#: unread sweep, a cold invite link, a mini app's room transport) and sw.js still precaches it
#: (test_cord_reader_stays_precached... below). The doors cost +3,288 B in concord.js and +959 B in
#: app.js (the shared loader, now versioned and retried), so:
#:     9,648,782 - 1,127,446 + 3,288 + 959 = 8,525,583 B = 8.131 MB measured.
#: 8.15 leaves ~20 KB -- a drift detector, as the 2.85 note below argues, not headroom to spend.
#: RAISED 8.15 -> 8.20 later on 2026-10-23, by the measured amount: 8.131 -> 8.152 MB (+22 KB) across the day's
#: flow-test fixes (Texts search/delete/block, scheduled-post errors, web reader, Office save, PWA update/offline,
#: terminal/VM errors, car-controls report) -- many files, a few hundred bytes each, no new page tag. 8.20 leaves
#: ~50 KB: still a drift detector. The next real reclaim is another on-demand module (see the split notes above).
#: LOWERED 8.20 -> 8.15 on 2026-10-26, THE RECLAIM THE NOTES ABOVE ASK FOR. The music player redesign (album art,
#: Now playing) added +27,943 B to the boot (client.css +14.8 KB, musicplayer.js +8.2 KB -- both must stay at boot for
#: the car-button cold resume) and put the page at 8.226 MB. code.js (107 KB, the Code editor + the highlighter) left
#: the page: its screen and Files already loaded it on demand, and the one synchronous reader (git.js's file viewer,
#: window.PCCodeHL) now fetches it beside the file. Measured 8,516,882 B = 8.122 MB; 8.15 leaves ~28 KB.
#: RAISED 8.15 -> 8.16 on 2026-10-27, by the measured amount: 8.150 -> 8.151 MB across the day's fixes after comments
#: were trimmed first (-1,086 B) -- the file-picker/Explorer CSS went to the lazy files-explorer.css instead (deploy 94),
#: and what is left is Appearance's per-section text + menu colours (apptheme.js +867 B, which must paint before the
#: app starts) and Communities' Back-closes-an-issue + Discover-stays-at-the-top (concord.js +647 B). Measured
#: 8,547,025 B = 8.151 MB; 8.16 leaves ~10 KB -- a drift detector, not headroom.
#: RAISED 8.16 -> 8.166 on 2026-10-27, by the measured amount, after reclaiming first: today's long Concord/bell comments
#: condensed (-1,149 B) and the Secure Boot settings page moved to an on-demand module (ossecureboot.js). What is left is
#: GROUP DMs ("yes do the group DMs"): the conversation key and its label (app.js DmKey), the list's group rows, and
#: delivery to every member (dms.js) -- all of it on the boot path, since an incoming DM is filed at login. Measured
#: 8.1656 MB (the New message dialog's recipient chips included); 8.166 leaves ~0.5 KB. The next reclaim is still an on-demand module (see the split notes above).
#: ...and 8.166 -> 8.167 the same evening for the upload resend on a busy store (upload.js, at boot: Files saves through
#: it), its comment cut to one line first: 8.1662 MB measured.
#: ...and 8.167 -> 8.168 (2026-10-28) for _replaceableCreatedAt (app.js): a same-second edit of a replaceable list lost
#: the newer version about half the time (bookmarks in the gate); a boot-path fix, two comments cut first: 8.1676 MB.
#: ...and 8.168 -> 8.170 (2026-10-28) for the event cache written once a minute instead of every 0.7 s (disk wear,
#: store.js: two write queues, the unload spill into localStorage) and the phone-storage bridge hook in hostfiles.js,
#: comments cut first: 8.1697 MB measured.
#: ...and 8.170 -> 8.172 (2026-10-28) for the music-start recorder (musicplayer.js + its Settings row): "opening
#: posterchan apps randomly starts music" left no trace -- presses were logged, starts were not. 8.1716 MB measured.
#: ...and 8.172 -> 8.173 for the always-on long-task count (timeline.js) in Restarts & memory: a tablet's "social ~20 s"
#: could not be told apart as CPU or network. 8.1726 MB measured.
#: ...and 8.173 -> 8.174 for what the page HOLDS in the same report (store/Concord counts, saved-data size): that
#: tablet's 73 MB heap could not be attributed. 8.1732 MB measured.
#: ...and 8.174 -> 8.175 for the Concord invite inbox staying live through the tunnel (dms.js). 8.1741 MB measured.
#: ...and 8.175 -> 8.178 for the COMMAND BAR's hooks in os.js (Ctrl+Space / Ctrl+K / Super+Space, its actions, and the
#: search split into data rows it shares with the Search window). The bar itself (cmdbar.js + its CSS) loads on first
#: open, never at boot. 8.1774 MB measured.
#: ...and 8.178 -> 8.180 for the Snap Layouts CARDS (thirds, two-thirds + a third, half + quarters) and their zones.
#: 8.1792 MB measured.
#: ...and 8.180 -> 8.185 for WORKSPACES (virtual desktops): window ownership, the tray switcher, the switch label, keys and
#: the compositor ticks -- boot code, since every window and the taskbar read it. 8.1845 MB measured.
#: ...and 8.185 -> 8.188 for the Thunderbird-safe calendar parser (quote-aware ical.js, a boot module) and the PHOTOS
#: entry points (nav item, route, deps -- photos.js and its CSS load on first open). 8.1871 MB measured.
#: ...and 8.188 -> 8.192 for the Screen Recorder's entry points and PER-APP VOLUME's boot hook (os.js: every media
#: element plays at its window's level -- it must be in place before anything plays; the mixer panel itself,
#: appmixer.js, loads on first open). 8.1915 MB measured.
#: ...and 8.192 -> 8.195 for the DESKTOP ASSISTANT's runner in os.js (each step is one of os.js's own window,
#: workspace and volume functions, which only it can reach); its panel, deskassist.js, loads on first use. 8.1939 MB.
#: ...and 8.195 -> 8.196 for the ✨ window buttons of Photos and the Screen Recorder (every window has its own) and
#: redrawing AI chat after a sign-in. 8.1952 MB measured.
#: ...and 8.196 -> 8.205 for WORKSPACES covering PosterChanOS's compositor windows (os.js: minimise/restore per workspace,
#: follow a raised window, carry a window along, the startup-app workspace) and the letterboxing rules that stop every
#: shared picture from being cropped (client.css). Photos' Synced tab is in photos.js, which loads on first open.
#: 8.2037 MB measured.
BUDGET_MB = 8.205
#: No single asset should be a surprise. app.js is 2.55 MB and is the reason this is not lower.
# RAISED 2026-09-18, 2.80 -> 2.85, AND DELIBERATELY BY THE MEASURED AMOUNT AND NOT A ROUND NUMBER.
#
# app.js reached 2.82 MB when the Remote Desktop viewer gained real zoom (Fit / 1:1 / free, pointer
# -anchored, panning) plus the host-side resolution request that stops a 4K desktop being encoded and
# sent to a window that cannot show it — measured over a real WebRTC loopback at −56% bytes, −69%
# encode, −72% decode. That is OUR OWN FEATURE CODE, which is the case this assertion's own message
# exempts ("if this is a LIBRARY rather than our own code it belongs behind the on-demand loader").
#
# The ceiling is a DRIFT DETECTOR (see this module's docstring), so it is moved to just above what
# was measured rather than given headroom: 2.85 leaves ~30 KB, so the next addition trips it too and
# has to justify itself here. It is not licence to keep growing app.js — the docstring already
# records that the reclaimable modules are gone and that the split was looked at and called off.
BIGGEST_SINGLE_MB = 2.85


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


def test_cord_reader_stays_precached_even_though_the_page_does_not_load_it():
    """The second deliberate exception, by name, for PDF.js's reason. cord-reader.js is fetched by
    concord.js on first need, so it reads like a module the precache can drop -- but a phone that
    opens Communities offline has nothing else to read its rooms with. And it must stay OUT of the
    page: the 1.1 MB it cost every cold load is what BUDGET_MB above was lowered by."""
    sw = (ROOT / "static/js/client/sw.js").read_text(encoding="utf-8")
    shell = sw[sw.index("const SHELL = ["):]
    shell = shell[:shell.index("\n];")]
    assert "'/static/js/client/cord-reader.js'" in shell, "cord-reader.js left the precache -- Communities stops opening offline"
    assert not any(p.endswith("/cord-reader.js") for _, p in boot_assets()), (
        "cord-reader.js is a <script> tag in client.html again -- 1.1 MB back on every cold load; concord.js "
        "loads it on demand (cordReaderReady)")
