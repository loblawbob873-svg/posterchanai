"""A saved Agent task must survive the device it was typed on.

It did not. `9e58badf` stored the list in `pc_nostr_settings` in localStorage and NOWHERE else —
not synced, not backed up, not in the relay-change carry — with both the getter and the setter
wrapping everything in a swallowing try/catch. Every other pref in that panel either syncs to
`pcai:client-prefs` or restores from a relay list, so after ANY localStorage loss they all come
back and the saved tasks are the single visible casualty. That also disguises the cause: it looks
like one feature broke rather than like storage went.

And it is not one failure mode. A moved app origin (the Electron userData dir after an update), a
corrupt blob (`Settings.all()` returns `{}` on any parse error, and the next `set` writes a fresh
object over all 46 keys), or a quota failure inside that swallowing catch all end identically —
silently, with no error and nothing in any log.

So the list is an encrypted kind-30078 doc now, `d=pcai:agent-tasks`, NIP-44 to the user's own key.
Deliberately NOT `pcai:client-prefs`, which is published as PLAINTEXT: these are shell commands and
agent goals, and syncing them in the clear would trade a lost task for a leaked one.

  restored-from-the-account  a device whose localStorage is empty gets its tasks back from the doc
  silence-is-not-empty       relays that never answered must not look like "no saved tasks", and
                             must NOT arm a write — that is the replaceable-doc wipe, and here it
                             would erase the real list using this device's empty cache
  undecryptable-is-not-empty a doc we cannot decrypt is left alone for the same reason
  first-run-migrates         tasks already in localStorage are adopted and published on first read,
                             so shipping this is what puts existing tasks somewhere durable
  writes-are-serialized      a rapid save-then-delete cannot publish two events from one base copy
  check-can-fail             the pre-fix getter/setter, run against the same stubs, loses the task —
                             so a pass here means the fix, not the harness

The functions are extracted from app.js rather than copied, so they cannot drift from what ships.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "static", "js", "client", "app.js")
NODE = shutil.which("node") or shutil.which("nodejs")

TASK = {"name": "Nightly disk check", "mode": "agent", "node": "local", "all": False, "text": "df -h"}


def _fn(src, opener):
    """Pull one top-level function out of app.js by brace counting from its opening line."""
    i = src.index(opener)
    depth, j, started = 0, i, False
    while j < len(src):
        if src[j] == "{":
            depth += 1
            started = True
        elif src[j] == "}":
            depth -= 1
            if started and depth == 0:
                return src[i:j + 1]
        j += 1
    raise AssertionError("could not bound " + opener)


def _line(src, pattern):
    m = re.search(pattern, src, re.M)
    assert m, "gone from app.js — the storage moved: " + pattern
    return m.group(0)


def _extract():
    with open(APP) as fh:
        src = fh.read()
    return "\n".join([
        _line(src, r"^\s*const _AGT_D = .*$"),
        _line(src, r"^\s*let _agtDoc = .*$"),
        _line(src, r"^\s*let _agtChain = .*$"),
        _line(src, r"^\s*let _agtLoading = .*$"),
        _fn(src, "function _agentSavedGet(){"),
        _fn(src, "function _agentSavedLoad(){"),
        _fn(src, "async function _agentSavedRead(){"),
        _fn(src, "function _agentSavedPublish(){"),
        _fn(src, "function _agentSavedSet(list){"),
    ])


# The pre-fix implementation, verbatim from 9e58badf — used only by check-can-fail.
LEGACY = """
function _agentSavedGet(){ try{ return ClientSettings.get('agentSavedTasks', [])||[]; }catch(_){ return []; } }
function _agentSavedSet(list){ try{ ClientSettings.set('agentSavedTasks', list||[]); }catch(_){} }
function _agentSavedLoad(){ return Promise.resolve(_agentSavedGet()); }
"""

HARNESS = """
// A device: its own localStorage, and a relay that may or may not answer.
const LS = %(ls)s;
const ClientSettings = {
  get:(k,d)=> (LS[k]===undefined ? d : LS[k]),
  set:(k,v)=>{ LS[k]=v; },
};
const ME = { pubkey:'aa'.repeat(32) };
const PUBLISHED = [];
const toast = ()=>{};
// The doc on the relay, as the account holds it.
const RELAY_DOC = %(doc)s;          // null = no doc; 'unreachable' = every REQ throws
const DECRYPTABLE = %(dec)s;
const Relay = { query: async ()=>{
  if (RELAY_DOC === 'unreachable') throw new Error('no socket');
  if (RELAY_DOC === null) return [];
  return [{ kind:30078, created_at:1, content:'CT', tags:[['d','pcai:agent-tasks']] }];
}};
// `signer`, the name app.js actually self-encrypts through. Getting this wrong is not hypothetical:
// the first version of the fix called `PC.nip44dec`, lifted from budget.js (a separate module with
// its own handle), and `tests/client/test_app_globals.py` is what caught it — `PC` is undefined in
// app.js, so every read would have thrown into the "undecryptable" catch and silently never synced.
const signer = {
  nip44enc: async (pk, s)=> 'CT:'+s,
  nip44dec: async (pk, ct)=>{ if(!DECRYPTABLE) throw new Error('bad key'); return JSON.stringify(RELAY_DOC); },
};
async function publish(kind, content, tags, opts){ PUBLISHED.push({kind, content, tags}); return {ok:true}; }

