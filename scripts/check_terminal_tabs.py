#!/usr/bin/env python3
"""Does "New tab" on a REMOTE host open a new SHELL — or a second view of the one on screen?

    venv-unified/bin/python scripts/check_terminal_tabs.py

Reported as "I can never start a new tab on a remote connection. I have 3 server1 connections now",
and every existing terminal check passes against it, because nothing fails: the button works, a
session is created, the strip repaints, the log says `opened a terminal on server1`. What comes back
is the prompt you were already looking at.

The reason is on the far end. A remote shell is opened inside `tmux new-session -A -s
pcai-<uid>-<label>` (ssh_service._mux_name) — attach-or-CREATE — and the label is what says which
tab this is. The client never sent one, so every tab took the server's `main` default and every
press of + made another SSH connection onto ONE tmux session: same screen, same keystrokes, same
scrollback, and the connection count climbing by one per press. Measured on the live node: three
`server1` sessions, two of them holding 567,559 and 567,518 bytes — the same pane, twice.

So this check drives the SHIPPED term.js in a real browser against a fake server that models the one
behaviour that matters: a shell is named (host, label), and opening one that exists ATTACHES. What
is asserted is the distinction the bug destroys —

  shells-shared    N tabs on a host must be N distinct shells, not N connections onto one.
  tab-not-active   a new tab must become the one on screen, or + reads as doing nothing.
  tabs-lost        opening a tab must not close the ones already there.
  tab-wrong-shell  clicking a tab must reattach to THAT shell, by its own label.
  tabs-unnamed     two tabs on one host must not carry the same name — a label renumbered by
                   position renames the tab you are sitting in when a neighbour closes, and says
                   nothing about which shell it is.

Exit 0 pass, 1 fail, 2 could-not-run.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = (shutil.which("chromium") or shutil.which("chromium-browser")
          or shutil.which("google-chrome") or shutil.which("google-chrome-stable"))
TABS = 3                     # the count in the report


def _f(*p):
    return "file://" + os.path.join(ROOT, *p)


PAGE = r"""<!doctype html><meta charset="utf-8">
<link rel="stylesheet" href="%(xtermcss)s">
<link rel="stylesheet" href="%(css)s">
<style>html,body{margin:0;height:100%%}
 .app{display:flex;flex-direction:column;height:100dvh}
 #feed{flex:1;min-height:0;display:flex;flex-direction:column}</style>
<div class="app"><div id="feed" class="feed"></div></div>
<script src="%(xtermjs)s"></script>
<script src="%(fitjs)s"></script>
<script>
/* THE FAR END, modelled down to the one thing that matters: a shell is named (host, label) and
   opening one that already exists ATTACHES to it. That is `tmux new-session -A`, which is what the
   real host runs, and it is why a missing label is not a missing name — it is a shared shell. */
const LOG = [], SHELLS = {}, SESS = [];
let nextId = 1;
function mkSess(host, label){
  label = String(label || 'main');
  const key = host + '/' + label;
  SHELLS[key] = (SHELLS[key] || 0) + 1;            // how many connections landed on this one shell
  const s = { sid: 's' + (nextId++), host, label, shell: key, seq: 0, age: 1 };
  SESS.push(s); return s;
}
mkSess('server1', 'main');                          // one already running, as after a reload

window.__PC_TOKEN__ = 'tok';
class FakeWS {
  constructor(url){ this.url = url; this.readyState = 0;
    setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 5); }
  send(raw){
    const m = JSON.parse(raw);
    LOG.push(m);
    if(m.t !== 'open') return;
    let s;
    if(m.resume){
      s = SESS.find(x => x.sid === m.resume) || mkSess(m.host || 'server1', m.label);
      this._say({ t:'ready', sid:s.sid, host:s.host, label:s.label, resumed:true });
    }else{
      s = mkSess(m.host, m.label);
      this._say({ t:'ready', sid:s.sid, host:s.host, label:s.label });
    }
    this.sess = s;
    setTimeout(() => { s.seq += 20;
      this._say({ t:'out', d:'\r\n[' + s.shell + '] $ ', seq:s.seq }); }, 10);
  }
  _say(o){ setTimeout(() => this.onmessage && this.onmessage({ data: JSON.stringify(o) }), 1); }
  close(){ this.readyState = 3; setTimeout(() => this.onclose && this.onclose(), 1); }
}
window.WebSocket = FakeWS;

