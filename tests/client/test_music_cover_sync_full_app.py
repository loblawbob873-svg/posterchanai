"""An album cover is found ONCE per account and reaches every other device -- in the SHIPPED bundle.

Asked for 2026-10-26: "make sure desktop does that too and that can sync the artwork to your phone so it doesn't have
to query online every posterchan client". Device A finds the covers (two from the files' own pictures, one from the
online lookup) and stores each album's thumbnail ONCE: AES-GCM with the drive master key, keep-flagged, its address
written as `tg.ca` on every track of the album in the encrypted files index. Device B -- a NEW browser profile, the
same account -- gets that index and shows the covers without reading one track's audio and without asking this node's
cover service. B is checked as a phone (390x844) and as a PosterChanOS window.

  * A: one upload per album with a cover (Neon Skyline's four tracks are one blob), X-Keep on every one, the bytes
    on the wire are not a JPEG/PNG and DO decrypt with the account's key to the JPEG thumbnail;
  * B: covers drawn for all three albums, `__coverReqs == []`, no tag read of any track, nothing uploaded again;
  * twelve tracks of one album: one upload, `ca` on all twelve;
  * a failed upload writes no `ca` (the album is not "done") and the next pass uploads it.

Lives beside test_music_library_views_full_app.py and uses the same library fixture (music_library_fixture.py).
"""
import asyncio
import base64
import json
import os
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client import music_art_fixture as A
from tests.client import music_library_fixture as L
from tests.client.test_music_library_views_full_app import INDEXED, _music, _screen, _tab, _until

CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
SH = L.SHAS
SHOTS = os.environ.get("PC_MUSIC_SHOTS")
COVERED = {  # album -> its tracks, for every album device A finds a cover for
    "Neon Skyline": [SH["a"], SH["b"], SH["c"], SH["d"]],
    "City Lights": [SH["e"], SH["f"]],
    "No Cover Club": [SH["0"]],
}


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def _run(check, init):
    asyncio.run(desktop.with_browser("online", "", check, init))


async def _shot(b, name):
    if not SHOTS:
        return
    Path(SHOTS).mkdir(parents=True, exist_ok=True)
    data = (await b.call("Page.captureScreenshot", {"format": "png"}))["data"]
    Path(SHOTS, name + ".png").write_bytes(base64.b64decode(data))


SETTLED = INDEXED + " && !PCMusicLibrary.st.running && PCMusicLibrary.st.runs>0"
CA = "(()=>{const f=__PC.filesIdx()._norm().files;return Object.fromEntries(Object.entries(f).map(([k,m])=>[k,(m.tg&&m.tg.ca)||null]));})()"
# What the account's other devices get: the encrypted index's records and the wrapped drive key (filesindex.js keeps
# both under the account in localStorage; the relay copy is the same document).
EXPORT = "(()=>{const F=__PC.filesIdx();return {key:F._key(), files:F._norm().files, mk:F._mkWrapped, blobs:Object.assign({},__blobs)};})()"
# Every upload decrypted with the account's key, as any device of the account would.
# Decrypted here with WebCrypto directly (12-byte IV, then AES-GCM), not through the code under test.
DECRYPT = r"""(async()=>{const mk=await __PC.filesIdx()._ensureMK();const out=[];
  const ck=(mk instanceof CryptoKey)?mk:await crypto.subtle.importKey('raw',mk,'AES-GCM',false,['decrypt']);
  for(const u of __uploads){const ct=Uint8Array.from(atob(u.b64),c=>c.charCodeAt(0));
    let plain=null; try{plain=new Uint8Array(await crypto.subtle.decrypt({name:'AES-GCM',iv:ct.slice(0,12)},ck,ct.slice(12)));}catch(e){}
    out.push({sha:u.sha, keep:u.headers['X-Keep']||null, head:[...ct.slice(0,4)], plainHead:plain?[...plain.slice(0,3)]:null});}
  return out;})()"""
# The average colour of each album card's cover -- which picture B drew, not merely that one is there.
COLOURS = r"""(async()=>{const out={};for(const c of document.querySelectorAll('#ma-albums .ma-card')){
  const t=c.querySelector('.ma-card-t').textContent, i=c.querySelector('img.pc-art-img'); if(!i){out[t]=null;continue;}
  if(!i.complete) await new Promise(r=>{i.onload=i.onerror=r;});
  const k=document.createElement('canvas');k.width=k.height=8;const x=k.getContext('2d');x.drawImage(i,0,0,8,8);
  const d=x.getImageData(0,0,8,8).data;let r=0,g=0,b=0;for(let j=0;j<d.length;j+=4){r+=d[j];g+=d[j+1];b+=d[j+2];}
  out[t]=[r,g,b].map(v=>Math.round(v/64));}return out;})()"""


