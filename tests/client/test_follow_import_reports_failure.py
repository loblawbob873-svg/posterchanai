"""An import whose follow list the relay did not take must SAY so.

`followMany` returned 0 both for "everyone was already followed" and for "the signed list was never
stored", and the import button turned that 0 into "Following 0 more (366 you already followed)" --
a success message over an import that had changed nothing, which is how 287 missing follows went
unnoticed. Runs the SHIPPED function under node with the relay and publish stubbed."""
import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _fn(src: str, head: str) -> str:
    i = src.index(head)
    depth, j = 0, src.index("{", i)
    for k in range(j, len(src)):
        depth += {"{": 1, "}": -1}.get(src[k], 0)
        if depth == 0:
            return src[i:k + 1]
    raise AssertionError("unbalanced " + head)


def _run(publish_result, opts):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    body = _fn((ROOT / "static/js/client/app.js").read_text(), "async function followMany(")
    script = f"""
const ME={{pubkey:'me'}}; const FOLLOWS=new Set(['a']); let persisted=0, pub=null;
const Relay={{query:async()=>[{{kind:3,created_at:1,content:'',tags:[['p','a']]}}]}};
async function publish(k,c,t,o){{ pub=t; return {json.dumps(publish_result)}; }}
function _persistFollows(){{ persisted++; }}
const _pleromaLinked=false;
{body}
followMany(['a','b','c'], {json.dumps(opts)}).then(
  n=>console.log(JSON.stringify({{ok:true,n,follows:[...FOLLOWS].sort(),persisted}})),
  e=>console.log(JSON.stringify({{ok:false,msg:String(e&&e.message),follows:[...FOLLOWS].sort(),persisted}})));
"""
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_an_accepted_list_counts_only_the_new_follows():
    got = _run({"ok": True}, {"skipPleroma": True, "throwOnFail": True})
    assert got == {"ok": True, "n": 2, "follows": ["a", "b", "c"], "persisted": 1}


def test_a_refused_list_is_an_error_for_the_import_and_changes_nothing():
    got = _run({"ok": False, "msg": "blocked: too large"}, {"skipPleroma": True, "throwOnFail": True})
    assert got["ok"] is False and "too large" in got["msg"]
    assert got["follows"] == ["a"] and got["persisted"] == 0


def test_other_callers_keep_the_count():
    got = _run({"ok": False}, {})
    assert got == {"ok": True, "n": 0, "follows": ["a"], "persisted": 0}


def test_the_import_button_asks_for_the_error():
    src = (ROOT / "static/js/client/settings.js").read_text()
    call = src[src.index("#us-plr-import'"):]
    call = call[:call.index("b.disabled=false")]
    assert "followMany(pks" in call and "throwOnFail:true" in call.replace(" ", "")
