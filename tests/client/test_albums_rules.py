"""The rules of a NIP-51 picture set (kind 30006), run on the SHIPPED albums.js under node.

An album is a replaceable event, so every edit rewrites the whole list; these are the rules that keep
that from losing photos, plus the NIP-68 shape of each photo. The phone-width UI and the full flow are
scripts/check_albums_mobile.py.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "static/js/client/albums.js"
ME = "a" * 64
E = lambda n: format(n, "064x")


def run(body):
    js = r"""
const vm = require('node:vm');
const ctx = { window: {}, console, Store: { get(){ return null; }, saveEvent(){} }, Relay: {}, CSS: { escape: s => s } };
vm.createContext(ctx);
vm.runInContext(require('fs').readFileSync(process.argv[1], 'utf8'), ctx);
const A = ctx.window.PCAlbumsFactory({ state: { ME: { pubkey: 'ME' } }, _firstImage: () => 'https://x/first.jpg' });
const out = (function(){ BODY })();
process.stdout.write(JSON.stringify(out));
""".replace("BODY", body).replace("'ME'", json.dumps(ME))
    r = subprocess.run(["node", "-e", js, str(SRC)], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_an_album_reads_its_title_cover_and_photos_in_order_without_duplicates():
    ev = {"kind": 30006, "pubkey": ME, "id": E(1), "created_at": 5,
          "tags": [["d", "trip"], ["title", "Trip"], ["description", "Summer"], ["image", "https://c/cover.jpg"],
                   ["e", E(3)], ["e", E(2)], ["e", E(3)], ["e", "not-an-id"], ["p", E(9)]]}
    a = run(f"return A.albumOf({json.dumps(ev)});")
    assert (a["d"], a["title"], a["description"], a["cover"]) == ("trip", "Trip", "Summer", "https://c/cover.jpg")
    assert a["ids"] == [E(3), E(2)]


def test_only_the_newest_version_of_each_album_counts_and_only_the_authors():
    evs = [{"kind": 30006, "pubkey": ME, "id": E(1), "created_at": 5, "tags": [["d", "a"], ["title", "old"]]},
           {"kind": 30006, "pubkey": ME, "id": E(2), "created_at": 9, "tags": [["d", "a"], ["title", "new"]]},
           {"kind": 30006, "pubkey": ME, "id": E(3), "created_at": 7, "tags": [["d", "b"], ["title", "B"]]},
           {"kind": 30006, "pubkey": "b" * 64, "id": E(4), "created_at": 99, "tags": [["d", "a"], ["title", "forged"]]},
           {"kind": 30003, "pubkey": ME, "id": E(5), "created_at": 99, "tags": [["d", "c"]]}]
    got = run(f"return A.latest({json.dumps(evs)}, {json.dumps(ME)}).map(a => [a.d, a.title]);")
    assert got == [["a", "new"], ["b", "B"]]


def test_adding_photos_keeps_every_photo_and_tag_already_there():
    tags = [["d", "a"], ["title", "T"], ["e", E(1)], ["e", E(2)], ["image", "https://c"]]
    got = run(f"return A.withPhotos({json.dumps(tags)}, [{json.dumps(E(2))}, {json.dumps(E(3))}, {json.dumps(E(4))}]);")
    assert [t for t in got if t[0] == "e"] == [["e", E(1)], ["e", E(2)], ["e", E(3)], ["e", E(4)]]
    assert ["title", "T"] in got and ["image", "https://c"] in got


def test_removing_a_photo_drops_only_it_and_the_cover_it_was():
    tags = [["d", "a"], ["e", E(1)], ["e", E(2)], ["image", "https://p2"]]
    got = run(f"return A.withoutPhoto({json.dumps(tags)}, {json.dumps(E(2))}, 'https://p2');")
    assert got == [["d", "a"], ["e", E(1)]]


def test_a_photo_is_a_nip68_picture_with_a_required_imeta():
    got = run("return A.picTags('https://m/p.jpg', { mime: 'image/jpeg', sha: 'ab'.repeat(32), title: 'Beach' });")
    imeta = next(t for t in got if t[0] == "imeta")
    assert "url https://m/p.jpg" in imeta and "m image/jpeg" in imeta and "x " + "ab" * 32 in imeta
    assert ["title", "Beach"] in got and ["m", "image/jpeg"] in got and ["x", "ab" * 32] in got
    # an uploader's own imeta (with dim) is kept, not rebuilt
    got = run("return A.picTags('https://m/q.png', { mime: 'image/png', extra: ['imeta', 'url https://m/q.png', 'dim 640x480'] });")
    imeta = next(t for t in got if t[0] == "imeta")
    assert "dim 640x480" in imeta and "m image/png" in imeta


def test_an_album_is_never_saved_over_a_read_the_relays_did_not_finish():
    """The replaceable-list wipe: "no answer" read as "empty album" and published over the real one."""
    js = r"""
const vm = require('node:vm');
const published = [];
const ctx = { window: {}, console, Store: { get(){ return null; }, saveEvent(){} }, CSS: { escape: s => s },
  Relay: { ready: async () => true, query: async () => { const a = []; Object.defineProperty(a, 'complete', { value: false }); return a; } } };
vm.createContext(ctx);
vm.runInContext(require('fs').readFileSync(process.argv[1], 'utf8'), ctx);
const A = ctx.window.PCAlbumsFactory({ state: { ME: { pubkey: 'x' } }, publish: async (k) => { published.push(k); return { ok: true }; } });
A.editAlbum('trip', t => t).then(() => 'saved', e => 'refused: ' + e.message)
 .then(r => process.stdout.write(JSON.stringify({ r, published })));
