/* #code — PosterChan Code: a small VS Code, inside the client.
 *
 * A file tree over the node's workspace, tabbed editing with syntax highlighting, a Format button
 * backed by black/beautysh on the server, and a REAL terminal in a panel underneath — so you edit a
 * script and run it without leaving the screen. Driven from app.js's renderView via
 * window.PCCode.render(), server side behind /api/code/*.
 *
 * ────────────────────────────────────────────────────────────────────────────────────────────────
 * WHY EVERY BYTE OF STATE IS IN `S` AND ALSO ON DISK, which is the whole design of this file.
 *
 * Three separate things repaint this screen, and each destroys the DOM:
 *
 *   1. `#feed` is shared by every view and app.js blanks it on entry. Glancing at Messages and
 *      coming back re-runs render() — the Web Search screen learned this first.
 *   2. On the windowed desktop the FOCUSED window's body carries `id="feed"`, and refocusing a
 *      window re-renders it from the module's own state. Clicking another window and back is a
 *      full repaint.
 *   3. A MONITOR HANDOFF RECREATES THE WINDOW IN A DIFFERENT ELECTRON RENDERER. os.js says so
 *      outright: "no DOM node can literally cross between" monitors, so the window is destroyed on
 *      one screen and rebuilt on the other. That is a different JavaScript context — module state
 *      does not survive it, and neither would a closure, a WeakMap, or anything else in memory.
 *
 * (1) and (2) are answered by keeping everything in `S` and painting from it. (3) is not: the new
 * renderer starts with an empty `S`. So `S` is also mirrored into localStorage, which IS shared
 * across renderers of the same origin (`app://posterchan`), and read back on first paint. That is
 * why the save is debounced to a fraction of a second AND flushed synchronously on pagehide,
 * visibilitychange and blur — a handoff gives no warning, and the last thing typed before a window
 * crossed screens is exactly the thing somebody would notice missing.
 *
 * The consequence for anyone editing this file: NOTHING may live only in the DOM. Not the caret,
 * not the scroll offset, not which tab is open, not the panel sizes. If you read it off an element,
 * write it into `S` in the same breath.
 *
 * ────────────────────────────────────────────────────────────────────────────────────────────────
 * THE EDITOR IS A TEXTAREA UNDER A HIGHLIGHTED <pre>, not a contenteditable.
 *
 * contenteditable owns the caret, and every browser has its own opinion about what Enter, paste and
 * IME composition do to the DOM inside one — which for CODE means invisible <div>s and <br>s in the
 * text you are about to save. A textarea's value is exactly the characters, on every platform and
 * in the APK's WebView; the colours are a <pre> painted behind it in the identical font, and the
 * textarea's own text is transparent with a visible caret. The two layers MUST agree on font,
 * size, line-height, padding, tab-size and white-space or the colours drift off the letters — which
 * is why those live in ONE CSS rule that both share (`.pcc-layer`).
 *
 * Highlighting is a pure function of (text, language) and is kept DOM-free at the top of this file
 * so tests/client/test_code_highlight.py can run it under node against real source.
 */