def _device_a():
    """Device A: the default (lookup ON), a full scan, then everything another device of this account would get."""
    got = {}

    async def check(b):
        await _screen(b, 390, 844)
        await _music(b)
        await _until(b, SETTLED + " && __uploads.length>=3", 40, "device A's covers stored")
        await asyncio.sleep(1.5)                           # nothing further goes up after the pass
        got["uploads"] = await b.js("__uploads.map(u=>({url:u.url,sha:u.sha}))")
        got["ca"] = await b.js(CA)
        got["decrypted"] = await b.js(DECRYPT)
        got["export"] = await b.js(EXPORT)

    _run(check, L.PLAIN + L.LIB)
    return got


def _expected_ca(got):
    shas = {u["sha"] for u in got["uploads"]}
    by_album = {al: {got["ca"][t] for t in tracks} for al, tracks in COVERED.items()}
    return shas, by_album


@CHROME
def test_device_a_stores_each_cover_once_encrypted_and_kept():
    got = _device_a()
    shas, by_album = _expected_ca(got)
    assert len(got["uploads"]) == 3 and len(shas) == 3, ("one upload per album with a cover -- four tracks are ONE blob", got["uploads"])
    for al, cas in by_album.items():
        assert len(cas) == 1 and next(iter(cas)) in shas, ("every track of the album names the same stored cover", al, cas)
    assert got["ca"][SH["9"]] is None, ("the unknown album has no cover to share", got["ca"])
    for d in got["decrypted"]:
        assert d["keep"] == "1", ("a synced cover must be exempt from the age cleanup (X-Keep)", d)
        assert d["head"][:3] != [0xFF, 0xD8, 0xFF] and d["head"] != [0x89, 0x50, 0x4E, 0x47], ("a cover went up in the clear", d)
        assert d["plainHead"] == [0xFF, 0xD8, 0xFF], ("the stored cover does not decrypt to the thumbnail with the account's key", d)


SEED_B = r"""(()=>{const X=__X__;
  localStorage.setItem(X.key, JSON.stringify({folders:['Music'],files:X.files,encFolders:[]}));
  if(X.mk) localStorage.setItem(X.key+'_mk', X.mk);
  const F=__PC.filesIdx(); F.mk=null; F._mkWrapped=X.mk;
  for(const [s,b] of Object.entries(X.blobs)) __putBlob(s,b);
  return true;})()"""


async def _seed_b(b, export):
    await b.js(SEED_B.replace("__X__", json.dumps(export)))


@CHROME
@pytest.mark.parametrize("where", ["phone", "window"])
def test_a_fresh_device_shows_the_covers_without_reading_a_track_or_asking(where):
    a = _device_a()
    _, by_album = _expected_ca(a)
    got = {}

    async def check(b):
        await desktop.login(b)
        if where == "phone":
            await _screen(b, 390, 844)
        else:
            await b.until("!!document.querySelector('#os-desk')")
        await _seed_b(b, a["export"])
        await _music(b, login=False)
        sel = "#ma-albums" if where == "phone" else ".osw #ma-albums"
        await _until(b, SETTLED, 40, "device B settled")
        await _tab(b, "albums")
        await _until(b, f"document.querySelectorAll('{sel} .ma-card img.pc-art-img').length>=3", 15, "the synced covers drawn")
        await asyncio.sleep(1.5)
        got["colours"] = await b.js(COLOURS)
        got["coverReqs"] = await b.js("__coverReqs.slice()")
        got["plain"] = await b.js("__plainCalls.slice()")
        got["uploads"] = await b.js("__uploads.length")
        got["gets"] = await b.js("__blobGets.slice()")
        got["errors"] = await b.js("__errors.slice()")
        await _shot(b, f"device-b-{where}-albums")

    _run(check, (L.PLAIN if where == "phone" else "") + L.LIB)
    assert got["coverReqs"] == [], ("device B asked the cover service for a cover device A already found", got["coverReqs"])
    read = [s for tracks in COVERED.values() for s in tracks if s in got["plain"]]
    assert read == [], ("device B read a track's audio for a cover it was handed", read)
    assert got["uploads"] == 0, ("device B stored a cover again", got)
    assert sorted(got["gets"]) == sorted(next(iter(c)) for c in by_album.values()), ("one small download per album", got["gets"])
    c = got["colours"]
    magenta, cyan, yellow = c["Neon Skyline"], c["City Lights"], c["No Cover Club"]
    assert magenta and magenta[0] > 200 and magenta[1] < 60 and magenta[2] > 150, ("Neon Skyline is not its own picture", c)
    assert cyan and cyan[0] < 60 and cyan[1] > 180 and cyan[2] > 200, ("City Lights is not its own picture", c)
    assert yellow and yellow[0] > 200 and yellow[1] > 150 and yellow[2] < 60, ("No Cover Club is not A's looked-up cover", c)
    assert c["Unknown album"] is None, c
    assert not got["errors"], got["errors"]


