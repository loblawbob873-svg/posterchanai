"""Group DMs keyed exactly as Armada keys them ("yes do the group DMs", 2026-10-27).

A NIP-17 conversation is its participant set, "everyone but me, sorted", comma-joined -- Armada
src/lib/nip17/conversation.ts (dmPeersOf/dmConvKey). The SHIPPED app.js DmKey is sliced out and run under node against
a line-for-line transcription of Armada's dmPeersOf over every shape a rumor takes, so a group started in Armada is the
same conversation here, and a 1:1 keeps the key every existing conversation already has.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ME, A, B, C = ("e" * 64), ("a" * 64), ("b" * 64), ("c" * 64)


def _run(js):
    src = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
    i = src.index("  const DmKey = {")
    j = src.index("\n  };\n", i) + 5
    prog = ("const profOf=()=>({}),needProfile=()=>{},niceNip05=()=>'',NT=()=>({nip19:{npubEncode:p=>'npub'+p}});\n"
            + src[i:j] + "\n" + js)
    r = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# Armada's dmPeersOf, transcribed (src/lib/nip17/conversation.ts, 2026-10-26), and dmConvKey = join(",").
ARMADA = r"""
const isPubkey=v=>/^[0-9a-f]{64}$/.test(v);
function dmPeersOf(rumor,self){const recipients=new Set();for(const [name,value] of rumor.tags){if(name==='p'&&value&&isPubkey(value))recipients.add(value);}
  if(rumor.pubkey!==self){if(!isPubkey(rumor.pubkey))return undefined;const others=new Set(recipients);others.add(rumor.pubkey);others.delete(self);return others.size===0?undefined:[...others].sort();}
  if(recipients.size===0)return undefined;const others=new Set(recipients);others.delete(self);return others.size===0?[self]:[...others].sort();}
const armadaKey=(r,s)=>{const p=dmPeersOf(r,s);return p?p.join(','):'';};
"""


def test_the_conversation_key_is_armadas():
    cases = [
        {"pubkey": A, "tags": [["p", ME]]},                                  # received 1:1
        {"pubkey": ME, "tags": [["p", A]]},                                  # our own 1:1 copy
        {"pubkey": B, "tags": [["p", ME], ["p", A], ["p", C]]},              # received group
        {"pubkey": ME, "tags": [["p", C], ["p", A], ["p", B]]},              # our own group copy, any order
        {"pubkey": ME, "tags": [["p", ME]]},                                 # note to self
        {"pubkey": ME, "tags": []},                                          # unattributable
        {"pubkey": A, "tags": [["p", ME], ["p", "not-a-key"], ["e", B]]},    # junk p / non-p tags ignored
        {"pubkey": "zz", "tags": [["p", ME]]},                               # a sender that is not a key
    ]
    got = _run(ARMADA + "console.log(JSON.stringify(%s.map(r=>[DmKey.ofRumor(r,%r),armadaKey(r,%r)])))"
               % (json.dumps(cases), ME, ME))
    for case, (ours, theirs) in zip(cases, got):
        assert ours == theirs, (case, ours, theirs)
    assert got[0][0] == A and got[1][0] == A, "a 1:1 is no longer keyed by the peer -- every old conversation moves"
    assert got[2][0] == got[3][0] == ",".join(sorted([A, B, C])), got


def test_starting_a_conversation_keys_it_the_same_way():
    got = _run("console.log(JSON.stringify([DmKey.of([%r],%r),DmKey.of([%r,%r,%r],%r),DmKey.of([%r],%r),"
               "DmKey.isGroup(DmKey.of([%r,%r],%r)),DmKey.peers('a,b')]))" % (A, ME, C, A, ME, ME, ME, ME, A, B, ME))
    assert got == [A, ",".join(sorted([A, C])), ME, True, ["a", "b"]], got
