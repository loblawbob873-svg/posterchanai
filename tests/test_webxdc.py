"""webxdc mini apps: the zip reader and the attachment parser, as the CLIENT runs them.

    venv-unified/bin/python -m unittest tests.test_webxdc

Both halves are pure, and both fail in ways nothing on screen explains:

  * `zip.js` reads the `.xdc` container. A mis-read offset does not throw — it yields plausible bytes,
    and the app then fails to start with nothing to say whether the ARCHIVE was misread or the app is
    simply broken. So the archives here are built by Python's zipfile (a writer nobody involved
    controls) and read by the shipped parser under node, including the two shapes that are easy to
    get right by accident: a stored (uncompressed) entry, and an archive with a trailing comment,
    which moves the end-of-central-directory record off the end of the file.

  * `appOf` decides whether a post carries an app at all. Wrong, and either every post grows a Play
    button or none of them do — and the second one is silent.

  * The LOADER's boot chain decides when the app's frame is created. Creating it before the service
    worker controls this document sends that navigation to the network, where this origin serves a
    404 by design — so the app never asks for a single file and there is nothing in any log to say
    why. That is not a theory: it is what Firefox did, and the old code's 4-second wait for a
    controller RESOLVED ANYWAY when it timed out, which is a delay rather than a wait. So the real
    script is run here under node against a stubbed service worker.

  * The BRIDGE and the blob SHIM are hand-assembled JavaScript — a template literal and an array of
    lines — served into a frame on another origin, where a syntax error is a silent black rectangle.
    They are parsed here as programs.
"""
import io
import json
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZIP_JS = ROOT / "static" / "js" / "client" / "zip.js"
WEBXDC_JS = ROOT / "static" / "js" / "client" / "webxdc.js"
LOADER = ROOT / "static" / "webxdc-sandbox" / "index.html"


def _loader_js() -> str:
    """The loader's script, as the browser runs it."""
    m = re.search(r"<script>\n(.*)\n</script>", LOADER.read_text(), re.S)
    if not m:
        raise AssertionError("the sandbox loader has no <script> block any more")
    return m.group(1)