# Twelve tracks of ONE album, already indexed, with the cover already on this device: the share step alone.
TWELVE = r"""(async()=>{const F=__PC.filesIdx(); F._norm(); F.data.files={};
  for(let i=0;i<12;i++){ const sha=(i.toString(16).padStart(2,'0')+'c0').repeat(16);
    F.data.files[sha]={folder:'Music',enc:true,shared:true,mime:'audio/wav',name:'t'+i,ts:9000-i,
      tg:{v:1,t:'Song '+i,a:'Dozen',al:'Twelve',n:i+1,oc:'-'}}; }
  F._pullDone=true; F._pullOk=true; F._save=async()=>{ F.saveLocal(); return true; }; F.saveLocal(); return true;})()"""
# ...and the album's cover on this device (music.js is loaded by then), then the next library pass.
TWELVE_ART = r"""(async()=>{const G=PCMusicGroups, key=G.albumKey(G.tags({m:{tg:{a:'Dozen',al:'Twelve'}}}));
  const k=document.createElement('canvas'); k.width=k.height=64; const x=k.getContext('2d'); x.fillStyle='#08f'; x.fillRect(0,0,64,64);
  const blob=await new Promise(r=>k.toBlob(r,'image/jpeg',.9)); await PCMusicArt.put(key, blob); PCMusicLibrary.start(); return key;})()"""


@CHROME
def test_twelve_tracks_of_one_album_are_one_upload():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("window.__publishOK=true; 1")
        await b.js(TWELVE)
        await b.js("__PC.switchView('music'); 1")
        await _until(b, SETTLED, 30, "the first pass")
        assert await b.js("__uploads.length") == 0       # no cover on this device yet: nothing to share
        await b.js(TWELVE_ART)
        await _until(b, SETTLED + " && __uploads.length>=1", 30, "the share pass")
        await asyncio.sleep(1.5)
        got["uploads"] = await b.js("__uploads.map(u=>u.sha)")
        got["ca"] = await b.js(CA)

    _run(check, L.PLAIN + L.LIB)
    assert len(got["uploads"]) == 1, ("twelve tracks, one cover, one blob", got["uploads"])
    assert set(got["ca"].values()) == {got["uploads"][0]}, ("every one of the twelve names the one blob", got["ca"])


@CHROME
def test_a_failed_upload_marks_nothing_and_is_tried_again():
    got = {}

    async def check(b):
        await _music(b)               # the fake media server refuses every upload from the start (init below)
        await _until(b, SETTLED + " && __uploads.length>=3", 40, "the uploads attempted (and refused)")
        await asyncio.sleep(1)
        got["failedCa"] = await b.js(CA)
        got["attempts"] = await b.js("__uploads.length")
        # The server is back; the next pass (the next time Music starts its library pass) stores them.
        await b.js("window.__uploadFail=false; PCMusicLibrary.start(); 1")
        await _until(b, "__uploads.filter(u=>u.ok).length>=3 && !PCMusicLibrary.st.running", 30, "the retry")
        await asyncio.sleep(1)
        got["afterCa"] = await b.js(CA)
        got["ok"] = await b.js("__uploads.filter(u=>u.ok).map(u=>u.sha)")

    _run(check, L.PLAIN + L.LIB + "window.__uploadFail=true;")
    assert got["attempts"] >= 3, got
    assert all(v is None for v in got["failedCa"].values()), ("a refused upload marked an album as shared", got["failedCa"])
    for al, tracks in COVERED.items():
        assert {got["afterCa"][t] for t in tracks} <= set(got["ok"]) and got["afterCa"][tracks[0]], ("not stored on the retry", al, got)