%(impl)s

(async () => {
  %(body)s
})().then(()=>{}, e => { console.error(e && e.stack || e); process.exit(1); });
"""


@unittest.skipIf(not NODE, "no node on this node")
class AgentTasksDurable(unittest.TestCase):
    def run_js(self, body, ls=None, doc=None, dec=True, impl=None):
        js = HARNESS % {
            "ls": json.dumps(ls or {}),
            "doc": "'unreachable'" if doc == "unreachable" else json.dumps(doc),
            "dec": "true" if dec else "false",
            "impl": impl if impl is not None else _extract(),
            "body": body,
        }
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise AssertionError("node failed:\n" + r.stderr[-3000:])
        return json.loads(r.stdout)

    OUT = ("process.stdout.write(JSON.stringify("
           "{list:_agentSavedGet(), published:PUBLISHED.length, ls:LS.agentSavedTasks||null}));")

    def test_restored_from_the_account(self):
        """A device with nothing locally gets the tasks back — the whole point of the change."""
        o = self.run_js("await _agentSavedLoad(); " + self.OUT, ls={}, doc=[TASK])
        self.assertEqual([t["name"] for t in o["list"]], ["Nightly disk check"])
        self.assertEqual(o["published"], 0, "reading somebody's task list must not write to it")
        self.assertTrue(o["ls"], "the offline cache was not refreshed from the doc")

    def test_silence_is_not_empty(self):
        """Relays never answered. This must not read as 'no saved tasks', and above all must not
        arm a write — publishing this device's empty cache would erase the real list."""
        o = self.run_js("await _agentSavedLoad(); _agentSavedSet([]); " + self.OUT,
                        ls={"agentSavedTasks": [TASK]}, doc="unreachable")
        self.assertEqual(o["published"], 0,
                         "a write was published from a read that no relay answered — that is the "
                         "replaceable-doc wipe, with the real list on another device")

    def test_undecryptable_is_not_empty(self):
        o = self.run_js("await _agentSavedLoad(); _agentSavedSet([]); " + self.OUT,
                        ls={"agentSavedTasks": [TASK]}, doc=[TASK], dec=False)
        self.assertEqual(o["published"], 0, "a doc we could not decrypt was overwritten")

    def test_first_run_migrates(self):
        """No doc yet + tasks in localStorage → adopt and publish. This is what makes shipping the
        fix actually rescue the tasks people already have."""
        o = self.run_js("await _agentSavedLoad(); " + self.OUT,
                        ls={"agentSavedTasks": [TASK]}, doc=None)
        self.assertEqual(o["published"], 1, "existing local tasks were not migrated to the account")
        self.assertEqual([t["name"] for t in o["list"]], ["Nightly disk check"])

    def test_a_save_after_a_good_read_is_published(self):
        o = self.run_js(
            "await _agentSavedLoad();"
            "_agentSavedSet([{name:'two',mode:'cmd',node:'local',all:false,text:'uptime'}]);"
            "await new Promise(r=>setTimeout(r,50));" + self.OUT,
            ls={}, doc=[TASK])
        self.assertEqual(o["published"], 1)
        self.assertEqual([t["name"] for t in o["list"]], ["two"])

    def test_writes_are_serialized(self):
        """Two saves back to back must not both build from the same base copy."""
        o = self.run_js(
            "await _agentSavedLoad();"
            "_agentSavedSet([{name:'a'}]); _agentSavedSet([{name:'a'},{name:'b'}]);"
            "await new Promise(r=>setTimeout(r,80));"
            "process.stdout.write(JSON.stringify({list:_agentSavedGet(),published:PUBLISHED.length,"
            "last:JSON.parse(PUBLISHED[PUBLISHED.length-1].content.slice(3)),ls:LS.agentSavedTasks||null}));",
            ls={}, doc=[TASK])
        self.assertEqual(o["published"], 2)
        self.assertEqual([t["name"] for t in o["last"]], ["a", "b"],
                         "the last event published was built from a stale copy")

    def test_check_can_fail(self):
        """The pre-fix code, same stubs: the task on the account is invisible to a device whose
        localStorage is empty, and nothing is ever published. That is the reported bug."""
        o = self.run_js("await _agentSavedLoad(); " + self.OUT,
                        ls={}, doc=[TASK], impl=LEGACY)
        self.assertEqual(o["list"], [],
                         "the harness is not reproducing the bug — the legacy code should see "
                         "nothing at all, since it never reads the account")
        self.assertEqual(o["published"], 0)


if __name__ == "__main__":
    unittest.main()