def _node(script: str):
    out = subprocess.run(["node", "-e", script], capture_output=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError(out.stderr.decode()[-2000:])
    return json.loads(out.stdout.decode() or "null")


def _xdc(files, comment=b"", compression=zipfile.ZIP_DEFLATED):
    """A real .xdc, built by a writer this repo does not control."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as z:
        for name, data in files.items():
            z.writestr(name, data)
        if comment:
            z.comment = comment
    return buf.getvalue()


def _read(archive: bytes, script: str):
    """Run `script` under node with PCZip loaded and `BYTES` holding the archive."""
    boot = (
        "global.window = {};\n"
        f"const PCZip = require({json.dumps(str(ZIP_JS))});\n"
        f"const BYTES = Uint8Array.from({json.dumps(list(archive))});\n"
        "(async () => {\n" + script + "\n})().catch(e => { console.error(e); process.exit(1); });"
    )
    return _node(boot)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ZipReader(unittest.TestCase):
    def test_it_reads_a_deflated_app(self):
        # Compressible on purpose: a tiny string can end up STORED even in a deflate archive, which
        # would quietly test the wrong branch.
        body = "<!doctype html>" + ("<p>hello webxdc</p>" * 200)
        out = _read(_xdc({"index.html": body, "manifest.toml": 'name = "Chess"\n'}), """
            const files = await PCZip.readAll(BYTES);
            const dec = new TextDecoder();
            console.log(JSON.stringify({
              names: [...files.keys()].sort(),
              html: dec.decode(files.get('index.html')),
              manifest: dec.decode(files.get('manifest.toml')),
            }));
        """)
        self.assertEqual(out["names"], ["index.html", "manifest.toml"])
        self.assertEqual(out["html"], body)
        self.assertIn('name = "Chess"', out["manifest"])

    def test_it_reads_a_stored_entry(self):
        """Method 0 is not deflate with a flag — it is raw bytes, and inflating them fails."""
        out = _read(_xdc({"index.html": "<b>hi</b>", "a.png": "\x00\x01\x02"},
                         compression=zipfile.ZIP_STORED), """
            const files = await PCZip.readAll(BYTES);
            console.log(JSON.stringify({ html: new TextDecoder().decode(files.get('index.html')),
                                         png: [...files.get('a.png')] }));
        """)
        self.assertEqual(out["html"], "<b>hi</b>")
        self.assertEqual(out["png"], [0, 1, 2])

    def test_a_trailing_comment_does_not_hide_the_directory(self):
        """The end-of-central-directory record is last EXCEPT for a comment of up to 64KB, so its
        position is not fixed. Reading the last 22 bytes works until somebody's build tool adds one."""
        out = _read(_xdc({"index.html": "ok"}, comment=b"built by something" * 40), """
            const files = await PCZip.readAll(BYTES);
            console.log(JSON.stringify(new TextDecoder().decode(files.get('index.html'))));
        """)
        self.assertEqual(out, "ok")

    def test_nested_paths_survive_intact(self):
        out = _read(_xdc({"index.html": "x", "js/game.js": "run()", "img/s/p.png": "P"}), """
            const files = await PCZip.readAll(BYTES);
            console.log(JSON.stringify([...files.keys()].sort()));
        """)
        self.assertEqual(out, ["img/s/p.png", "index.html", "js/game.js"])

    def test_a_traversing_name_cannot_climb_out(self):
        """Entry names are attacker-controlled text and become the keys a sandboxed app fetches by."""
        out = _node("global.window={};const Z=require(%s);console.log(JSON.stringify("
                    "['../../etc/passwd','/abs/x.js','a/./b/../c.js','..\\\\..\\\\w.js']"
                    ".map(n=>Z.normalise(n))));" % json.dumps(str(ZIP_JS)))
        self.assertEqual(out, ["etc/passwd", "abs/x.js", "a/c.js", "w.js"])

    def test_something_that_is_not_a_zip_says_so(self):
        """"Not a zip" and "a zip with nothing in it" must not look the same to the UI."""
        out = _read(b"this is not a zip file at all, not even close", """
            try{ await PCZip.readAll(BYTES); console.log(JSON.stringify('no error')); }
            catch(e){ console.log(JSON.stringify(e.message)); }
        """)
        self.assertIn("not a zip", out)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class AttachmentParsing(unittest.TestCase):
    """Which posts carry a mini app. Both shapes from the NIP: an `imeta` tag on any event, and a
    kind-1063 file-metadata event, whose tags are flat instead."""

    def app_of(self, ev):
        boot = (
            "global.window = { addEventListener(){}, __PC: { $:()=>null, enc:s=>s, toast(){}, "
            "publish(){}, me:()=>null, profOf:()=>({}), apiBase:()=>'https://example.com' } };\n"
            "global.document = { addEventListener(){}, querySelectorAll:()=>[], createElement:()=>({"
            "  setAttribute(){}, classList:{add(){}}, appendChild(){}, style:{} }) };\n"
            "global.location = { hostname: 'example.com', href: 'https://example.com/' };\n"
            f"require({json.dumps(str(WEBXDC_JS))});\n"
            f"console.log(JSON.stringify(window.PCWebxdc.appOf({json.dumps(ev)})));"
        )
        return _node(boot)

    def test_an_imeta_attachment_is_found(self):
        ev = {"kind": 1, "content": "let's play", "tags": [
            ["imeta", "url https://blossom.example.com/abc.xdc", "m application/x-webxdc",
             "x " + "a" * 64, "webxdc 9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"]]}
        got = self.app_of(ev)
        self.assertEqual(got["url"], "https://blossom.example.com/abc.xdc")
        self.assertEqual(got["sha"], "a" * 64)
        self.assertEqual(got["uuid"], "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d")

    def test_a_kind_1063_file_event_is_found(self):
        ev = {"kind": 1063, "content": "A collaborative chess game.", "tags": [
            ["url", "https://blossom.example.com/abc.xdc"], ["m", "application/x-webxdc"],
            ["x", "b" * 64], ["alt", "Webxdc app: Chess"], ["webxdc", "u-u-i-d"]]}
        got = self.app_of(ev)
        self.assertEqual(got["url"], "https://blossom.example.com/abc.xdc")
        self.assertEqual(got["uuid"], "u-u-i-d")
        self.assertEqual(got["name"], "Chess", "the alt prefix should not become the app's name")

    def test_an_ordinary_post_carries_nothing(self):
        self.assertIsNone(self.app_of({"kind": 1, "content": "gm", "tags": []}))
        self.assertIsNone(self.app_of({"kind": 1, "content": "pic", "tags": [
            ["imeta", "url https://x.example/a.png", "m image/png"]]}))

    def test_a_non_http_url_is_refused(self):
        """The URL is fetched and its bytes are executed. `javascript:` and `file:` are not apps."""
        for bad in ("javascript:alert(1)", "file:///etc/passwd", "/relative.xdc", ""):
            ev = {"kind": 1, "content": "", "tags": [
                ["imeta", "url " + bad, "m application/x-webxdc"]]}
            self.assertIsNone(self.app_of(ev), f"{bad!r} was accepted as an app")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class HandAssembledJavaScript(unittest.TestCase):
    """Two programs in this feature are BUILT AS TEXT and never parsed by anything here: the
    `window.webxdc` bridge (a template literal in webxdc.js) and the blob path's fetch shim (an array
    of lines in the loader). Both are served into a document on another origin, where a syntax error
    produces no toast, no console the reader will ever see, and no request — just a black rectangle.
    """

    def check(self, source: str, what: str):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(source)
            path = fh.name
        try:
            out = subprocess.run(["node", "--check", path], capture_output=True, timeout=60)
            if out.returncode != 0:
                raise AssertionError(f"{what} is not a valid program:\n" + out.stderr.decode()[-2000:])
        finally:
            Path(path).unlink(missing_ok=True)

    def test_the_injected_bridge_parses(self):
        src = WEBXDC_JS.read_text()
        # Up to the first UNESCAPED backtick: the bridge's own comments contain escaped ones.
        m = re.search(r"const BRIDGE = `((?:[^`\\]|\\.)*)`;", src, re.S)
        self.assertTrue(m, "BRIDGE is no longer a template literal — update this test")
        body = m.group(1)
        # As it is SERVED: the ${…} interpolations are numbers, and the escaped backticks in its
        # comments are backticks again.
        body = re.sub(r"\$\{[^}]*\}", "0", body).replace("\\`", "`")
        self.check("var __XDC = { addr:'', name:'', ns:'x' };\n" + body, "the injected webxdc bridge")

    def test_the_bridge_offers_exactly_the_api_apps_look_for(self):
        """`window.webxdc` IS the contract with every app in the ecosystem, and apps feature-detect it
        rather than trusting it: Quake III refuses to start when `joinRealtimeChannel` is undefined,
        and Half-Life throws its own error from `electHost` for the same reason — surfaced to the
        reader as "make sure this app is running inside a WebXDC-compatible messenger", which names
        the messenger and not the missing method. A property dropped from the hand-assembled string
        should fail here instead."""
        src = WEBXDC_JS.read_text()
        m = re.search(r"const BRIDGE = `((?:[^`\\]|\\.)*)`;", src, re.S)
        body = re.sub(r"\$\{[^}]*\}", "1000", m.group(1)).replace("\\`", "`")
        out = _node("""
        const vm = require('vm');
        const sent = [], listeners = [];
        global.window = { addEventListener(t, fn){ if(t === 'message') listeners.push(fn); } };
        global.parent = { postMessage(m){ sent.push(m); } };
        global.document = { createElement: () => ({ getContext: () => ({}) }) };
        vm.runInThisContext(%s);
        const w = window.webxdc, ch = w.joinRealtimeChannel();
        let threw = false;
        try { ch.send([1, 2, 3]); } catch (e) { threw = true; }
        /* THE CODEC, BOTH WAYS, THROUGH THE PUBLIC API. Every packet crosses two frames as base64,
           and the bytes a game sends are binary: high bytes, zeroes, and CR/LF pairs are exactly
           what a careless string round trip mangles — silently, into a packet the peer's app
           discards as malformed. */
        const BYTES = [0, 1, 127, 128, 200, 255, 13, 10, 0];
        ch.send(Uint8Array.from(BYTES));
        const outgoing = sent.filter(m => m.method === 'webxdc.rtSend').pop();
        let heard = null;
        ch.setListener((data) => { heard = [...data]; });
        for (const fn of listeners) fn({ data: { jsonrpc:'2.0', method:'webxdc.realtime',
                                                 params:{ b64: outgoing.params.b64 } } });
        console.log(JSON.stringify({ keys: Object.keys(w).sort(), channel: Object.keys(ch).sort(),
          addr: w.selfAddr, name: w.selfName, max: typeof w.sendUpdateMaxSize,
          every: typeof w.sendUpdateInterval, rejectsPlainArrays: threw,
          wire: outgoing.params.b64, sent: BYTES, heard }));
        """ % json.dumps('var __XDC = { addr:"npub1abc", name:"Ann", ns:"game" };\n' + body))
        self.assertEqual(out["heard"], out["sent"], "the realtime codec does not round-trip bytes")
        self.assertEqual(out["wire"], "AAF/gMj/DQoA", "the wire form is not base64 of those bytes")
        self.assertEqual(out["keys"], ["joinRealtimeChannel", "selfAddr", "selfName", "sendUpdate",
                                       "sendUpdateInterval", "sendUpdateMaxSize", "setUpdateListener"])
        self.assertEqual(out["channel"], ["leave", "send", "setListener"])
        self.assertEqual(out["addr"], "npub1abc")
        self.assertEqual(out["name"], "Ann")
        self.assertEqual(out["max"], "number")
        self.assertEqual(out["every"], "number")
        self.assertTrue(out["rejectsPlainArrays"], "realtime data must be a Uint8Array, per the spec")

    def test_the_blob_fallback_shim_parses(self):
        """It is an ARRAY OF STRINGS joined with newlines, so a missing comma or an unbalanced quote
        is a runtime surprise inside somebody else's app rather than a build error here."""
        m = re.search(r"var SHIM_SRC = (\[.*?\])\.join\('\\n'\);", _loader_js(), re.S)
        self.assertTrue(m, "SHIM_SRC is no longer an array of lines — update this test")
        shim = _node("console.log(JSON.stringify((%s).join('\\n')));" % m.group(1))
        self.check(shim, "the blob fallback's fetch shim")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TheLoaderWaitsForItsWorker(unittest.TestCase):
    """THE APP FRAME MUST NOT BE CREATED WHILE THE WORKER IS NOT CONTROLLING THIS DOCUMENT.

    That frame navigates to `/`, and the only thing that can answer it is the worker — the app's
    files are not on the server at all, so the network answers 404 by design. Build the frame a
    moment too early and the app loads a 404 page: it asks for nothing, draws nothing, throws
    nothing, and the only symptom is the parent reporting that the sandbox never asked for a file.
    Chromium's `clients.claim()` hides it; Firefox's timing did not.

    The old code waited 3-4 seconds for a `controllerchange` and then built the frame REGARDLESS,
    which is why a stubbed run is the only test that can catch this: the code looked like a wait.
    """

    def run_loader(self, controlled: bool, hash_: str = ""):
        harness = """
        const vm = require('vm'), fs = require('fs');
        const SRC = fs.readFileSync(%s, 'utf8');
        const out = { said: [], appended: [], toParent: [], reloads: 0 };
        // Collapse the loader's own waits so the whole boot chain settles inside this test, but keep
        // them as TIMERS: a resolved promise must still win the race against a timeout, as it does
        // in a browser, or this harness would prove the opposite of what it is asked.
        const realSetTimeout = setTimeout;
        global.setTimeout = (fn, ms) => realSetTimeout(fn, Math.min(ms || 0, 5));
        const msg = { _t: '', set textContent(v){ this._t = v; out.said.push(v); },
                      get textContent(){ return this._t; }, remove(){ out.msgRemoved = true; } };
        const frames = [];
        global.document = {
          getElementById(id){ return id === 'm' ? msg : (id === 'f' ? (frames[frames.length-1] || null) : null); },
          createElement(tag){ return { tag, id:'', src:'', style:{}, setAttribute(k, v){ this[k] = v; },
                                       addEventListener(){}, remove(){}, contentWindow:{ postMessage(){} } }; },
          body: { appendChild(e){ frames.push(e); out.appended.push({ id: e.id, src: e.src }); } },
          addEventListener(){},
        };
        const listeners = [];
        const parentStub = { postMessage(m){ out.toParent.push(m); } };
        global.parent = parentStub;
        global.window = { addEventListener(t, fn){ if(t === 'message') listeners.push(fn); },
                          removeEventListener(){} };
        global.location = { origin: 'https://xdc.example', href: 'https://xdc.example/__sandbox__/',
                            pathname: '/__sandbox__/', hash: %s, reload(){ out.reloads++; } };
        // defineProperty, not assignment: node ships its own read-only `navigator`, and a plain
        // assignment silently does nothing — the loader then reports "no service worker" and every
        // assertion below passes for the wrong reason.
        Object.defineProperty(global, 'navigator', { configurable: true, value: { serviceWorker: {
          controller: %s, register(){ out.registered = true; return Promise.resolve({}); },
          ready: Promise.resolve({}), addEventListener(){}, } } });
        vm.runInThisContext(SRC);
        // The parent answers the loader's `ready` with `init`, which is what starts the boot chain.
        for(const fn of listeners) fn({ source: parentStub, origin: 'https://client.example',
          data: { jsonrpc: '2.0', method: 'init', params: { version: 1 } } });
        realSetTimeout(() => console.log(JSON.stringify(out)), 120);
        """ % (json.dumps(str(LOADER.parent / "loader.tmp.js")), json.dumps(hash_),
               "{}" if controlled else "null")
        script = LOADER.parent / "loader.tmp.js"
        script.write_text(_loader_js())
        try:
            return _node(harness)
        finally:
            script.unlink(missing_ok=True)

    def test_an_uncontrolled_loader_reloads_instead_of_framing_a_404(self):
        out = self.run_loader(controlled=False)
        self.assertEqual(out["reloads"], 1, "an uncontrolled loader must reload to become controlled")
        self.assertEqual([a for a in out["appended"] if a["id"] == "f"], [],
                         "the app frame was created while nothing was controlling this document")

    def test_a_controlled_loader_frames_the_app_at_the_origin_root(self):
        out = self.run_loader(controlled=True)
        self.assertEqual(out["reloads"], 0, "a controlled loader must not reload")
        self.assertEqual([a["src"] for a in out["appended"] if a["id"] == "f"], ["/"],
                         "the app owns the origin root; the loader lives at /__sandbox__/")

    def test_it_reloads_only_once_and_then_falls_back(self):
        """A reload loop is worse than the bug it fixes — the reader watches a frame flicker for
        ever. After one attempt the blob path takes over, which at least serves the simple apps."""
        out = self.run_loader(controlled=False, hash_="#pcxdc-reloaded")
        self.assertEqual(out["reloads"], 0, "it reloaded twice — that is a loop")
        self.assertTrue(any(m.get("method") == "fetch" for m in out["toParent"]),
                        "nothing took over: the reader gets a dead status line, not the app")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TwoPlayers(unittest.TestCase):
    """THE RECEIVE PATH, WHICH NOTHING HAD EVER RUN. Sending was proven in production the moment a
    game moved — packets on the relay, two senders, the right identifier. Receiving is the other
    half and every way it can fail is silent: a filter that matches nothing, a self-drop that drops
    everybody, a base64 round trip that mangles high bytes, an `onEvent` that never fires.

    So: two Sessions in one process against a relay stub that matches filters the way NIP-01 says
    (kinds, single-letter tags and `since`, which is the one that bites). One sends; the other must
    hand its app the identical bytes, and the sender must not hear its own echo.
    """

    def play(self, extra="", since_skew=0):
        return _node("""
        const APP = 'game-uuid-1';
        const out = { published: 0, A: [], B: [], filters: [] };
        function matches(f, ev){
          if (f.kinds && !f.kinds.map(Number).includes(Number(ev.kind))) return false;
          if (f.since != null && Number(ev.created_at) < Number(f.since)) return false;
          for (const k of Object.keys(f)){
            if (k.length === 2 && k[0] === '#'){
              const want = new Set(f[k].map(String));
              const have = new Set((ev.tags||[]).filter(t=>t.length>=2 && t[0]===k[1]).map(t=>String(t[1])));
              if (![...want].some(v=>have.has(v))) return false;
            }
          }
          return true;
        }
        let sn = 0, kn = 0;
        const RELAY = {
          subs: [],
          subscribe(filters, opts){ const id='s'+(++sn); RELAY.subs.push({id, filters, onEvent:opts&&opts.onEvent});
                                    out.filters.push(JSON.parse(JSON.stringify(filters))); return id; },
          close(id){ RELAY.subs = RELAY.subs.filter(s=>s.id!==id); },
          publishFast(ev){ out.published++; ev = Object.assign({}, ev, { created_at: ev.created_at + (%d) });
            for (const s of RELAY.subs) if (s.onEvent && (s.filters||[]).some(f=>matches(f, ev))) s.onEvent(ev);
            return true; },
          query: async () => [],
        };
        const NT = {
          generateSecretKey(){ const a = new Uint8Array(32); a[0] = ++kn; return a; },
          getPublicKey(sk){ return 'pk' + sk[0]; },
          finalizeEvent(t, sk){ return Object.assign({}, t, { pubkey: NT.getPublicKey(sk), id: 'ev'+(++kn), sig:'s' }); },
        };
        global.window = { addEventListener(){}, removeEventListener(){},
          NostrTools: NT, Relay: RELAY, Store: { query: () => [] },
          __PC: { $: () => null, enc: s => s, toast(){}, publish: async () => ({ ok:true, ev:null }),
                  me: () => ({ pubkey:'me', npub:'npub1me' }), profOf: () => ({}),
                  apiBase: () => 'https://example.com' } };
        global.Relay = RELAY;
        global.document = { addEventListener(){}, querySelectorAll:()=>[],
          createElement:()=>({ setAttribute(){}, classList:{add(){}}, appendChild(){}, style:{} }) };
        global.location = { hostname:'example.com', href:'https://example.com/' };
        global.crypto = require('crypto').webcrypto;
        require(%s);

        const S = window.PCWebxdc.Session;
        function mk(tag){
          const s = new S({ url:'https://x/a.xdc', uuid: APP, name:'g' },
                          { index:new Map(), bytes:new Uint8Array() });
          s.origin = 'https://xdc.example';
          s.frame = { contentWindow: { postMessage(m){ out[tag].push(m); } } };
          return s;
        }
        const A = mk('A'), B = mk('B');
        A.onRpc({ jsonrpc:'2.0', id:1, method:'webxdc.rtJoin', params:{} });
        B.onRpc({ jsonrpc:'2.0', id:1, method:'webxdc.rtJoin', params:{} });
        // High bytes, a zero, and a CR/LF pair — everything a naive string round trip mangles.
        const BYTES = [2, 0, 255, 128, 13, 10, 0, 254];
        A.onRpc({ jsonrpc:'2.0', id:2, method:'webxdc.rtSend',
                  params:{ b64: Buffer.from(BYTES).toString('base64') } });
        %s
        setTimeout(() => {
          const rt = (a) => a.filter(m => m.method === 'webxdc.realtime');
          console.log(JSON.stringify({
            published: out.published, filters: out.filters[0],
            heard: rt(out.B).map(m => [...Buffer.from(m.params.b64, 'base64')]),
            ownEcho: rt(out.A).length, sent: BYTES,
            keys: [A.rtPk, B.rtPk],
          }));
          process.exit(0);
        }, 60);
        """ % (since_skew, json.dumps(str(WEBXDC_JS)), extra))

    def test_a_packet_one_player_sends_is_the_packet_the_other_receives(self):
        out = self.play()
        self.assertEqual(out["published"], 1)
        self.assertEqual(out["heard"], [out["sent"]],
                         "the other player heard nothing, or heard different bytes")

    def test_a_player_does_not_hear_their_own_echo(self):
        """An app that folds its own movement back into its state sees every player twice."""
        out = self.play()
        self.assertEqual(out["ownEcho"], 0)
        self.assertNotEqual(out["keys"][0], out["keys"][1],
                            "both sessions share a channel key — the self-drop would silence both")

    def test_a_peer_whose_clock_is_behind_is_still_heard(self):
        """`since` is compared against the SENDER's `created_at`, so a peer a few seconds behind had
        every packet dropped by the relay for the whole session — an OK on their side, silence on
        ours. Two browsers on one machine share a clock and never show it; a phone and a laptop do."""
        out = self.play(since_skew=-5)
        self.assertEqual(out["heard"], [out["sent"]],
                         "a peer with a slightly slow clock is inaudible")
        self.assertLess(out["filters"][0]["since"], int(time.time()) - 1,
                        "the subscription's `since` must leave room for clock skew")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TheWorkerServesTheRIGHTApp(unittest.TestCase):
    """ONE ORIGIN MEANS ONE SERVICE WORKER FOR EVERY MINI APP, so the worker has to decide which open
    game each request belongs to. It used to take the first client whose path looked like a loader —
    and with Half-Life still open, pressing Play on Quake III started Half-Life. That is one app's
    bytes delivered into another app's frame, so it is a leak as well as a mix-up.

    The rule is: answer only the loader holding this instance's token, and REFUSE when it cannot be
    told. These run the shipped worker under node with stubbed clients, because the wrong answer is
    a perfectly successful response — nothing throws, nothing logs, the wrong game just starts.
    """

    def route(self, clients, url, client_id="", resulting="", referrer=""):
        """Returns which loader (by id) the worker asked, or the refusal it produced instead."""
        harness = """
        const vm = require('vm'), fs = require('fs');
        const SRC = fs.readFileSync(%s, 'utf8');
        const asked = [];
        const CLIENTS = %s.map(c => Object.assign({}, c, {
          postMessage(msg, transfer){
            asked.push(c.id);
            // Answer on the port so the response completes, exactly as the loader does.
            transfer[0].postMessage({ ok: true, res: { status: 200, headers: {}, body: btoa('bytes') } });
          },
        }));
        const handlers = {};
        Object.defineProperty(global, 'self', { configurable: true, value: {
          addEventListener(t, fn){ handlers[t] = fn; },
          location: { origin: 'https://xdc.example' },
          skipWaiting(){}, registration: {},
          clients: {
            matchAll: async () => CLIENTS,
            get: async (id) => CLIENTS.find(c => c.id === id),
            claim: async () => {},
          },
        } });
        vm.runInThisContext(SRC);
        const request = { url: %s, method: 'GET', headers: new Headers(), referrer: %s };
        let answer = null;
        handlers.fetch({ request, clientId: %s, resultingClientId: %s,
                         respondWith(p){ answer = p; } });
        (async () => {
          const res = answer ? await answer : null;
          console.log(JSON.stringify({ asked, status: res ? res.status : null,
                                       body: res ? await res.text() : null }));
          // An open MessagePort keeps node's event loop alive for ever — the worker holds one per
          // request by design, so the run has to end itself rather than wait to be timed out.
          process.exit(0);
        })();
        """ % (json.dumps(str(ROOT / "static" / "webxdc-sandbox" / "sw.js")),
               json.dumps(clients), json.dumps(url), json.dumps(referrer),
               json.dumps(client_id), json.dumps(resulting))
        return _node(harness)

    HALFLIFE = {"id": "loader-hl", "url": "https://xdc.example/__sandbox__/?__xdc=tok-hl"}
    QUAKE = {"id": "loader-q3", "url": "https://xdc.example/__sandbox__/?__xdc=tok-q3"}

    def test_a_second_game_is_not_served_by_the_first_ones_loader(self):
        """The reported bug, exactly: Half-Life open, Quake III pressed, Half-Life starts."""
        clients = [self.HALFLIFE, self.QUAKE,
                   {"id": "app-q3", "url": "https://xdc.example/?__xdc=tok-q3"}]
        out = self.route(clients, "https://xdc.example/ioquake3.wasm", client_id="app-q3")
        self.assertEqual(out["asked"], ["loader-q3"],
                         "the worker asked the wrong game for this app's files")

    def test_a_navigation_is_routed_by_the_token_in_its_url(self):
        """A navigation has no client id at all — the token in the URL is the only thing there is."""
        out = self.route([self.HALFLIFE, self.QUAKE], "https://xdc.example/?__xdc=tok-hl",
                         client_id="", resulting="app-hl")
        self.assertEqual(out["asked"], ["loader-hl"])

    def test_an_in_app_link_is_routed_by_its_referrer(self):
        """An app that navigates to its own second page (Quake III has a main menu) inherits no
        query — but the page it came from is the referrer, and that carries the token."""
        out = self.route([self.HALFLIFE, self.QUAKE], "https://xdc.example/main-menu.html",
                         referrer="https://xdc.example/?__xdc=tok-q3")
        self.assertEqual(out["asked"], ["loader-q3"])

    def test_an_unattributable_request_is_refused_rather_than_guessed(self):
        out = self.route([self.HALFLIFE, self.QUAKE], "https://xdc.example/game.js")
        self.assertEqual(out["asked"], [], "it guessed — which is the whole bug")
        self.assertEqual(out["status"], 502)

    def test_one_open_game_still_needs_no_token(self):
        """A client too old to mint one is not ambiguous when it is the only game open, and must
        keep working — the worker updates the moment it is fetched, the client may be cached."""
        out = self.route([self.HALFLIFE], "https://xdc.example/game.js")
        self.assertEqual(out["asked"], ["loader-hl"])

    def test_a_closed_game_is_not_answered_by_whoever_is_left(self):
        out = self.route([self.HALFLIFE], "https://xdc.example/game.js",
                         referrer="https://xdc.example/?__xdc=tok-q3")
        self.assertEqual(out["asked"], [])
        self.assertEqual(out["status"], 502)

    def test_the_app_still_cannot_reach_the_network(self):
        """The property the whole feature rests on, asserted beside the routing that now precedes
        it: an off-origin request is refused here as well as by the CSP."""
        out = self.route([self.HALFLIFE], "https://example.com/tracker.gif")
        self.assertEqual(out["asked"], [])
        self.assertEqual(out["status"], 403)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ServingDoesNotConsumeTheArchive(unittest.TestCase):
    """Serving a file must not damage the archive it came out of.

    The sandbox is fed one entry at a time and the reply TRANSFERS the entry's ArrayBuffer, because
    an app can be 178 MB and a copy per hop is the difference between a game that starts and a black
    screen. A transfer detaches the buffer *in this realm* — all of it, not the slice being sent — so
    if any entry's bytes were a view onto the ARCHIVE, serving one file would empty the archive and
    every file after it would be zero bytes. The app boots into nothing and says nothing, on every
    launch, and the only thing that clears it is wiping the browser's storage.

    Both compression methods are exercised because they take different code paths, and the largest
    entry is served FIRST so a shared buffer would take everything else down with it.
    """

    def _serve(self, archive: bytes, names, method_note=""):
        return _read(archive, """
            const idx = new Map();
            for(const e of PCZip.entries(BYTES)) if(e.name) idx.set(e.name, e);
            const order = %s;
            const served = {}, sha = {};
            for(const n of order){
                const b = await PCZip.read(BYTES, idx.get(n));
                // …exactly what the reply site does with it.
                const buf = (b.byteOffset === 0 && b.byteLength === b.buffer.byteLength)
                          ? b.buffer : b.slice().buffer;
                served[n] = b.length;
                sha[n] = [...new Uint8Array(buf)].reduce((a,c)=>(a*31+c)%%1000003, 7);
                structuredClone(buf, { transfer: [buf] });     // the transfer, for real
            }
            // …and now the archive has to still be an archive.
            const after = [...PCZip.entries(BYTES)].map(e => e.name).sort();
            const reread = {};
            for(const n of order) reread[n] = (await PCZip.read(BYTES, idx.get(n))).length;
            console.log(JSON.stringify({ served, sha, after, reread }));
        """ % json.dumps(list(names)))

    def test_a_transferred_entry_does_not_empty_a_deflated_archive(self):
        files = {"index.html": "<b>hi</b>", "big.bin": "Q" * 200000, "small.js": "run()"}
        out = self._serve(_xdc(files), ["big.bin", "index.html", "small.js"])
        self.assertEqual(out["after"], ["big.bin", "index.html", "small.js"])
        self.assertEqual(out["reread"], {"big.bin": 200000, "index.html": 9, "small.js": 5},
                         "serving one file emptied the archive the others come from")

    def test_a_transferred_entry_does_not_empty_a_STORED_archive(self):
        """The dangerous half: a stored entry is bytes already sitting inside the archive, so it is
        the one a reader is most likely to hand out as a subarray. Half-Life's three campaign
        archives (29-79 MB) are all stored."""
        files = {"index.html": "<b>hi</b>", "hl/big.zip": "Z" * 4000, "a.png": "\x00\x01\x02"}
        out = self._serve(_xdc(files, compression=zipfile.ZIP_STORED),
                          ["hl/big.zip", "index.html", "a.png"])
        self.assertEqual(out["after"], ["a.png", "hl/big.zip", "index.html"])
        self.assertEqual(out["reread"], {"hl/big.zip": 4000, "index.html": 9, "a.png": 3},
                         "serving the stored entry detached the archive's own buffer")

    def test_every_entry_owns_its_bytes(self):
        """The invariant the reply site depends on, stated once: `read()` never returns a window
        onto the archive. Asserted rather than inferred, because it is two files away from the
        transfer that would punish it."""
        files = {"index.html": "x", "stored.bin": "S" * 5000, "deflated.bin": "D" * 5000}
        for comp in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            out = _read(_xdc(files, compression=comp), """
                const own = {};
                for(const e of PCZip.entries(BYTES)){
                    if(!e.name) continue;
                    const b = await PCZip.read(BYTES, e);
                    own[e.name] = (b.byteOffset === 0 && b.byteLength === b.buffer.byteLength);
                }
                console.log(JSON.stringify(own));
            """)
            self.assertTrue(all(out.values()), f"{comp}: an entry shares the archive's buffer: {out}")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ResettingAnApp(unittest.TestCase):
    """`?__reset=1` on the loader, which is the only way a reader can clear a mini app's state.

    An app keeps its saves on the sandbox origin (localStorage, IndexedDB — an emscripten game keeps
    its whole config there) and that origin exists precisely so the client cannot reach it. Before
    this there was no way out of an app that had saved itself into a state it could not start from
    except the browser's "clear browsing data", which also signs the reader out of their instance.

    Run under node against the shipped loader script with the storage APIs stubbed, because every
    step of it fails silently: a wipe that never runs, a flag that survives into the next load (a
    reset loop), or a `ready` posted into a document that is about to navigate.
    """

    def run_loader(self, search):
        src = _loader_js()
        return _node("""
            const calls = { caches: [], idb: [], unregistered: 0, local: 0, session: 0,
                            replaced: null, ready: [] };
            const search = %s;
            global.location = { search, href: 'https://xdc.example/__sandbox__/' + search,
                                hash: '', reload(){ calls.reloaded = true; },
                                replace(u){ calls.replaced = u; } };
            global.URL = URL; global.URLSearchParams = URLSearchParams;
            global.document = { getElementById: () => ({ set textContent(v){ calls.said = v; },
                                                         remove(){} }),
                                createElement: () => ({ setAttribute(){}, addEventListener(){} }),
                                body: { appendChild(){} } };
            global.window = { addEventListener(){} };
            global.parent = { postMessage(m){ calls.ready.push(m && m.method); } };
            global.localStorage = { clear(){ calls.local++; } };
            global.sessionStorage = { clear(){ calls.session++; } };
            global.caches = { keys: async () => ['app-v1', 'other'],
                              delete: async (k) => { calls.caches.push(k); return true; } };
            global.indexedDB = { databases: async () => [{ name: 'xash-fs' }, { name: 'saves' }],
                                 deleteDatabase(n){ calls.idb.push(n);
                                   const r = {}; setTimeout(() => r.onsuccess && r.onsuccess(), 0);
                                   return r; } };
            // `navigator` is a read-only built-in global in modern node — a plain assignment is
            // silently ignored, and the loader would see node's own, which has no serviceWorker.
            Object.defineProperty(globalThis, 'navigator', { configurable: true, value: {
                serviceWorker: {
                  addEventListener(){}, register: async () => ({}),
                  getRegistrations: async () => [{ unregister: async () => { calls.unregistered++; } }],
                } } });
            %s
            setTimeout(() => console.log(JSON.stringify(calls)), 60);
        """ % (json.dumps(search), src))

    def test_a_reset_wipes_the_origin_and_reboots_without_the_flag(self):
        out = self.run_loader("?__xdc=tok&__reset=1")
        self.assertEqual(sorted(out["caches"]), ["app-v1", "other"])
        self.assertEqual(sorted(out["idb"]), ["saves", "xash-fs"])
        self.assertEqual(out["local"], 1)
        self.assertEqual(out["session"], 1)
        self.assertEqual(out["unregistered"], 1)
        self.assertTrue(out["replaced"], "the reset never navigated to a clean boot")
        self.assertNotIn("__reset", out["replaced"],
                         "the flag survived the reset — the next load wipes again, for ever")
        self.assertIn("__xdc=tok", out["replaced"], "the reset lost the app's own token")
        self.assertEqual(out["ready"], [],
                         "it announced itself to the parent in a document that is navigating away")

    def test_an_ordinary_open_wipes_nothing(self):
        out = self.run_loader("?__xdc=tok")
        self.assertEqual(out["caches"], [])
        self.assertEqual(out["idb"], [])
        self.assertEqual(out["local"], 0)
        self.assertEqual(out["unregistered"], 0)
        self.assertIsNone(out["replaced"])
        self.assertEqual(out["ready"], ["ready"], "a normal open must still announce itself")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class EveryUpdateReachesTheAppExactlyOnce(unittest.TestCase):
    """THE APPEND-ONLY LOG, WHICH IS THE ENTIRE SPEC.

    An app's state IS the sequence of updates. Skip one and the game is not "a bit behind" — it is
    wrong, permanently, with the board on two screens disagreeing and nothing anywhere to say why.

    Delivery used to be driven by `before = ordered.length`, captured BEFORE `absorb()` — which
    pushes AND re-sorts by (created_at, id). Any update that sorts earlier than the newest one
    already held (a peer whose clock is a second behind, or the same second with a lower id, both
    completely ordinary) therefore landed at or before that index: the app was handed a DUPLICATE of
    an old update and never saw the new one. The sender's own echo, which the spec explicitly
    requires be delivered, is the most likely case of all.

    Run against the shipped Session with a relay stub, because this is a relationship between two
    functions and a sort — nothing static can see it.
    """

    def deliver(self, tail):
        return _node("""
        const out = { updates: [] };
        let live = null;
        const RELAY = {
          subscribe(filters, opts){ live = opts && opts.onEvent; return 'sub1'; },
          close(){},
          query: async () => STORED,
          publishFast: () => true,
        };
        const ev = (id, at, payload) => ({ id, kind:4932, created_at:at, pubkey:'p',
                                           tags:[['i','g1']], content: JSON.stringify(payload) });
        const STORED = [ev('aaa', 100, 'first'), ev('ccc', 300, 'third')];
        global.window = { addEventListener(){}, removeEventListener(){},
          Relay: RELAY, Store: { query: () => [] },
          __PC: { $: () => null, enc: s => s, toast(){},
                  publish: async () => ({ ok:true, ev: ev('bbb', 200, 'mine') }),
                  me: () => ({ pubkey:'me', npub:'npub1me' }), profOf: () => ({}),
                  apiBase: () => 'https://example.com' } };
        global.Relay = RELAY;
        global.document = { addEventListener(){}, querySelectorAll: () => [],
          createElement: () => ({ setAttribute(){}, classList:{add(){}}, appendChild(){}, style:{} }) };
        global.location = { hostname:'example.com', href:'https://example.com/' };
        global.crypto = require('crypto').webcrypto;
        require(%s);

        const s = new window.PCWebxdc.Session({ url:'https://x/a.xdc', uuid:'g1', name:'g' },
                                              { index:new Map(), bytes:new Uint8Array() });
        s.origin = 'https://xdc.example';
        s.frame = { contentWindow: { postMessage(m){
          if(m && m.method === 'webxdc.update')
            out.updates.push([m.params.payload, m.params.serial, m.params.max_serial]);
        } } };
        (async () => {
          await s.start(0);
          %s
          setTimeout(() => { console.log(JSON.stringify(out)); process.exit(0); }, 40);
        })().catch(e => { console.error(e); process.exit(1); });
        """ % (json.dumps(str(WEBXDC_JS)), tail))

    def _check(self, out):
        got = [u[0] for u in out["updates"]]
        self.assertEqual(len(got), len(set(got)),
                         f"an update was delivered twice: {out['updates']}")
        serials = [u[1] for u in out["updates"]]
        self.assertEqual(serials, sorted(set(serials)),
                         f"serials repeated or went backwards: {out['updates']}")
        return got

    def test_an_update_that_arrives_out_of_order_is_still_delivered(self):
        """`bbb` sorts BETWEEN the two already delivered. The old code delivered `third` a second
        time and `mine` never at all."""
        out = self.deliver(
            "live({ id:'bbb', kind:4932, created_at:200, pubkey:'q', tags:[['i','g1']],"
            " content: JSON.stringify('middle') });")
        got = self._check(out)
        self.assertEqual(got, ["first", "third", "middle"])

    def test_the_senders_own_move_is_delivered_even_when_it_sorts_first(self):
        """The spec: the sender receives its own updates. `publish` here answers with an event dated
        BEFORE what we already hold — a phone whose clock is a second behind its peer, which is the
        ordinary case, not a pathological one."""
        out = self.deliver("await s.sendUpdate({ payload:'mine' });")
        got = self._check(out)
        self.assertIn("mine", got, "the sender never received its own update")

    def test_a_tie_broken_by_id_downwards_is_not_lost(self):
        """Same second, lower id: the sort puts it first, which is exactly the position the old
        index had already passed."""
        out = self.deliver(
            "live({ id:'000', kind:4932, created_at:300, pubkey:'q', tags:[['i','g1']],"
            " content: JSON.stringify('tie') });")
        got = self._check(out)
        self.assertIn("tie", got)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class OpeningAnApp(unittest.TestCase):
    """`open()` end to end, with a real .xdc and a stubbed browser.

    Two bugs live here and both are invisible on screen:

      * the archive memo was keyed on `app.sha`, which the same file documents is often a LABEL and
        not a digest (the published Half-Life port carries the literal "hl"). Two apps whose authors
        both wrote one collide and the second one is served the FIRST one's archive — the wrong
        game, no download, nothing in any console.
      * pressing Play on an app that is already open mounted a SECOND Session over the first. The
        old one keeps its relay REQ and its `message` listener, so every packet is delivered twice
        and the app plays one move as two.
    """

    APP_A = None

    def drive(self, tail, apps=None):
        a = _xdc({"index.html": b"<b>A</b>", "manifest.toml": b'name = "Ay"'})
        b = _xdc({"index.html": b"<b>B</b>", "manifest.toml": b'name = "Bee"'})
        return _node("""
        const out = { fetched: [], toasts: [], sheets: 0, destroyed: [], mounted: [] };
        const BODIES = {
          'https://host/a.xdc': Uint8Array.from(%s),
          'https://host/b.xdc': Uint8Array.from(%s),
        };
        global.fetch = async (url) => {
          out.fetched.push(url);
          const b = BODIES[url];
          if(!b) throw new Error('no such app');
          return { ok:true, status:200, headers:{ get: () => String(b.length) },
                   arrayBuffer: async () => b.buffer.slice(b.byteOffset, b.byteOffset + b.length) };
        };
        global.caches = { open: async () => ({ match: async () => null, put: async () => {},
                                               delete: async () => true }) };
        const store = {};
        global.localStorage = { getItem: k => (k in store ? store[k] : null),
                                setItem: (k,v) => { store[k] = String(v); },
                                removeItem: k => { delete store[k]; } };
        function el(tag){
          const e = { tagName:tag, className:'', innerHTML:'', parentElement:null, onclick:null,
            src:'', style:{}, dataset:{},
            classList:{ add(){}, remove(){}, contains: () => false },
            setAttribute(){}, getAttribute: () => null, addEventListener(){},
            appendChild(c){ if(c) c.parentElement = e; return c; },
            remove(){ if(e.parentElement === document.body) out.sheets--; e.parentElement = null; },
            querySelector: () => el('div'), querySelectorAll: () => [] };
          return e;
        }
        const body = el('body');
        body.appendChild = (c) => { if(c){ if(c.parentElement !== body && c.className === 'xdc-sheet') out.sheets++;
                                           c.parentElement = body; } return c; };
        global.document = { body, createElement: el, addEventListener(){}, removeEventListener(){},
                            querySelector: () => null, querySelectorAll: () => [] };
        global.location = { hostname:'example.com', href:'https://example.com/' };
        global.crypto = require('crypto').webcrypto;
        const RELAY = { subscribe: () => 's1', close(){}, query: async () => [], publishFast: () => true };
        global.window = { addEventListener(){}, removeEventListener(){}, Relay: RELAY,
          Store: { query: () => [] }, localStorage: global.localStorage,
          __PC: { $: (sel, root) => el('div'), enc: s => s,
                  toast: (m) => { out.toasts.push(String(m)); },
                  publish: async () => ({ ok:true, ev:null }),
                  me: () => ({ pubkey:'me', npub:'npub1me' }), profOf: () => ({}),
                  apiBase: () => 'https://example.com' } };
        global.Relay = RELAY;
        global.window.PCZip = require(%s);
        require(%s);
        const X = window.PCWebxdc;
        // The frame is never really created here, so mount() is stubbed out — what is under test is
        // open()'s bookkeeping, not the sandbox handshake (covered elsewhere).
        X.Session.prototype.mount = async function(){ out.mounted.push(this.app.url); this.frame = el('iframe'); };
        const _d = X.Session.prototype.destroy;
        X.Session.prototype.destroy = function(){ out.destroyed.push(this.app.url); return _d.call(this); };
        (async () => {
          %s
          console.log(JSON.stringify(out));
          process.exit(0);
        })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """ % (json.dumps(list(a)), json.dumps(list(b)),
               json.dumps(str(ZIP_JS)), json.dumps(str(WEBXDC_JS)), tail))

    def test_two_apps_sharing_a_non_hash_x_tag_are_two_apps(self):
        """Both carry `x: "hl"` — a label, exactly like the real Half-Life port. Keyed on that, the
        second open is answered from the first one's archive and never downloads at all."""
        out = self.drive("""
          await X.open({ url:'https://host/a.xdc', sha:'hl', uuid:'ua' });
          await X.open({ url:'https://host/b.xdc', sha:'hl', uuid:'ub' });
        """)
        self.assertEqual(out["fetched"], ['https://host/a.xdc', 'https://host/b.xdc'],
                         "the second app was served the first one's archive")
        self.assertEqual(out["mounted"], ['https://host/a.xdc', 'https://host/b.xdc'])

    def test_the_same_app_is_still_downloaded_only_once(self):
        """The memo has to keep working — 178 MB re-downloaded per launch is the thing it exists for.
        Two DIFFERENT identifiers so the already-open check does not answer instead."""
        out = self.drive("""
          const s = await X.open({ url:'https://host/a.xdc', sha:'hl', uuid:'ua' });
          s.destroy();
          await X.open({ url:'https://host/a.xdc', sha:'hl', uuid:'ua2' });
        """)
        self.assertEqual(out["fetched"], ['https://host/a.xdc'])

    def test_pressing_play_on_an_open_app_does_not_mount_a_second_session(self):
        """Two Sessions on one app = two relay REQs and two message listeners, so every update is
        delivered twice and the app plays one move as two."""
        out = self.drive("""
          const a = await X.open({ url:'https://host/a.xdc', sha:'', uuid:'ua' });
          const b = await X.open({ url:'https://host/a.xdc', sha:'', uuid:'ua' });
          out.same = (a === b);
        """)
        self.assertTrue(out["same"], "a second Session was mounted over the first")
        self.assertEqual(out["mounted"], ['https://host/a.xdc'])
        self.assertEqual(out["sheets"], 1, "a second full-screen sheet was stacked on the first")

    def test_a_reset_does_tear_the_old_one_down(self):
        """Reset means "throw this away and start again" — the one case that must NOT be answered by
        focusing what is already there."""
        out = self.drive("""
          await X.open({ url:'https://host/a.xdc', sha:'', uuid:'ua' });
          await X.open({ url:'https://host/a.xdc', sha:'', uuid:'ua' }, { reset:true });
        """)
        self.assertEqual(out["destroyed"], ['https://host/a.xdc'])
        self.assertEqual(out["mounted"], ['https://host/a.xdc', 'https://host/a.xdc'])
        self.assertEqual(out["sheets"], 1, "the old sheet was left behind under the new one")

    def test_the_back_button_has_a_sheet_to_close(self):
        """A full-screen mini app is an overlay like Notes' drawer and Web Search's reader, and every
        one of those is in app.js's backButton chain. Without these two the Android Back button
        navigated the view UNDERNEATH and left the game standing on top of it."""
        out = self.drive("""
          out.beforeOpen = X.sheetOpen();
          await X.open({ url:'https://host/a.xdc', sha:'', uuid:'ua' });
          out.whileOpen = X.sheetOpen();
          out.closed = X.closeSheet();
          out.afterClose = X.sheetOpen();
        """)
        self.assertFalse(out["beforeOpen"])
        self.assertTrue(out["whileOpen"], "an open mini app is invisible to the back button")
        self.assertTrue(out["closed"])
        self.assertFalse(out["afterClose"])
        self.assertEqual(out["destroyed"], ['https://host/a.xdc'],
                         "closing the sheet must stop the session, not just hide it")
        self.assertEqual(out["sheets"], 0)


if __name__ == "__main__":
    unittest.main()
