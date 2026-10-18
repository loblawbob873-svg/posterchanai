"""Concord bans name the exact account, and a member who is not listed can be banned by npub.

2026-10-18: the owner's ban went to "Dr. Todd Smith" while THE TODDSTR stayed in the community -- the confirmation
said only "Ban Todd?" -- and Todd, Beatbox and Josephus "keep reappearing": a person not on the member list at that
moment could not be banned at all (the only ban was a member-row menu). Runs the SHIPPED helpers under node.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")


def _fn(name):
    """The shipped function's source: from its declaration to its closing line (two-space indent)."""
    start = JS.index("  function %s(" % name)
    return JS[start:JS.index("\n  }\n", start) + 4]


def _run(code):
    src = "\n".join([_fn("banLabel"), _fn("banTargetFromInput"),
                     "const {nip19}=require(%s);" % json.dumps(str(ROOT / "node_modules/nostr-tools/lib/cjs/index.js"))
                     if (ROOT / "node_modules/nostr-tools").exists() else
                     "require('vm').runInThisContext(require('fs').readFileSync(%s,'utf8'));const nip19=NostrTools.nip19;"
                     % json.dumps(str(ROOT / "static/vendor/nostr/nostr.bundle.js")),
                     "globalThis.window=globalThis;", code])
    r = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


TODDSTR = "8e1e3efef6509b9eff4523d8359096a0b6e655707d58b89d5f70f4f8547cc6e0"
DRTODD = "c4e865e3d432d2b5"


def test_the_confirmation_names_the_account_not_just_a_name():
    got = _run("""
const a=banLabel(%s,{display_name:'Todd',nip05:'The_Toddstr@Cordn.net'},nip19.npubEncode(%s));
const b=banLabel(%s,{display_name:'Todd',nip05:'drtodd@cordn.net'},nip19.npubEncode(%s));
console.log(JSON.stringify([a,b]));""" % (json.dumps(TODDSTR), json.dumps(TODDSTR), json.dumps("c4" * 32), json.dumps("c4" * 32)))
    assert got[0] != got[1], "two Todds are offered with the same words"
    assert "The_Toddstr@Cordn.net" in got[0] and "npub1" in got[0]


def test_an_npub_or_hex_bans_and_everything_else_says_why():
    got = _run("""
const npub=nip19.npubEncode(%s), opt={viewer:'aa'.repeat(32),owner:'bb'.repeat(32),decode:v=>nip19.decode(v)};
console.log(JSON.stringify([
  banTargetFromInput(npub,opt), banTargetFromInput('nostr:'+npub,opt), banTargetFromInput('  '+%s.toUpperCase()+' ',opt),
  banTargetFromInput('THE TODDSTR',opt), banTargetFromInput('',opt),
  banTargetFromInput(nip19.npubEncode('aa'.repeat(32)),opt), banTargetFromInput('bb'.repeat(32),opt),
  banTargetFromInput(nip19.noteEncode('cc'.repeat(32)),opt)]));""" % (json.dumps(TODDSTR), json.dumps(TODDSTR)))
    assert [g.get("target") for g in got[:3]] == [TODDSTR] * 3
    assert got[3]["error"] == "That is not an npub."
    assert got[4] == {"error": ""}                                          # cancelled: nothing said
    assert "yourself" in got[5]["error"] and "owner" in got[6]["error"]
    assert got[7]["error"] == "That is not an npub."                        # a note id is not an account


def test_both_member_lists_offer_it_to_whoever_may_ban_and_it_uses_the_real_ban():
    assert JS.count("cc-ban-npub\">Ban by npub…") + JS.count("cc-ban-npub\">Ban by npub") >= 2
    assert "canBanAny=!!current&&!current.local&&!!current.cord&&(isOwner||memberMay(current,viewer.pubkey,'BAN'))" in JS
    bind = JS[JS.index("$$('.cc-ban-npub')"):][:900]
    assert "isOwnerNow?banMember(got.target):staffModerate(got.target,'ban')" in bind


def test_the_ban_permission_is_folded_once_per_control_state_not_per_render():
    """The members list asks "may this viewer ban?" on every render. authorityCitation folds the whole control stream
    (signatures included); asked per render, a busy community held the APK's main thread until the WebView stopped
    answering (every-view smoke test, 2026-10-18). Same controls -> one fold; new controls -> a fresh answer."""
    start = JS.index("  const mayMemo=new WeakMap()")
    end = JS.index("\n  }\n", JS.index("  function memberMay(", start)) + 4
    src = JS[start:end]
    got = _run(src.replace("roomControls", "RC") + """
const RC=new Map(); let calls=0; const allowed=new Set(['bb'.repeat(32)]);
window.PosterCordReader={authorityCitation:(b,w,pk,perm)=>{calls++; if(!allowed.has(pk)) throw new Error('no');}};
const room={communityId:'c1',cord:{bundle:{community_id:'c1',community_root:'r1'}}};
RC.set('c1',[{id:1}]);
const a=[]; for(let i=0;i<50;i++){ a.push(memberMay(room,'bb'.repeat(32),'BAN')); a.push(memberMay(room,'cc'.repeat(32),'BAN')); }
const first=calls;
RC.set('c1',[{id:1},{id:2}]);                          // a new control event: asked again
allowed.delete('bb'.repeat(32));
const after=memberMay(room,'bb'.repeat(32),'BAN');
console.log(JSON.stringify({first, total:calls, yes:a[0], no:a[1], after}));""")
    assert got["first"] == 2, "the control stream was folded %d times for 100 renders" % got["first"]
    assert got["yes"] is True and got["no"] is False
    assert got["total"] == 3 and got["after"] is False, "a new control state kept the old answer"
