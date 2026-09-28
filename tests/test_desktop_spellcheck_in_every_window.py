"""Spelling suggestions on right-click in EVERY PosterChan window, not only the desktop surface.

Reported on PosterChanOS: "Need SpellCheck hints in Social, replies, new post, notes, etc. The thing
where you right-click and choose the right spelled word." Electron ships no context menu; this shell
builds one (suggestions → replaceMisspelling, Add to dictionary, cut/copy/paste) -- but installed it
on the main window only. On PosterChanOS, Social, Notes and the composer run in POPPED-OUT windows
(oswin.js, created through setWindowOpenHandler), which got nothing: a red underline and no menu.

Runs the shipped `installContextMenu` and the shipped `did-create-window` handler under node with
fake Electron objects, and right-clicks a misspelled word in a popped-out Social window.
"""
import json
import subprocess
from pathlib import Path

MAIN = (Path(__file__).resolve().parents[1] / "desktop/main.js").read_text()


def _block(start, end_marker):
    i = MAIN.index(start)
    return MAIN[i:MAIN.index(end_marker, i) + len(end_marker)]


SPELL = _block("const _spellSessions = new WeakSet();", "\n}\n")
MENU = _block("function installContextMenu(created) {", "\n}\n")
CHILD = _block("  created.webContents.on('did-create-window', (child, details) => {", "\n  });\n")

HARNESS = r"""
const [spell, menu, child] = process.argv.slice(1);
const popped = [];
const Menu = { buildFromTemplate: items => ({ popup: () => popped.push(items) }) };
const shell = { openExternal(){} }, clipboard = { writeText: async () => {} };
const app = { getLocale: () => 'en-GB' };
function mkWin(){
  const on = {}, calls = [];
  const ses = { availableSpellCheckerLanguages: ['en-US','en-GB','de'], langs: null,
                setSpellCheckerLanguages(l){ this.langs = l; }, addWordToSpellCheckerDictionary: w => calls.push('add:'+w) };
  const wc = { session: ses, on: (n, fn) => { on[n] = fn; }, once(){}, replaceMisspelling: w => calls.push('replace:'+w),
               copyImageAt(){}, downloadURL(){} };
  wc['setWindow' + 'OpenHandler'] = () => {};
  return { webContents: wc, on, calls, ses, once(){} };
}
eval(spell + '\n' + menu);
const created = mkWin();
const pcAppWindows = new Map();
const pcWindowView = url => /pcwin=([a-z]+)/.exec(url||'') ? /pcwin=([a-z]+)/.exec(url)[1] : null;
const isOurs = () => true;
installContextMenu(created);
eval(child);
const social = mkWin();
created.on['did-create-window'](social, { url: 'app://posterchan/index.html?pcwin=social' });
const fn = social.on['context-menu'];
let out = { installed: !!fn };
if (fn) {
  fn({}, { misspelledWord: 'recieve', dictionarySuggestions: ['receive', 'relieve'], isEditable: true,
           editFlags: { canCut: true, canCopy: true, canPaste: true }, mediaType: 'none' });
  const items = popped[popped.length - 1] || [];
  out.labels = items.filter(i => i.label).map(i => i.label);
  const pick = items.find(i => i.label === 'receive'); if (pick) pick.click();
  const add = items.find(i => i.label === 'Add to dictionary'); if (add) add.click();
  out.calls = social.calls; out.langs = social.ses.langs;
}
process.stdout.write(JSON.stringify(out));
"""


def _run():
    r = subprocess.run(["node", "-e", HARNESS, SPELL, MENU, CHILD], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_a_popped_out_window_offers_the_right_spelling():
    out = _run()
    assert out["installed"], "a popped-out PosterChan window has no right-click menu at all"
    assert out["labels"][:2] == ["receive", "relieve"], out["labels"]
    assert "Add to dictionary" in out["labels"]
    assert out["calls"] == ["replace:receive", "add:recieve"], \
        "choosing a suggestion must correct the word in THAT window"


def test_the_dictionary_language_is_set():
    assert _run()["langs"] == ["en-GB", "en-US"]


def test_popped_out_windows_ask_for_spellcheck():
    handler = MAIN[MAIN.index("created.webContents.setWindowOpenHandler(({ url, features }) => {"):]
    prefs = handler[handler.index("webPreferences: {"):handler.index("additionalArguments")]
    assert "spellcheck: true" in prefs