(function(){
  'use strict';

  // ══════════════════════════════════════════════════════════════════════════════════════════════
  // The highlighter — pure, DOM-free, tested under node.
  // ══════════════════════════════════════════════════════════════════════════════════════════════

  /* Rules are ordered and the FIRST match wins, so comments and strings must come before anything
   * that could match inside one. Getting that order wrong is not a crash: it is a keyword lit up
   * inside a string, which looks like a rendering quirk rather than a bug in the scanner. */
  const KW = {
    python: 'def class return if elif else for while in not and or is None True False import from as with try except finally raise yield lambda global nonlocal assert pass break continue async await del match case',
    bash: 'if then elif else fi for while until do done case esac in function return local export source declare readonly shift break continue exit trap set unset eval exec read echo printf',
    javascript: 'const let var function return if else for while class new await async try catch finally throw typeof instanceof of in do switch case break continue default delete void yield extends super this null true false undefined import export from static get set',
    java: 'public private protected class interface enum extends implements return if else for while do switch case break continue new this super static final void int long double float boolean char byte short String try catch finally throw throws import package abstract synchronized volatile transient native instanceof null true false',
    sql: 'select from where insert into values update set delete create table drop alter add index join left right inner outer on group by order having limit offset union all as distinct and or not null primary key foreign references default',
  };
  const kwRe = (lang) => '\\b(?:' + KW[lang].trim().split(/\s+/).join('|') + ')\\b';

  const NUM = '\\b(?:0[xXbBoO][0-9a-fA-F_]+|\\d[\\d_]*(?:\\.\\d[\\d_]*)?(?:[eE][+-]?\\d+)?)\\b';
  const FN = '\\b[A-Za-z_]\\w*(?=\\s*\\()';

  const RULES = {
    python: [
      ['com', '#[^\\n]*'],
      // Triple quotes FIRST — a docstring starts with what also opens a plain string, so the short
      // rule would match its first two quotes and end the token immediately.
      ['str', '[bBrRuUfF]{0,3}(?:"""[\\s\\S]*?"""|\'\'\'[\\s\\S]*?\'\'\'|"(?:\\\\[\\s\\S]|[^"\\\\\\n])*"|\'(?:\\\\[\\s\\S]|[^\'\\\\\\n])*\')'],
      ['dec', '@[A-Za-z_][\\w.]*'],
      ['kw', kwRe('python')],
      ['num', NUM],
      ['fn', FN],
      ['op', '[+\\-*/%=<>!&|^~]+'],
    ],
    bash: [
      ['com', '#[^\\n]*'],
      ['str', '"(?:\\\\[\\s\\S]|[^"\\\\])*"|\'[^\']*\''],
      ['var', '\\$\\{[^}]*\\}|\\$\\(\\(?|\\$[A-Za-z_]\\w*|\\$[@*#?$!0-9-]'],
      ['kw', kwRe('bash')],
      ['num', NUM],
      ['fn', '^[ \\t]*[A-Za-z_]\\w*(?=[ \\t]*\\(\\s*\\))'],
      ['op', '[|&;<>]+|[=!]=|[-+*/%]'],
    ],
    javascript: [
      ['com', '//[^\\n]*|/\\*[\\s\\S]*?\\*/'],
      ['str', '`(?:\\\\[\\s\\S]|[^`\\\\])*`|"(?:\\\\[\\s\\S]|[^"\\\\\\n])*"|\'(?:\\\\[\\s\\S]|[^\'\\\\\\n])*\''],
      ['kw', kwRe('javascript')],
      ['num', NUM],
      ['fn', FN],
      ['op', '=>|[+\\-*/%=<>!&|^~?:]+'],
    ],
    java: [
      ['com', '//[^\\n]*|/\\*[\\s\\S]*?\\*/'],
      ['str', '"(?:\\\\[\\s\\S]|[^"\\\\\\n])*"|\'(?:\\\\[\\s\\S]|[^\'\\\\\\n])*\''],
      ['ann', '@[A-Za-z_]\\w*'],
      ['kw', kwRe('java')],
      ['num', NUM],
      ['fn', FN],
      ['op', '[+\\-*/%=<>!&|^~?:]+'],
    ],
    json: [
      // A KEY IS A STRING FOLLOWED BY A COLON, and that lookahead is the only thing telling the two
      // apart — without it every value is painted as a key and the structure stops being readable.
      ['key', '"(?:\\\\[\\s\\S]|[^"\\\\])*"(?=\\s*:)'],
      ['str', '"(?:\\\\[\\s\\S]|[^"\\\\])*"'],
      ['kw', '\\b(?:true|false|null)\\b'],
      ['num', '-?' + NUM],
      ['op', '[{}\\[\\],:]'],
    ],
    css: [
      ['com', '/\\*[\\s\\S]*?\\*/'],
      ['str', '"(?:\\\\[\\s\\S]|[^"\\\\\\n])*"|\'(?:\\\\[\\s\\S]|[^\'\\\\\\n])*\''],
      ['var', '--[A-Za-z0-9_-]+'],
      ['key', '[-A-Za-z]+(?=\\s*:)'],
      ['num', '#[0-9a-fA-F]{3,8}\\b|\\b\\d[\\d.]*(?:px|em|rem|%|vh|vw|s|ms|deg|fr)?\\b'],
      ['dec', '@[A-Za-z-]+'],
      ['op', '[{};:,>+~]'],
    ],
    html: [
      ['com', '<!--[\\s\\S]*?-->'],
      ['str', '"(?:[^"]*)"|\'(?:[^\']*)\''],
      ['kw', '</?[A-Za-z][\\w:-]*|/?>'],
      ['key', '\\b[A-Za-z-]+(?=\\s*=)'],
    ],
    yaml: [
      ['com', '#[^\\n]*'],
      ['str', '"(?:\\\\[\\s\\S]|[^"\\\\\\n])*"|\'[^\'\\n]*\''],
      ['key', '^[ \\t]*-?[ \\t]*[A-Za-z_][\\w.-]*(?=\\s*:)'],
      ['kw', '\\b(?:true|false|null|yes|no|on|off)\\b'],
      ['num', NUM],
      ['op', '^[ \\t]*-(?=\\s)|[:|>]'],
    ],
    sql: [
      ['com', '--[^\\n]*|/\\*[\\s\\S]*?\\*/'],
      ['str', '\'(?:\'\'|[^\'])*\''],
      ['kw', kwRe('sql')],
      ['num', NUM],
      ['op', '[=<>!+\\-*/,;()]'],
    ],
    markdown: [
      ['com', '^\\s{0,3}>[^\\n]*'],
      ['str', '```[\\s\\S]*?```|`[^`\\n]*`'],
      ['kw', '^#{1,6}[ \\t][^\\n]*'],
      ['fn', '\\[[^\\]\\n]*\\]\\([^)\\n]*\\)'],
      ['dec', '\\*\\*[^*\\n]+\\*\\*|__[^_\\n]+__'],
      ['op', '^\\s{0,3}(?:[-*+]|\\d+\\.)(?=\\s)'],
    ],
  };
  /* SQL IS THE ONE CASE-INSENSITIVE LANGUAGE HERE, and `(?i:…)` — the obvious way to say so — is a
   * syntax error in JavaScript: inline flag groups are a PCRE/Python feature that V8 does not
   * implement, so the whole alternation fails to compile and SQL silently loses every colour (the
   * compile is inside a try, precisely so a bad rule cannot take the screen). Per-language flags
   * instead; `SELECT` and `select` are the same keyword and people write both. */
  const FLAGS = { sql: 'gmi' };

  RULES.ini = RULES.yaml;
  RULES.toml = RULES.yaml;
  RULES.xml = RULES.html;

  function esc(s){
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  /* How many CAPTURING groups a pattern contains.
   *
   * The scanner joins every rule into one alternation and finds which rule matched by looking for
   * the first defined group. That mapping is only correct if it accounts for groups INSIDE a rule —
   * and several rules here legitimately need them. Relying on "always write (?:…)" instead is a
   * convention, and a convention silently mis-colours the whole file the first time somebody
   * forgets. Counted, it cannot be got wrong: appending `|` makes the pattern match the empty
   * string, so the result's length is 1 + the number of groups. */
  function groupCount(src){
    try{ return new RegExp(src + '|').exec('').length - 1; }
    catch(_){ return 0; }
  }

  const _compiled = {};
  function compiled(lang){
    if(Object.prototype.hasOwnProperty.call(_compiled, lang)) return _compiled[lang];
    const rules = RULES[lang];
    if(!rules){ return (_compiled[lang] = null); }
    const slot = [];            // capture-group index → rule index
    let n = 0;
    rules.forEach((r, i) => { slot[n] = i; n += 1 + groupCount(r[1]); });
    let re;
    try{ re = new RegExp(rules.map(r => '(' + r[1] + ')').join('|'), FLAGS[lang] || 'gm'); }
    catch(_){ return (_compiled[lang] = null); }   // a bad rule costs colour, never the screen
    return (_compiled[lang] = { re, slot, cls: rules.map(r => r[0]) });
  }

  /** Source → HTML with <span class="t-…"> around each token. Escaped; never returns raw input. */
  function highlight(text, lang){
    const c = compiled(lang);
    if(!c) return esc(text);
    let out = '', last = 0, m;
    c.re.lastIndex = 0;
    while((m = c.re.exec(text)) !== null){
      // A rule that can match the empty string would spin here for ever with the tab frozen and
      // nothing in any log. Cheap to guard, impossible to diagnose from a screenshot.
      if(m[0] === ''){ c.re.lastIndex++; continue; }
      if(m.index > last) out += esc(text.slice(last, m.index));
      let ri = -1;
      for(let i = 1; i < m.length; i++){
        if(m[i] !== undefined){ ri = c.slot[i - 1]; break; }
      }
      out += ri >= 0 ? '<span class="t-' + c.cls[ri] + '">' + esc(m[0]) + '</span>' : esc(m[0]);
      last = m.index + m[0].length;
    }
    return out + esc(text.slice(last));
  }

  const EXT = {
    py:'python', pyw:'python', sh:'bash', bash:'bash', zsh:'bash',
    js:'javascript', mjs:'javascript', cjs:'javascript', json:'json',
    html:'html', htm:'html', xml:'xml', css:'css', md:'markdown', markdown:'markdown',
    java:'java', yml:'yaml', yaml:'yaml', sql:'sql', toml:'toml', ini:'ini', cfg:'ini',
  };
  const langOf = (name) => EXT[String(name).split('.').pop().toLowerCase()] || 'text';

  /* Above this, colour is dropped and the buffer is shown as plain text.
   *
   * The scan is one pass over the whole file on every repaint, and a repaint follows a keystroke.
   * At a few hundred kilobytes that is visible as lag on every character typed — a slow editor is a
   * broken editor, and plain black text that keeps up is strictly better than colour that does not.
   * Said out loud in the status bar rather than left as a mystery. */
  const HL_MAX = 120 * 1024;

  window.PCCodeHL = { highlight, langOf, esc, RULES, HL_MAX };

  // ══════════════════════════════════════════════════════════════════════════════════════════════
  // State — see the header. Everything here is painted from, and mirrored to localStorage.
  // ══════════════════════════════════════════════════════════════════════════════════════════════

  const S = {
    ready: false,
    root: '', engines: {}, gate: '',        // gate: why the screen is unavailable, if it is
    cwd: '', tree: [], treeErr: '', treeBusy: false,
    expanded: {},                            // dir path → true (the tree remembers what you opened)
    open: [],                                // [{path, lang, text, disk, mtime, sel, scroll}]
    active: -1,
    termOpen: false, termH: 260, sideW: 250,
    status: '', statusKind: '',
    busy: false,
  };

  const doc = () => (S.active >= 0 && S.open[S.active]) || null;
  const dirty = (d) => !!d && d.text !== d.disk;

  /* PERSIST BUDGET. localStorage is a few megabytes for the whole origin and this app already keeps
   * an event cache, a drive index and a terminal history in it. An editor holding six large files
   * could quietly evict all of that, so what gets written is bounded and PRIORITISED: unsaved work
   * first, because a clean buffer is still on disk and reloads on the next open. */
  const PERSIST_MAX = 1024 * 1024;
  const LSKEY = () => {
    const PC = window.__PC;
    const pk = PC && PC.ME && PC.ME.pubkey ? String(PC.ME.pubkey).slice(0, 12) : 'anon';
    return 'pccode_' + pk;
  };

  let _saveT = null;
  function persist(){
    /* Written whole, never merged. Two renderers of the same origin can both hold this key during a
     * monitor handoff, and a read-modify-write between them would interleave two editors' tabs. The
     * window being LOOKED AT is the one that writes, and the last write wins — which is what the
     * person means by "this is where I was". */
    try{
      const slim = { cwd: S.cwd, active: S.active, termOpen: S.termOpen, termH: S.termH,
                     sideW: S.sideW, expanded: S.expanded, open: [] };
      let budget = PERSIST_MAX;
      // Unsaved buffers first — see PERSIST_MAX.
      const order = S.open.map((d, i) => [i, d]).sort((a, b) => (dirty(b[1]) ? 1 : 0) - (dirty(a[1]) ? 1 : 0));
      const keep = {};
      for(const [i, d] of order){
        const cost = (d.text || '').length;
        keep[i] = budget >= cost;
        if(keep[i]) budget -= cost;
      }
      slim.open = S.open.map((d, i) => ({
        path: d.path, lang: d.lang, mtime: d.mtime, sel: d.sel, scroll: d.scroll,
        // `text:null` means "reload me from disk". Recorded explicitly so restore() can tell it
        // from an empty file, which is a real thing somebody may be editing.
        text: keep[i] ? d.text : null,
        disk: keep[i] ? d.disk : null,
      }));
      localStorage.setItem(LSKEY(), JSON.stringify(slim));
    }catch(_){ /* quota, private mode, a disabled store — the editor still works, it just forgets */ }
  }
  function save(now){
    if(_saveT){ clearTimeout(_saveT); _saveT = null; }
    if(now) return persist();
    _saveT = setTimeout(() => { _saveT = null; persist(); }, 300);
  }
  function restore(){
    try{
      const raw = localStorage.getItem(LSKEY());
      if(!raw) return;
      const v = JSON.parse(raw);
      if(!v || typeof v !== 'object') return;
      S.cwd = typeof v.cwd === 'string' ? v.cwd : '';
      S.termOpen = !!v.termOpen;
      S.termH = Math.max(120, Math.min(900, Number(v.termH) || 260));
      S.sideW = Math.max(150, Math.min(600, Number(v.sideW) || 250));
      S.expanded = (v.expanded && typeof v.expanded === 'object') ? v.expanded : {};
      S.open = Array.isArray(v.open) ? v.open.filter(d => d && typeof d.path === 'string').map(d => ({
        path: d.path, lang: d.lang || langOf(d.path), text: typeof d.text === 'string' ? d.text : null,
        disk: typeof d.disk === 'string' ? d.disk : null, mtime: Number(d.mtime) || 0,
        sel: d.sel && typeof d.sel === 'object' ? d.sel : { s: 0, e: 0 }, scroll: Number(d.scroll) || 0,
      })) : [];
      S.active = (Number.isInteger(v.active) && v.active >= 0 && v.active < S.open.length) ? v.active : (S.open.length ? 0 : -1);
    }catch(_){ /* a corrupt blob is not worth refusing to open the editor over */ }
  }

  function init(){
    const PC = window.__PC;
    if(!PC){ return setTimeout(init, 50); }
    const { $, enc, toast, authFetch, ensureAiSession } = PC;
    const inView = () => window.__PC.VIEW === 'code';

    // ---- server ------------------------------------------------------------------------------

    async function api(path, opts){
      try{ await ensureAiSession(); }catch(_){}
      const r = await authFetch('/api/code' + path, opts);
      let body = null;
      try{ body = await r.json(); }catch(_){}
      if(!r.ok){
        const e = new Error((body && (body.detail || body.error)) || ('HTTP ' + r.status));
        e.status = r.status;
        throw e;
      }
      return body || {};
    }
    const post = (path, obj) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                            body: JSON.stringify(obj) });

    function status(msg, kind){
      S.status = msg || '';
      S.statusKind = kind || '';
      const el = $('#pcc-status');
      if(el){ el.textContent = S.status; el.className = 'pcc-status ' + (S.statusKind || ''); }
      else if(inView()) paint();
    }

    // ---- files -------------------------------------------------------------------------------

    async function loadConfig(){
      try{
        const c = await api('/config');
        S.root = c.root || '';
        S.engines = c.engines || {};
        S.gate = '';
      }catch(e){
        // 403 IS THE EXPECTED ANSWER FOR MOST PEOPLE, not an error. This screen edits files on the
        // node, so it shares the terminal's gate; saying so plainly beats an empty tree.
        S.gate = e.status === 403
          ? (e.message || 'PosterChan Code is limited to administrators.')
          : ('Could not reach this node: ' + e.message);
      }
      S.ready = true;
    }

    async function loadTree(path){
      S.treeBusy = true; S.treeErr = '';
      try{
        const t = await api('/tree?path=' + encodeURIComponent(path || ''));
        S.cwd = t.path || '';
        S.tree = t.entries || [];
        if(t.truncated) status('This folder has more files than the tree will show', 'warn');
      }catch(e){
        // A FAILED LISTING KEEPS THE LAST GOOD ONE. Blanking the tree on a transient error makes an
        // unreachable node look like an empty workspace — the same rule the file screens follow.
        S.treeErr = e.message || 'Could not read that folder';
      }
      S.treeBusy = false;
      save();
      if(inView()) paint();
    }

    async function openPath(path){
      const at = S.open.findIndex(d => d.path === path);
      if(at >= 0){ S.active = at; save(); paint(); return; }
      status('Opening ' + path + '…');
      try{
        const f = await api('/file?path=' + encodeURIComponent(path));
        S.open.push({ path: f.path, lang: f.lang || langOf(f.path), text: f.text, disk: f.text,
                      mtime: f.mtime || 0, sel: { s: 0, e: 0 }, scroll: 0 });
        S.active = S.open.length - 1;
        status('');
      }catch(e){ status(e.message || 'Could not open that file', 'err'); }
      save(); paint();
    }

    /* A buffer restored from localStorage with `text:null` was too big to persist — it is clean, so
     * disk is the truth. Fetched lazily when it is first LOOKED at rather than all at once on
     * restore: reopening a window with nine tabs should cost one request, not nine. */
    async function hydrate(d){
      if(!d || d.text !== null) return;
      try{
        const f = await api('/file?path=' + encodeURIComponent(d.path));
        d.text = f.text; d.disk = f.text; d.mtime = f.mtime || 0;
        d.lang = f.lang || d.lang;
      }catch(e){
        d.text = ''; d.disk = '';
        status('Could not re-open ' + d.path + ': ' + (e.message || ''), 'err');
      }
      if(inView()) paint();
    }

    async function saveDoc(){
      const d = doc();
      if(!d || d.text === null) return;
      if(!dirty(d)){ status('No changes to save'); return; }
      S.busy = true; status('Saving…');
      try{
        const r = await post('/file', { path: d.path, text: d.text, mtime: d.mtime });
        d.disk = d.text; d.mtime = r.mtime || 0;
        status('Saved ' + d.path, 'ok');
      }catch(e){
        // 409 IS THE INTERESTING ONE: the terminal underneath this editor is the likeliest thing to
        // have changed the file. Named as what it is, with the two ways out, rather than "failed".
        status(e.status === 409
          ? (d.path + ' changed on disk since you opened it — reload it, or use Save as… to keep yours')
          : ('Could not save: ' + (e.message || '')), 'err');
      }
      S.busy = false;
      save(); paint();
    }

    async function reloadDoc(){
      const d = doc();
      if(!d) return;
      try{
        const f = await api('/file?path=' + encodeURIComponent(d.path));
        d.text = f.text; d.disk = f.text; d.mtime = f.mtime || 0;
        status('Reloaded ' + d.path, 'ok');
      }catch(e){ status(e.message || 'Could not reload', 'err'); }
      save(); paint();
    }

    async function formatDoc(){
      const d = doc();
      if(!d || d.text === null) return;
      const engine = S.engines[d.lang] || (d.lang === 'json' ? 'json' : '');
      if(!engine){ status('No formatter on this node for ' + d.lang, 'warn'); return; }
      S.busy = true; status('Formatting with ' + engine + '…');
      try{
        const r = await post('/format', { language: d.lang, source: d.text, indent: 4 });
        if(!r.ok){ status(engine + ' left it alone: ' + (r.error || ''), 'warn'); }
        else if(!r.changed){ status('Already tidy'); }
        else {
          /* THE CARET IS KEPT AS A FRACTION OF THE DOCUMENT, not as an offset. Formatting moves
           * every character after the first change, so a restored absolute offset lands somewhere
           * arbitrary — usually mid-token, several lines from where the person was looking. */
          const frac = d.text.length ? Math.min(1, (d.sel.s || 0) / d.text.length) : 0;
          d.text = r.source;
          const at = Math.round(frac * d.text.length);
          d.sel = { s: at, e: at };
          status('Formatted with ' + engine, 'ok');
        }
      }catch(e){ status('Could not format: ' + (e.message || ''), 'err'); }
      S.busy = false;
      save(); paint();
    }

    function closeTab(i){
      const d = S.open[i];
      if(!d) return;
      if(dirty(d) && !confirmDiscard(d)) return;
      S.open.splice(i, 1);
      if(S.active >= S.open.length) S.active = S.open.length - 1;
      else if(S.active > i) S.active--;
      save(); paint();
    }
    /* NOT `window.confirm`. A native dialog wedges the Electron shell (it blocks the renderer that
     * owns the window chrome), and in the APK's WebView it can be suppressed entirely — in which
     * case it returns false and the tab silently refuses to close. Two clicks on the × instead:
     * the first arms it and says so, the second does it. */
    const _armed = {};
    function confirmDiscard(d){
      if(_armed[d.path]){ delete _armed[d.path]; return true; }
      _armed[d.path] = true;
      setTimeout(() => { delete _armed[d.path]; }, 4000);
      status('Unsaved changes in ' + d.path + ' — click × again to discard', 'warn');
      return false;
    }

    // ---- painting ----------------------------------------------------------------------------

    const icon = (e) => e.dir ? '📁' : ({ python:'🐍', bash:'📜', javascript:'📒', json:'🧾',
      html:'🌐', css:'🎨', markdown:'📝', java:'☕', yaml:'⚙️', sql:'🗃️' }[e.lang] || '📄');

    function crumbs(){
      const parts = S.cwd ? S.cwd.split('/') : [];
      let acc = '';
      const out = ['<button class="pcc-crumb" data-go="">workspace</button>'];
      for(const p of parts){
        acc = acc ? acc + '/' + p : p;
        out.push('<span class="pcc-sep">/</span><button class="pcc-crumb" data-go="' + enc(acc) + '">' + enc(p) + '</button>');
      }
      return out.join('');
    }

    function treeHtml(){
      if(S.treeErr) return '<div class="pcc-note err">' + enc(S.treeErr) + '</div>';
      if(!S.tree.length) return '<div class="pcc-note">This folder is empty</div>';
      return S.tree.map(e => {
        const path = S.cwd ? S.cwd + '/' + e.name : e.name;
        const isOpen = S.open.some(d => d.path === path);
        return '<button class="pcc-item' + (isOpen ? ' on' : '') + '" data-' + (e.dir ? 'dir' : 'file') +
               '="' + enc(path) + '" title="' + enc(path) + '">' +
               '<span class="pcc-ic">' + icon(e) + '</span><span class="pcc-nm">' + enc(e.name) + '</span></button>';
      }).join('');
    }

    function tabsHtml(){
      if(!S.open.length) return '';
      return S.open.map((d, i) => {
        const name = d.path.split('/').pop();
        return '<div class="pcc-tab' + (i === S.active ? ' on' : '') + (dirty(d) ? ' dirty' : '') + '" data-tab="' + i + '">' +
               '<span class="pcc-tabname" title="' + enc(d.path) + '">' + enc(name) + '</span>' +
               '<button class="pcc-x" data-close="' + i + '" title="Close" aria-label="Close ' + enc(name) + '">×</button></div>';
      }).join('');
    }

    function editorHtml(){
      const d = doc();
      if(!d) return '<div class="pcc-blank"><b>PosterChan Code</b><span>Pick a file on the left to start editing.</span></div>';
      if(d.text === null) return '<div class="pcc-blank"><div class="spinner"></div></div>';
      const big = d.text.length > HL_MAX;
      const lines = d.text.split('\n').length;
      let nums = '';
      for(let i = 1; i <= lines; i++) nums += i + '\n';
      // The highlight layer ends with a newline so a trailing empty line still gets a row, and the
      // <pre> keeps its final line height instead of collapsing under the caret.
      const body = big ? esc(d.text) : highlight(d.text, d.lang);
      return '<div class="pcc-editwrap">' +
             '<pre class="pcc-gutter" id="pcc-gutter" aria-hidden="true">' + nums + '</pre>' +
             '<div class="pcc-edit">' +
               '<pre class="pcc-layer pcc-hl" id="pcc-hl" aria-hidden="true">' + body + '\n</pre>' +
               '<textarea class="pcc-layer pcc-ta" id="pcc-ta" spellcheck="false" autocapitalize="off" ' +
                 'autocorrect="off" autocomplete="off" wrap="off" aria-label="' + enc(d.path) + '">' +
                 esc(d.text) + '</textarea>' +
             '</div></div>';
    }

    function toolbarHtml(){
      const d = doc();
      const eng = d ? (S.engines[d.lang] || (d.lang === 'json' ? 'json' : '')) : '';
      return '<div class="pcc-bar">' +
        '<button class="btn pcc-b" id="pcc-save"' + (d && dirty(d) ? '' : ' disabled') + '>Save</button>' +
        '<button class="btn btn-ghost pcc-b" id="pcc-fmt"' + (d && eng ? '' : ' disabled') + ' title="' +
          (eng ? 'Beautify with ' + enc(eng) : 'No formatter on this node for this language') + '">Format</button>' +
        '<button class="btn btn-ghost pcc-b" id="pcc-reload"' + (d ? '' : ' disabled') + '>Reload</button>' +
        '<span class="pcc-grow"></span>' +
        '<span class="pcc-lang">' + enc(d ? d.lang : '') + (eng ? ' · ' + enc(eng) : '') + '</span>' +
        '<button class="btn btn-ghost pcc-b" id="pcc-term">' + (S.termOpen ? 'Hide' : 'Show') + ' terminal</button>' +
        '</div>';
    }

    function paint(){
      const feed = $('#feed');
      if(!feed || !inView()) return;
      /* classList.add, NEVER `className =` — assigning drops the base `.feed` class that supplies
       * flex:1/overflow-y:auto, and nothing puts it back, so the TIMELINE stops scrolling for the
       * rest of the session after one visit here. term.js was bitten by exactly this. */
      feed.classList.add('feed-code');

      if(!S.ready){ feed.innerHTML = '<div class="spinner"></div>'; return; }
      if(S.gate){
        feed.innerHTML = '<div class="empty">' + enc(S.gate) + '</div>';
        return;
      }

      const d = doc();
      feed.innerHTML =
        '<div class="pcc" style="--pcc-side:' + S.sideW + 'px;--pcc-term:' + S.termH + 'px">' +
          '<div class="pcc-main">' +
            '<aside class="pcc-side" id="pcc-side">' +
              '<div class="pcc-crumbs">' + crumbs() + '</div>' +
              '<div class="pcc-tree" id="pcc-tree">' + treeHtml() + '</div>' +
              '<div class="pcc-root" title="' + enc(S.root) + '">' + enc(S.root) + '</div>' +
            '</aside>' +
            '<div class="pcc-grip pcc-grip-v" id="pcc-gripv" role="separator" aria-label="Resize file tree"></div>' +
            '<section class="pcc-pane">' +
              '<div class="pcc-tabs" id="pcc-tabs">' + tabsHtml() + '</div>' +
              toolbarHtml() +
              editorHtml() +
              '<div class="pcc-foot"><span class="pcc-status ' + enc(S.statusKind) + '" id="pcc-status">' +
                enc(S.status) + '</span><span class="pcc-grow"></span><span class="pcc-pos" id="pcc-pos"></span></div>' +
            '</section>' +
          '</div>' +
          (S.termOpen
            ? '<div class="pcc-grip pcc-grip-h" id="pcc-griph" role="separator" aria-label="Resize terminal"></div>' +
              '<div class="pcc-term feed-term" id="pcc-termhost"></div>'
            : '') +
        '</div>';

      wire();
      if(d && d.text === null) hydrate(d);
      if(S.termOpen) mountTerm();
    }

    // ---- wiring ------------------------------------------------------------------------------

    /* THE CARET AND SCROLL ARE READ OFF THE ELEMENT AND WRITTEN INTO `S` IN THE SAME BREATH.
     *
     * This is the rule the header states, in the one place it is easiest to break. Everything that
     * moves the caret — typing, clicking, arrow keys, selecting — funnels through here, so a repaint
     * (a refocus, a monitor handoff, a Format) can put it back exactly where it was. */
    function capture(){
      const ta = $('#pcc-ta'), d = doc();
      if(!ta || !d) return;
      d.sel = { s: ta.selectionStart, e: ta.selectionEnd };
      d.scroll = ta.scrollTop;
      save();
    }

    function syncScroll(){
      const ta = $('#pcc-ta'), hl = $('#pcc-hl'), g = $('#pcc-gutter');
      if(!ta) return;
      if(hl){ hl.scrollTop = ta.scrollTop; hl.scrollLeft = ta.scrollLeft; }
      if(g) g.scrollTop = ta.scrollTop;
    }

    function showPos(){
      const ta = $('#pcc-ta'), el = $('#pcc-pos');
      if(!ta || !el) return;
      const upto = ta.value.slice(0, ta.selectionStart);
      const line = upto.split('\n').length;
      const col = upto.length - upto.lastIndexOf('\n');
      el.textContent = 'Ln ' + line + ', Col ' + col;
    }

    /* Repaint only the coloured layer and the gutter — never the textarea.
     *
     * Rewriting the textarea's value on every keystroke destroys the caret, the undo stack and any
     * IME composition in progress: typing accented or CJK text would become impossible. The
     * textarea is the source of truth while the person types; the <pre> behind it is what gets
     * redrawn. */
    let _hlT = null;
    function repaintHl(){
      const d = doc();
      if(!d) return;
      const hl = $('#pcc-hl'), g = $('#pcc-gutter'), ta = $('#pcc-ta');
      if(!hl || !ta) return;
      const big = d.text.length > HL_MAX;
      hl.innerHTML = (big ? esc(d.text) : highlight(d.text, d.lang)) + '\n';
      if(g){
        const lines = d.text.split('\n').length;
        let nums = '';
        for(let i = 1; i <= lines; i++) nums += i + '\n';
        if(g.textContent !== nums) g.textContent = nums;
      }
      syncScroll();
    }
    function scheduleHl(){
      if(_hlT) return;
      // One frame's delay coalesces a burst of keystrokes (and a held-down key) into a single scan,
      // which is what keeps a large file typable.
      _hlT = requestAnimationFrame(() => { _hlT = null; repaintHl(); });
    }

    function onInput(){
      const ta = $('#pcc-ta'), d = doc();
      if(!ta || !d) return;
      d.text = ta.value;
      capture();
      scheduleHl();
      const b = $('#pcc-save');
      if(b) b.disabled = !dirty(d);
      const tab = document.querySelector('.pcc-tab[data-tab="' + S.active + '"]');
      if(tab) tab.classList.toggle('dirty', dirty(d));
      showPos();
    }

    /* Tab inserts INDENTATION, and that is not the browser default.
     *
     * In a textarea Tab moves focus to the next control, which in a code editor means the caret
     * leaves the file every time somebody indents a line. Shift+Tab outdents; with a selection both
     * act on whole lines, because that is what people mean by indenting a block. */
    const INDENT = '    ';
    function onKey(ev){
      const ta = $('#pcc-ta'), d = doc();
      if(!ta || !d) return;
      const s = ta.selectionStart, e = ta.selectionEnd;

      if(ev.key === 'Tab'){
        ev.preventDefault();
        const multi = s !== e && ta.value.slice(s, e).indexOf('\n') >= 0;
        if(!multi && !ev.shiftKey){
          setValue(ta.value.slice(0, s) + INDENT + ta.value.slice(e), s + INDENT.length);
          return;
        }
        const from = ta.value.lastIndexOf('\n', s - 1) + 1;
        const to = e + (ta.value.slice(e).indexOf('\n') < 0 ? ta.value.length - e : ta.value.slice(e).indexOf('\n'));
        const block = ta.value.slice(from, to);
        const next = ev.shiftKey
          ? block.replace(/^(?: {1,4}|\t)/gm, '')
          : block.replace(/^(?!$)/gm, INDENT);
        setValue(ta.value.slice(0, from) + next + ta.value.slice(to), from, from + next.length);
        return;
      }

      if(ev.key === 'Enter'){
        // AUTO-INDENT. Carrying the previous line's leading whitespace is the difference between an
        // editor and a textarea; a colon or an opening brace adds one level, the way every editor does.
        const lineStart = ta.value.lastIndexOf('\n', s - 1) + 1;
        const line = ta.value.slice(lineStart, s);
        const lead = (line.match(/^[ \t]*/) || [''])[0];
        const deeper = /[:{[(]\s*$/.test(line) ? INDENT : '';
        if(!lead && !deeper) return;              // nothing to add — let the browser do it
        ev.preventDefault();
        const ins = '\n' + lead + deeper;
        setValue(ta.value.slice(0, s) + ins + ta.value.slice(e), s + ins.length);
        return;
      }

      if((ev.ctrlKey || ev.metaKey) && (ev.key === 's' || ev.key === 'S')){
        ev.preventDefault(); saveDoc(); return;
      }
      if((ev.ctrlKey || ev.metaKey) && ev.shiftKey && (ev.key === 'f' || ev.key === 'F')){
        ev.preventDefault(); formatDoc(); return;
      }
    }

    /* Write through the textarea with `execCommand('insertText')` when it is available, so the
     * browser's own UNDO STACK records the change. Setting `.value` wipes undo — Ctrl+Z after an
     * auto-indent would jump past everything typed before it, or do nothing at all. */
    function setValue(next, selStart, selEnd){
      const ta = $('#pcc-ta'), d = doc();
      if(!ta || !d) return;
      const from = ta.selectionStart, to = ta.selectionEnd;
      let ok = false;
      // Only the common case (a pure insertion at the caret) maps onto insertText; block indent
      // replaces a range, so select it first and let insertText overwrite.
      try{
        const head = commonHead(ta.value, next);
        const tailLen = commonTail(ta.value, next, head);
        ta.setSelectionRange(head, ta.value.length - tailLen);
        ok = document.execCommand && document.execCommand('insertText', false,
              next.slice(head, next.length - tailLen));
      }catch(_){ ok = false; }
      if(!ok){ ta.value = next; }
      ta.setSelectionRange(selStart, selEnd === undefined ? selStart : selEnd);
      void from; void to;
      onInput();
    }
    function commonHead(a, b){
      const n = Math.min(a.length, b.length);
      let i = 0;
      while(i < n && a.charCodeAt(i) === b.charCodeAt(i)) i++;
      return i;
    }
    function commonTail(a, b, head){
      const n = Math.min(a.length, b.length) - head;
      let i = 0;
      while(i < n && a.charCodeAt(a.length - 1 - i) === b.charCodeAt(b.length - 1 - i)) i++;
      return i;
    }

    function wire(){
      const on = (sel, ev, fn) => { const el = $(sel); if(el) el.addEventListener(ev, fn); };

      // Tree + breadcrumbs: ONE delegated listener, so a repaint cannot leave a dead button behind.
      const side = $('#pcc-side');
      if(side) side.addEventListener('click', (ev) => {
        const b = ev.target.closest && ev.target.closest('[data-dir],[data-file],[data-go]');
        if(!b) return;
        if(b.hasAttribute('data-go')) return loadTree(b.getAttribute('data-go'));
        if(b.hasAttribute('data-dir')) return loadTree(b.getAttribute('data-dir'));
        openPath(b.getAttribute('data-file'));
      });

      const tabs = $('#pcc-tabs');
      if(tabs) tabs.addEventListener('click', (ev) => {
        const x = ev.target.closest && ev.target.closest('[data-close]');
        if(x){ ev.stopPropagation(); return closeTab(Number(x.getAttribute('data-close'))); }
        const t = ev.target.closest && ev.target.closest('[data-tab]');
        if(t){ S.active = Number(t.getAttribute('data-tab')); save(); paint(); }
      });

      on('#pcc-save', 'click', saveDoc);
      on('#pcc-fmt', 'click', formatDoc);
      on('#pcc-reload', 'click', reloadDoc);
      on('#pcc-term', 'click', () => { S.termOpen = !S.termOpen; save(); paint(); });

      const ta = $('#pcc-ta');
      if(ta){
        ta.addEventListener('input', onInput);
        ta.addEventListener('keydown', onKey);
        ta.addEventListener('scroll', () => { syncScroll(); capture(); });
        // `select` and `click` as well as `keyup`: a mouse drag moves the caret without a key ever
        // being pressed, and that position is as much "where I was" as a typed one.
        ['keyup', 'click', 'select'].forEach(e => ta.addEventListener(e, () => { capture(); showPos(); }));
        restoreCaret();
      }

      grips();
    }

    /* PUT THE CARET AND THE SCROLL BACK. This is the visible half of the whole state design: after
     * a refocus, a resize that re-rendered, a Format, or a window rebuilt on another monitor, the
     * file must still be scrolled where it was with the caret between the same two characters. */
    function restoreCaret(){
      const ta = $('#pcc-ta'), d = doc();
      if(!ta || !d) return;
      const len = ta.value.length;
      const s = Math.max(0, Math.min(len, (d.sel && d.sel.s) || 0));
      const e = Math.max(s, Math.min(len, (d.sel && d.sel.e) || s));
      try{ ta.setSelectionRange(s, e); }catch(_){}
      ta.scrollTop = d.scroll || 0;
      syncScroll();
      showPos();
    }

    /* The two drag handles. Sizes go into `S` (and therefore to disk) on every pointer move, so a
     * layout somebody arranged survives the same three repaints everything else here does. */
    function grips(){
      const drag = (id, fn) => {
        const g = $(id);
        if(!g) return;
        g.addEventListener('pointerdown', (ev) => {
          ev.preventDefault();
          // Pointer CAPTURE, so a fast drag that leaves the handle keeps resizing instead of
          // stopping wherever the pointer escaped.
          try{ g.setPointerCapture(ev.pointerId); }catch(_){}
          const move = (m) => { fn(m); const root = $('.pcc');
            if(root){ root.style.setProperty('--pcc-side', S.sideW + 'px');
                      root.style.setProperty('--pcc-term', S.termH + 'px'); }
            fitTerm(); };
          const up = () => {
            g.removeEventListener('pointermove', move);
            g.removeEventListener('pointerup', up);
            save(true);
          };
          g.addEventListener('pointermove', move);
          g.addEventListener('pointerup', up);
        });
      };
      drag('#pcc-gripv', (m) => {
        const root = $('.pcc');
        const left = root ? root.getBoundingClientRect().left : 0;
        S.sideW = Math.max(150, Math.min(600, m.clientX - left));
      });
      drag('#pcc-griph', (m) => {
        const root = $('.pcc');
        const bottom = root ? root.getBoundingClientRect().bottom : window.innerHeight;
        S.termH = Math.max(120, Math.min(900, bottom - m.clientY));
      });
    }

    // ---- the terminal panel --------------------------------------------------------------------

    /* A REAL SHELL, not a second implementation of one. PCTerm is the Terminal view's module and it
     * is a SINGLETON — one xterm, one PTY, one session id — so this hands it a container instead of
     * cloning it. Whoever renders last owns it, which is right on the desktop because only the
     * focused window is ever rendered. Loaded on demand: somebody who never opens the panel should
     * not pay for xterm. */
    function mountTerm(){
      const host = $('#pcc-termhost');
      if(!host) return;
      const go = () => {
        const T = window.PCTerm;
        if(!T || !T.render) return;
        try{ T.render(host); }
        catch(e){ host.innerHTML = '<div class="pcc-note err">Could not open a terminal: ' + enc(String(e && e.message || e)) + '</div>'; }
      };
      /* `PC.loadModule` DOES NOT EXIST — app.js keeps its loader private (`_withModule`), and
       * reaching for a helper that merely looks like it should be on the bridge is the
       * `PC._fmtBytes is not a function` trap this codebase has been bitten by more than once.
       * term.js has its own <script> tag in client.html, so the global is coming; it is simply not
       * guaranteed to be there on a cold APK or straight after a renderer reload. Wait for it the
       * way renderModuleView does, and give up out loud rather than leaving an empty panel. */
      if(window.PCTerm) return go();
      host.innerHTML = '<div class="pcc-note"><div class="spinner"></div></div>';
      let tries = 0;
      const poll = setInterval(() => {
        if(window.PCTerm){ clearInterval(poll); return go(); }
        if(++tries > 40){                       // ~4s: long past a script tag that is going to load
          clearInterval(poll);
          host.innerHTML = '<div class="pcc-note err">The terminal did not load in this build.</div>';
        }
      }, 100);
    }
    /* xterm sizes itself to its container ONCE. A panel that has just been dragged is a container
     * that changed without the window changing, which xterm's own resize observer may not see —
     * so the same `resize` event it does listen for is dispatched by hand. */
    let _fitT = null;
    function fitTerm(){
      if(!S.termOpen) return;
      if(_fitT) return;
      _fitT = setTimeout(() => { _fitT = null;
        try{ window.dispatchEvent(new Event('resize')); }catch(_){}
      }, 60);
    }

    // ---- entry -------------------------------------------------------------------------------

    /* FLUSHED SYNCHRONOUSLY ON THE WAY OUT.
     *
     * The debounce exists so typing does not write to localStorage on every character; the flush
     * exists because a monitor handoff, a renderer being reclaimed under memory pressure, and a
     * closed window all give no warning at all. `pagehide` is the one that fires in the APK's
     * WebView, where `beforeunload` frequently does not. */
    let _hooked = false;
    function hooks(){
      if(_hooked) return;
      _hooked = true;
      const flush = () => save(true);
      window.addEventListener('pagehide', flush);
      window.addEventListener('blur', flush);
      document.addEventListener('visibilitychange', () => { if(document.hidden) flush(); });
    }

    async function render(){
      hooks();
      if(!S.ready){
        restore();                 // ← the half that survives a different Electron renderer
        paint();                   // spinner, from the same paint path as everything else
        await loadConfig();
        if(!S.gate) await loadTree(S.cwd);
      }
      paint();
    }

    window.PCCode = {
      render,
      // For tests and for anything that wants to open a file from elsewhere in the app.
      open: openPath,
      _state: S,
      _highlight: highlight,
      _langOf: langOf,
    };
  }
  init();
})();