const $ = (s, r) => (r||document).querySelector(s);
const $$ = (s, r) => Array.from((r||document).querySelectorAll(s));
window.__PC = {
  $, $$,
  enc: (s) => String(s==null?'':s).replace(/[&<>"']/g,
        c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  toast: () => {}, publish: async () => {}, ensureAiSession: async () => {},
  uiPrompt: async () => '', uiConfirm: async () => true,
  authFetch: async (u) => {
    if(u.indexOf('/api/ssh/hosts') === 0)
      return { ok:true, status:200, json: async () => ({ ok:true, available:true, hosts:[
        { name:'server1', label:'me@server1.lan', keyed:true },
        { name:'nas.lan', label:'me@nas.lan', keyed:true }]}) };
    if(u.indexOf('/api/ssh/sessions') === 0)
      return { ok:true, status:200, json: async () => ({ ok:true, keeper:true, sessions: SESS.map(s => ({
        sid:s.sid, host:s.host, label:s.label, detached:false, age:s.age, idle:0, bytes:s.seq })) }) };
    return { ok:true, status:200, json: async () => ({}) };
  },
};
window.__PC.VIEW = 'terminal';
</script>
<script src="%(termjs)s"></script>
<script>
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const tabs = () => Array.from(document.querySelectorAll('#tty-sessions .tty-tab')).map(t => ({
  sid: t.dataset.tab, host: t.dataset.host, label: t.dataset.label,
  active: t.classList.contains('active'),
  name: ((t.querySelector('b') || {}).textContent || '').trim() }));
const shellOf = (sid) => (SESS.find(s => s.sid === sid) || {}).shell || '';
(async () => {
  const out = { steps: [], err: null };
  window.onerror = (m) => { out.err = String(m); };
  try{
    await PCTerm.render(document.getElementById('feed'));
    await sleep(400);
    for(let i = 0; i < %(tabs)d; i++){
      const plus = document.querySelector('#tty-tab-new');
      if(!plus){ out.err = 'the + button is not in the strip'; break; }
      plus.click();
      await sleep(450);
      out.steps.push({ press: i + 1, sid: PCTerm.sessionId(), shell: shellOf(PCTerm.sessionId()),
                       tabs: tabs() });
    }
    /* AND BACK. Clicking a tab must land in THAT shell — the half a shared `main` also destroyed,
       silently, because every tab did lead to a working prompt. */
    const first = document.querySelector('#tty-sessions .tty-tab');
    first && first.click();
    await sleep(450);
    out.back = { want: first && first.dataset.tab, sid: PCTerm.sessionId(),
                 shell: shellOf(PCTerm.sessionId()),
                 wantShell: shellOf(first && first.dataset.tab), tabs: tabs() };
    out.shells = Object.keys(SHELLS).map(k => ({ shell: k, connections: SHELLS[k] }));
    out.sessions = SESS.length;
  }catch(e){ out.err = String((e && e.stack) || e); }
  document.title = JSON.stringify(out);
})();
</script>"""


def main():
    if not CHROME:
        print("SKIP  no chrome on this node")
        return 2
    for p in ("static/js/client/term.js", "static/css/client.css",
              "static/vendor/xterm/xterm.js", "static/vendor/xterm/fit.js"):
        if not os.path.exists(os.path.join(ROOT, p)):
            print(f"SKIP  {p} is missing — re-point this check")
            return 2

    page = PAGE % {"css": _f("static", "css", "client.css"),
                   "xtermcss": _f("static", "vendor", "xterm", "xterm.css"),
                   "xtermjs": _f("static", "vendor", "xterm", "xterm.js"),
                   "fitjs": _f("static", "vendor", "xterm", "fit.js"),
                   "termjs": _f("static", "js", "client", "term.js"),
                   "tabs": TABS}
    tmp = tempfile.mkdtemp(prefix="pctabs-")
    try:
        path = os.path.join(tmp, "r.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(page)
        try:
            res = subprocess.run(
                [CHROME, "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                 "--window-size=1400,900", "--virtual-time-budget=15000", "--dump-dom",
                 "--allow-file-access-from-files", "file://" + path],
                capture_output=True, text=True, timeout=180).stdout
        except subprocess.TimeoutExpired:
            print("FAIL  chrome timed out")
            return 2
        m = re.search(r"<title>(.*?)</title>", res, re.S)
        if not m:
            print("SKIP  the page produced no measurements — the terminal did not mount")
            return 2
        q = json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&")
                       .replace("&lt;", "<").replace("&gt;", ">"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if q.get("err"):
        print("FAIL  the terminal threw: " + q["err"])
        return 1

    problems = []
    for st in q["steps"]:
        print(f"press {st['press']}: now in {st['shell'] or '(nothing)'} — tabs "
              + ", ".join(f"{t['name']}{'*' if t['active'] else ''}" for t in st["tabs"]))
        act = [t for t in st["tabs"] if t["active"]]
        if len(act) != 1 or act[0]["sid"] != st["sid"]:
            problems.append(("tab-not-active", f"after press {st['press']} the strip does not show "
                                               "the new tab as the one on screen"))
        if len(st["tabs"]) != st["press"] + 1:
            problems.append(("tabs-lost", f"after press {st['press']} the strip holds "
                                          f"{len(st['tabs'])} tabs, not {st['press'] + 1}"))
        names = [t["name"] for t in st["tabs"] if t["host"] == "server1"]
        if len(set(names)) != len(names):
            problems.append(("tabs-unnamed", "two tabs on one host carry the same name: "
                                             + ", ".join(names)))

    print("\nshells on the far end: " + ", ".join(
        f"{s['shell']} ({s['connections']} connection{'s' if s['connections'] != 1 else ''})"
        for s in q["shells"]))
    shared = [s for s in q["shells"] if s["connections"] > 1]
    if shared:
        problems.append(("shells-shared",
                         "; ".join(f"{s['connections']} connections landed on {s['shell']}"
                                   for s in shared)
                         + " — those tabs are one shell wearing several names, which is the whole "
                           "of \"I can never start a new tab\""))
    if len(q["shells"]) != q["sessions"]:
        problems.append(("shells-shared", f"{q['sessions']} sessions are open but only "
                                          f"{len(q['shells'])} shells exist behind them"))

    back = q.get("back") or {}
    print(f"clicked the first tab: wanted {back.get('wantShell')}, landed in {back.get('shell')}")
    if back.get("sid") != back.get("want") or not back.get("shell") \
            or back.get("shell") != back.get("wantShell"):
        problems.append(("tab-wrong-shell", "clicking a tab did not reattach to that tab's shell"))

    if problems:
        print()
        for kind, why in problems:
            print(f"FAIL  {kind}: {why}")
        return 1
    print(f"\nOK  {TABS} new tabs on one remote host are {TABS} distinct shells, each reachable "
          "by its own tab")
    return 0


if __name__ == "__main__":
    sys.exit(main())