"""
    r = subprocess.run(["node", "-e", js, str(SRC)], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["published"] == [] and out["r"].startswith("refused"), out


EDIT_HARNESS = r"""
const vm = require('node:vm');
const published = [];
const SEEN = SEEN_JSON;
const ctx = { window: {}, console, Store: { get(){ return null; }, saveEvent(){} }, CSS: { escape: s => s },
  Relay: { ready: async () => true, query: async () => { const a = SEEN.slice(); Object.defineProperty(a, 'complete', { value: true }); return a; } } };
vm.createContext(ctx);
vm.runInContext(require('fs').readFileSync(process.argv[1], 'utf8'), ctx);
const A = ctx.window.PCAlbumsFactory({ state: { ME: { pubkey: 'ME' } },
  publish: async (k, c, t, o) => { published.push({ kind: k, tags: t, createdAt: o && o.createdAt }); return { ok: true, ev: { kind: k, tags: t, pubkey: 'ME', id: 'f'.repeat(64), created_at: o && o.createdAt } }; } });
(async () => {
  const out = {};
  for (const [name, fn] of Object.entries(CASES)) {
    const before = published.length;
    try { await fn(A); out[name] = { ok: true, published: published.slice(before) }; }
    catch (e) { out[name] = { ok: false, msg: String(e.message), published: published.slice(before) }; }
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def run_edits(seen, cases_js):
    js = EDIT_HARNESS.replace("SEEN_JSON", json.dumps(seen)).replace("CASES", cases_js).replace("'ME'", json.dumps(ME))
    r = subprocess.run(["node", "-e", js, str(SRC)], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_a_complete_read_without_the_album_never_publishes_the_new_photos_alone():
    """Reviewed 2026-09-24: a relay dropping mid-query leaves the query COMPLETE on the others' EOSE, and
    one that never had the album answers complete and empty. Built on that, "add photos" published a set
    holding ONLY the new photos -- newer, so it replaced the real album everywhere."""
    out = run_edits([], """{
      add:    A => A.editAlbum('trip', t => A.withPhotos(t, ['%s'])),
      cover:  A => A.editAlbum('trip', t => A.withTag(t, 'image', 'https://x/c.jpg')),
      rename: A => A.editAlbum('trip', t => A.withTag(t, 'title', 'New')),
      create: A => A.editAlbum('new-one', t => A.withTag(t, 'title', 'Fresh'), { create: true }),
    }""" % E(7))
    for edit in ("add", "cover", "rename"):
        assert out[edit]["ok"] is False and out[edit]["published"] == [], (edit, out[edit])
    assert out["create"]["ok"] and out["create"]["published"][0]["tags"][:2] == [["d", "new-one"], ["title", "Fresh"]]


def test_an_older_copy_than_the_one_on_screen_is_refused_and_a_new_version_is_strictly_newer():
    cur = {"kind": 30006, "pubkey": ME, "id": E(1), "created_at": 4102444800,   # year 2100: "now" is older
           "tags": [["d", "trip"], ["title", "T"], ["e", E(2)]]}
    out = run_edits([cur], """{
      stale: A => A.editAlbum('trip', t => A.withPhotos(t, ['%s']), { known: { created_at: 4102444900 } }),
      add:   A => A.editAlbum('trip', t => A.withPhotos(t, ['%s']), { known: { created_at: 4102444800 } }),
      dup:   A => A.editAlbum('trip', t => t, { create: true }),
    }""" % (E(3), E(3)))
    assert out["stale"]["ok"] is False and out["stale"]["published"] == []
    pub = out["add"]["published"][0]
    assert [t for t in pub["tags"] if t[0] == "e"] == [["e", E(2)], ["e", E(3)]]     # the old photo kept
    assert pub["createdAt"] == 4102444801, "not signed strictly newer than the album it replaces"
    assert out["dup"]["ok"] is False and out["dup"]["published"] == [], "create overwrote an existing album"


def test_every_helper_albums_js_takes_is_handed_over_by_app_js_and_the_profile_can_open_it():
    """The browser check stubs its helpers, so a name missing from app.js's _albumsDeps (undefined at
    runtime: "imetaTagsFor is not a function" after the first upload) would pass it. Read both sides."""
    import re
    albums = SRC.read_text()
    wanted = re.search(r"const \{([^}]*)\} = dep;", albums).group(1)
    wanted = {w.strip() for w in wanted.replace("\n", " ").split(",") if w.strip()}
    app = (ROOT / "static/js/client/app.js").read_text()
    body = re.search(r"function _albumsDeps\(\)\{ return \{(.*?)\}; \}", app, re.S).group(1)
    handed = {w.strip() for w in re.sub(r"state:\s*\{[^}]*\},", "", body).replace("\n", " ").split(",") if w.strip()}
    assert wanted <= handed, f"albums.js needs {sorted(wanted - handed)} but app.js never hands them over"
    for name in wanted:
        assert re.search(rf"function {re.escape(name)}\(|const {re.escape(name)}\s*=", app), f"{name} is not defined in app.js"
    prof = (ROOT / "static/js/client/profile.js").read_text()
    assert "mountAlbums" in re.search(r"const \{([^}]*)\} = dep;", prof).group(1), "profile.js never receives mountAlbums"
    assert re.search(r"_profileDeps\(\)\{ return \{.*?mountAlbums", app, re.S), "app.js never gives profile.js mountAlbums"
