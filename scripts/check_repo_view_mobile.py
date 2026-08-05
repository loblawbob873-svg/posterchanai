#!/usr/bin/env python3
"""Layout check for the /client git REPO VIEW — run before deploying a change to it.

check_client_mobile.py never opens this screen (it audits the shell + the home feed), which is the
same reason the Meme Builder needed its own check. What is at risk here is one row: the repo header's
action buttons — Copy / Share / Web / Edit / Delete — sitting beside a clone URL that is long, mono,
and unbreakable.

  overflow          The page scrolls sideways. A clone URL is one unbreakable token ~70 chars wide;
                    it must scroll INSIDE its own box, never push the document.
  off-screen        An action button whose right edge is past the viewport — how "the Delete button is
                    gone on my phone" happens with no error anywhere.
  label-overrun     A button squeezed narrower than its own text. The label does not wrap, it spills.
  tap-target        Shorter than a .btn.small (the app's own floor, ~31px): squeezed by a flex parent.
  not-grouped       The action buttons are not children of one flex .rv-acts. As loose children of
                    .rv-clone they wrapped independently and stranded a button on a line of its own;
                    grouping them is the fix, so this asserts the group still exists.
  beside-url        On a phone a button shares a line with the clone URL, which is a ~70-char
                    unbreakable mono token — whatever is next to it is squeezed to nothing.

The owner-only buttons (Edit, Delete) only render for the signed-in owner, and this check runs as a
guest. So it INJECTS clones of them into .rv-acts before measuring: the point is to measure the widest
row the markup can produce, which is exactly the row the owner sees.

    venv-unified/bin/python scripts/check_repo_view_mobile.py [base_url]

Exit 0 = clean, 1 = regressions (printed), 2 = could not run (no Chrome / site unreachable).
"""
import asyncio
import json
import shutil
import subprocess
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://poster.place"
# The PosterChanAI repo's own naddr (30617:4b56bbf4…:posterchanai) — /client/<naddr> opens the repo
# view directly for anyone, logged in or not, which is what makes this checkable without a session.
NADDR = ("naddr1qqx8qmmnw3jhycmgv9hxz6gprpmhxue69uhhyetvv9ujuur0wd6x2u3wwpkxzcm9qgsyk44m7swf9evxa"
         "zyj0t9h3qmwkj0jkxzqs8hc2f39eautul2kh4srqsqqqaue7tufgk")
WIDTHS = [(390, 844, True), (360, 780, True), (1280, 900, False)]
PORT = 9473
PROFILE = "/tmp/pc-repoview-check"

AUDIT = r"""(() => {
  const out = {found:false, overflow:false, offScreen:[], overrun:[], tiny:[], rows:0, acts:0,
               grouped:false, besideUrl:null};
  const view = document.querySelector('.repo-view');
  if (!view) return out;
  out.found = true;

  // Owner-only buttons: clone the markup the owner gets, so the row is measured at its real width.
  const acts = view.querySelector('.rv-acts');
  if (acts) {
    const mk = (cls, label) => {
      if (acts.querySelector('.' + cls)) return;
      const b = document.createElement('button');
      b.className = 'btn btn-ghost small ' + cls;
      b.innerHTML = '<svg class="ic b-ic" aria-hidden="true"><use href="#i-pen"></use></svg>' + label;
      acts.appendChild(b);
    };
    mk('rv-edit', 'Edit');
    mk('rv-delete', 'Delete');
  }

  out.overflow = document.documentElement.scrollWidth > window.innerWidth + 1;
  out.grouped = !!(acts && getComputedStyle(acts).display === 'flex'
                   && view.querySelectorAll('.rv-clone > .btn').length === 0);

  const btns = acts ? [...acts.children] : [];
  out.acts = btns.length;
  const tops = new Set();
  const url = view.querySelector('.rv-clone-url');
  const ur = url && url.getBoundingClientRect();
  btns.forEach(b => {
    const r = b.getBoundingClientRect();
    const label = (b.textContent || '').trim() || b.className;
    if (r.right > window.innerWidth + 1 || r.left < -1)
      out.offScreen.push(label + ' at ' + Math.round(r.left) + '..' + Math.round(r.right)
                         + ' of ' + window.innerWidth);
    if (b.scrollWidth > b.clientWidth + 1)
      out.overrun.push(label + ' ' + b.clientWidth + '<' + b.scrollWidth);
    if (r.height < 30) out.tiny.push(label + ' ' + Math.round(r.height) + 'px tall');
    if (ur && r.top < ur.bottom - 2 && !out.besideUrl)
      out.besideUrl = label + ' shares the clone URL’s line';
    tops.add(Math.round(r.top / 10));   // bucketed: align-items:center leaves same-row tops 1-2px apart
  });
  out.rows = tops.size;
  return out;
})()"""


async def run():
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed")
        return 2

    shutil.rmtree(PROFILE, ignore_errors=True)
    proc = subprocess.Popen(
        ["google-chrome-stable", "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return await drive()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(PROFILE, ignore_errors=True)


async def drive():
    import websockets
    page = None
    for _ in range(60):
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
            page = [t for t in tabs if t["type"] == "page"][0]
            break
        except Exception:
            await asyncio.sleep(0.5)
    if not page:
        print("SKIP  could not start Chrome")
        return 2

    problems = []
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024) as ws:
        n = [0]

        async def call(method, params=None):
            n[0] += 1
            await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == n[0]:
                    return msg.get("result")

        async def js(expr):
            r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
            if r.get("exceptionDetails"):
                return None
            return r["result"].get("value")

        await call("Runtime.enable")
        await call("Page.enable")
        for w, h, mobile in WIDTHS:
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": w, "height": h, "deviceScaleFactor": 2 if mobile else 1,
                        "mobile": mobile})
            await call("Emulation.setTouchEmulationEnabled",
                       {"enabled": mobile, "maxTouchPoints": 5 if mobile else 0})
            await call("Page.navigate", {"url": f"{BASE}/client/{NADDR}"})
            res = None
            for _ in range(20):                     # the repo view needs a relay round-trip to render
                await asyncio.sleep(1.5)
                res = await js(AUDIT)
                if res and res.get("found"):
                    break
            if res is None:
                print(f"SKIP  {w}px: page did not evaluate (site unreachable?)")
                return 2
            label = f"{w}px"
            if not res["found"]:
                print(f"SKIP  {label}: the repo view never rendered (relay unreachable?)")
                return 2
            if res["overflow"]:
                problems.append((label, "overflow", "the page scrolls sideways"))
            for x in res["offScreen"]:
                problems.append((label, "off-screen", x))
            for x in res["overrun"]:
                problems.append((label, "label-overrun", x))
            if not res["grouped"]:
                problems.append((label, "not-grouped", "action buttons are not one flex .rv-acts"))
            if mobile:
                for x in res["tiny"]:
                    problems.append((label, "tap-target", x))
                if res["besideUrl"]:
                    problems.append((label, "beside-url", res["besideUrl"]))
            print(f"{label}: overflow={res['overflow']} buttons={res['acts']} rows={res['rows']} "
                  f"offScreen={len(res['offScreen'])} overrun={len(res['overrun'])} "
                  f"tiny={len(res['tiny'])}")

    if not problems:
        print("OK  repo view layout checks passed")
        return 0
    print()
    for width, kind, detail in problems:
        print(f"FAIL  [{width}] {kind}: {detail}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
