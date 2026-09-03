"""Executable NIP-22 note-thread acceptance tests over the shipped client functions."""
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
STORE = (ROOT / "static/js/client/store.js").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = APP.index(f"function {name}(")
    brace = APP.index("{", start)
    depth = 0
    for pos in range(brace, len(APP)):
        if APP[pos] == "{":
            depth += 1
        elif APP[pos] == "}":
            depth -= 1
            if depth == 0:
                return APP[start:pos + 1]
    raise AssertionError(f"unterminated {name}")


def _node(source: str):
    out = subprocess.run(["node", "-e", source], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_missing_immediate_parent_still_resolves_the_immutable_nip22_root():
    root = {"id": "a" * 64, "kind": 1, "pubkey": "1" * 64, "tags": []}
    nested = {"id": "c" * 64, "kind": 1111, "pubkey": "3" * 64,
              "tags": [["E", root["id"], "wss://root.example", root["pubkey"]],
                       ["K", "1"], ["P", root["pubkey"], "wss://root.example"],
                       ["e", "b" * 64, "wss://parent.example", "2" * 64],
                       ["k", "1111"], ["p", "2" * 64, "wss://parent.example"]]}
    thread_root = _function("_threadRoot").replace("function _threadRoot", "async function _threadRoot", 1)
    js = "\n".join([_function("replyParentTag"), _function("replyParentId"),
        thread_root,
        f"const root={json.dumps(root)}, ev={json.dumps(nested)};",
        "const Store={get:id=>id===root.id?root:null,saveEvent(){}};",
        "const eTagRelays=e=>(e.tags||[]).filter(t=>(t[0]==='e'||t[0]==='E')&&t[2]).map(t=>t[2]);",
        "const fetchEvent=async(id,hints)=>id===root.id?root:null;",
        "_threadRoot(ev,[]).then(x=>console.log(JSON.stringify({rootId:x.rootId,chain:x.chain.map(e=>e.id)})));"
    ])
    got = _node(js)
    assert got == {"rootId": root["id"], "chain": [nested["id"], root["id"]]}


def test_kind1111_counts_as_a_reply_to_its_immediate_parent():
    root_id = "a" * 64
    comment = {"id": "b" * 64, "kind": 1111, "pubkey": "2" * 64,
               "tags": [["E", root_id], ["K", "1"], ["P", "1" * 64],
                        ["e", root_id], ["k", "1"], ["p", "1" * 64]], "content": "reply"}
    js = "\n".join(["let CIDX=null,_cidxErrs=0;const ME={pubkey:'me'};",
        f"const Store={{all:()=>[{json.dumps(comment)}]}};",
        "const _tipNote=()=>null,reactDisp=()=>'',_isSob=()=>false;",
        _function("buildCounts"), _function("countsFor"),
        f"console.log(JSON.stringify(countsFor('{root_id}')));"
    ])
    assert _node(js)["replies"] == 1


def test_comment_storage_and_surfaces_do_not_turn_replies_into_feed_roots():
    feed = STORE.split("feed(filterFn){", 1)[1].split("byKind(kind)", 1)[0]
    assert "ev.kind===1111" not in feed
    assert "kinds:[1,1111], '#e':batch" in APP
    assert "Store.query([{authors:[pk],kinds:[1,1111]}]).filter(isReply)" in APP
    assert "ev.kind===1111?'replied to you'" in APP
    assert "else if(e.kind===1111){cls='reply';ic='💬';txt='replied'" in APP


def test_quotes_remain_separate_from_reply_writes():
    scope = APP[APP.index("function _commentScope(parent)"):APP.index("function niceNip05")]
    assert "q'" not in scope and '"q"' not in scope
    # Reader compatibility is executable in test_client_is_reply: q + matching unmarked e is false.
