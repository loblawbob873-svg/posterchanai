"""Copy / Paste / Select All in the desktop's right-click menu act on the window that opened it.

Reported on PosterChanOS: "copy and paste buttons on the window still don't work, I selected select
and can't select". The menu used Electron's ROLES (`role:'copy'` …). A role item has no handler of
its own: when clicked, Electron applies it to `BrowserWindow.getFocusedWindow()` AT CLICK TIME. On
Wayland the menu is its own popup surface, and on PosterChanOS app windows are frequently not the
window Electron counts as focused — so the command went to nothing (or to another window), with no
error anywhere. Each edit item is now bound to the webContents the menu was opened on.

This runs the SHIPPED installContextMenu under node with a fake Electron and clicks every item the
way Electron does with no focused window: `click(menuItem, undefined, undefined)`.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

MAIN = Path(__file__).resolve().parents[1] / "desktop/main.js"

RUN = r"""
const src=require('fs').readFileSync(process.argv[1],'utf8');
const at=src.indexOf('function installContextMenu(created)');
let i=src.indexOf('{',at),d=0; for(;i<src.length;i++){ if(src[i]==='{')d++; else if(src[i]==='}'&&--d===0)break; }
const fn=src.slice(at,i+1);
const calls=[]; let built=null;
const wc={ on:(ev,h)=>{ wc.h=h; }, session:{ setSpellCheckerLanguages(){}, addWordToSpellCheckerDictionary(){} },
  cut:()=>calls.push('cut'), copy:()=>calls.push('copy'), paste:()=>calls.push('paste'), selectAll:()=>calls.push('selectAll'),
  replaceMisspelling(){}, copyImageAt(){}, downloadURL(){} };
const Menu={ buildFromTemplate:t=>{ built=t; return { popup(){} }; } };
const f=new Function('Menu','shell','clipboard','ensureSpellLanguages', fn+'; return installContextMenu;')(
  Menu, {openExternal(){}}, {writeText(){}}, ()=>{});
f({ webContents: wc });
const out={};
for(const [name, params] of Object.entries({
  editable:{ isEditable:true, editFlags:{canCut:true,canCopy:true,canPaste:true}, dictionarySuggestions:[] },
  text:{ isEditable:false, editFlags:{canCut:false,canCopy:true,canPaste:false}, dictionarySuggestions:[] } })){
  built=null; calls.length=0; wc.h({}, params);
  const labels=[];
  for(const it of built||[]){
    if(it.type==='separator' || it.enabled===false) continue;
    labels.push(it.label||it.role);
    if(typeof it.click==='function') it.click({}, undefined, undefined);   // Electron, no focused window
  }
  out[name]={ offered:labels, reached:calls.slice() };
}
process.stdout.write(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class EditItemsReachTheirWindow(unittest.TestCase):
    def run_menu(self):
        r = subprocess.run(["node", "-e", RUN, str(MAIN)], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        return json.loads(r.stdout)

    def test_in_a_text_field_every_edit_item_acts_with_no_focused_window(self):
        got = self.run_menu()["editable"]
        self.assertEqual(got["reached"], ["cut", "copy", "paste", "selectAll"], got)

    def test_copying_selected_page_text_acts_too(self):
        got = self.run_menu()["text"]
        self.assertEqual(got["reached"], ["copy"], got)

    def test_the_items_still_read_as_the_usual_words(self):
        got = self.run_menu()["editable"]["offered"]
        for word in ("Cut", "Copy", "Paste", "Select All"):
            self.assertIn(word, got)


if __name__ == "__main__":
    unittest.main()
