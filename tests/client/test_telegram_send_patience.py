"""How long Telegram waits before "Telegram did not answer" (static/js/client/telegram.js `patience`): 15 s for a
read, never 15 s for a send -- an attachment that took ~20 s was reported as unanswered while it went, and every
retry sent it again (2026-10-22). Loads the shipped file in node and asks it."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = r"""
const vm=require('vm'),fs=require('fs');
const win={addEventListener(){},localStorage:{getItem(){return null},setItem(){},removeItem(){}},matchMedia:()=>({matches:false,addEventListener(){}})};
const ctx={window:win,document:{documentElement:{classList:{toggle(){}}},addEventListener(){},querySelector:()=>null},
  localStorage:win.localStorage,setTimeout,clearTimeout,console,FormData:class FormData{},navigator:{},location:{href:''}};
ctx.globalThis=ctx;vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),ctx);
const P=win.PCTelegram._patience,fd=new ctx.FormData();
console.log(JSON.stringify({status:P('/api/tgc/status'),dialogs:P('/api/tgc/dialogs?limit=200',{}),
  text:P('/api/tgc/send',{method:'POST',body:'{}'}),file:P('/api/tgc/send-file',{method:'POST',body:fd}),
  formOther:P('/api/tgc/other',{method:'POST',body:fd})}));
"""


def test_reads_give_up_at_15_s_and_sends_wait_long_enough_to_finish():
    out = subprocess.run(["node", "-e", JS, str(ROOT / "static/js/client/telegram.js")], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["status"] == 15000 and got["dialogs"] == 15000, got
    assert got["text"] >= 60000, got
    assert got["file"] >= 600000 and got["formOther"] >= 600000, got
