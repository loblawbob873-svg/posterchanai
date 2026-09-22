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


  // ══════════════════════════════════════════════════════════════════════════════════════════════
  // Unified diffs — pure, DOM-free, tested under node beside the highlighter.
  // ══════════════════════════════════════════════════════════════════════════════════════════════

  /* A DIFF ROW IS ONLY CLICKABLE IF IT KNOWS WHICH LINE OF THE FILE IT IS.
   *
   * A hunk header carries the new-file start (`@@ -a,b +c,d @@`) and nothing after it repeats that
   * number, so the line a row belongs to is only knowable by WALKING: context and `+` rows advance
   * the counter, `-` rows do not (they describe a line that is no longer in the file), and a
   * `\ No newline at end of file` marker describes the row above rather than a line of its own.
   * Getting that walk wrong is not visible in the picture — the diff still reads perfectly — it
   * shows up as a click that lands a few lines off, every time, and further off the further down
   * the file you click. Which is why this is a pure function with the walk under test rather than
   * an offset computed in the middle of a click handler.
   *
   * A `-` row is given the position it was deleted FROM, i.e. the next surviving line. There is no
   * honest alternative: the text it shows is not in the file any more, so the only place to put the
   * caret is where it used to be.
   */
  function parseDiff(text){
    const src = String(text == null ? '' : text);
    const raw = src.split('\n');
    // `split` on a trailing newline leaves one empty element that is not a row of anything.
    if(raw.length && raw[raw.length - 1] === '') raw.pop();
    const rows = [];
    let nl = 0, inHunk = false;
    for(const line of raw){
      const at = /^@@+ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
      if(at){ nl = parseInt(at[1], 10); inHunk = true; rows.push({ type:'hunk', text:line, line:nl }); continue; }
      if(!inHunk){ rows.push({ type:'meta', text:line, line:0 }); continue; }
      const c = line.charAt(0);
      if(c === '\\'){ rows.push({ type:'note', text:line, line:0 }); continue; }
      if(c === '+'){ rows.push({ type:'add', text:line, line:nl }); nl++; continue; }
      if(c === '-'){ rows.push({ type:'del', text:line, line:nl }); continue; }
      if(c === ' ' || line === ''){ rows.push({ type:'ctx', text:line, line:nl }); nl++; continue; }
      // Anything else ends the hunk — a multi-file patch's next header line.
      inHunk = false; rows.push({ type:'meta', text:line, line:0 });
    }
    return rows;
  }

  /* Added/removed counts, from the SAME walk the rows come from — a second scanner would be a
   * second opinion about the same bytes and the two would drift. The `+++`/`---` file headers are
   * meta rows here and can never be counted as content, which a naive `startsWith('+')` gets wrong
   * by exactly one line in each direction for every file in the patch.
   */
  function diffCounts(text){
    let added = 0, removed = 0;
    for(const r of parseDiff(text)){
      if(r.type === 'add') added++;
      else if(r.type === 'del') removed++;
    }
    return { added, removed };
  }

  /* ══ WHAT A DISCARD COSTS, MEASURED BEFORE IT IS OFFERED ══
   *
   * "Discard every change to X? This cannot be undone." was one sentence for three different acts,
   * and it was wrong about two of them:
   *
   *   - an UNTRACKED file is not "changed" at all. Discarding it DELETES it — the desktop bridge
   *     and the node route both unlink the resolved path — and there has never been a stored copy,
   *     so there is nothing anywhere to restore from. The dialog said "discard changes" about the
   *     permanent removal of a whole file somebody had just written.
   *   - a STAGED edit is thrown away too (the restore takes the index and the working tree), which
   *     somebody who staged deliberately does not expect: staging reads as "kept".
   *
   * So the question is not "are you sure", it is Folder Sync's question: CAN THIS BE BROUGHT BACK,
   * and from where. That is answered per file from the porcelain code plus the file's own diff, and
   * the answer is stated in the dialog before anything is touched.
   *
   * THE THIRD ANSWER IS "COULD NOT ASK". A diff that failed to load is not an empty diff: if the
   * measurement did not happen the dialog must say so and must not print a count, because a
   * confident "3 lines" about a file nothing read is exactly the sentence that makes somebody
   * click. Same rule as the drive check — "the store said no" and "the store could not be asked"
   * are different answers.
   */
  function discardPlan(o){
    o = o || {};
    const path = String(o.path || '');
    const xy = (String(o.xy || '  ') + '  ').slice(0, 2);
    const untracked = xy === '??';
    const staged = !untracked && xy[0] !== ' ' && xy[0] !== '?';
    const measured = !!o.measured;
    const c = measured ? diffCounts(o.diff) : null;
    const n = c ? c.added + c.removed : 0;
    const plan = { path, xy, untracked, staged, measured,
                   added: c ? c.added : null, removed: c ? c.removed : null,
                   ok: '', danger: true, lines: [] };
    if(untracked){
      plan.ok = 'Delete file';
      plan.lines.push('Discard DELETES “' + path + '” from this computer.');
      plan.lines.push(measured && c.added
        ? 'Nothing has ever stored a copy, so its ' + c.added + ' line' + (c.added === 1 ? '' : 's')
          + ' cannot be brought back from the repository or from anywhere else.'
        : 'Nothing has ever stored a copy, so nothing can bring it back.');
    }else{
      plan.ok = 'Discard changes';
      plan.lines.push('Discard rewrites “' + path + '” with the version in the last commit.');
      if(measured){
        plan.lines.push(n
          ? c.added + ' added and ' + c.removed + ' removed line' + (n === 1 ? '' : 's')
            + ' are committed nowhere, so they cannot be brought back.'
          : 'Nothing in this file differs from the last commit.');
      }
      if(staged) plan.lines.push('The staged copy goes too — staging is not a backup.');
    }
    if(!measured){
      plan.ok = 'Discard anyway';
      plan.lines.push('PosterChan could not read what this would lose'
        + (o.error ? ' (' + String(o.error) + ')' : '') + ', so it cannot tell you what goes.');
    }
    plan.message = plan.lines.join('\n\n');
    return plan;
  }

  window.PCCodeHL = { highlight, langOf, esc, RULES, HL_MAX };

  // ══════════════════════════════════════════════════════════════════════════════════════════════
  // Find / replace — pure, DOM-free, run under node by tests/client/test_code_find_replace.py.
  // ══════════════════════════════════════════════════════════════════════════════════════════════

  /* ONE compiler for every search on this screen — the find bar's count, its marks, Replace,
   * Replace all and Search in files — so the number shown and what Replace all changes cannot
   * disagree. A bad regex is an ANSWER ({error}), never a throw: it is typed a character at a
   * time, and `(` on its way to `(\w+)` must not take the screen down. */
  const reEsc = (s) => String(s).replace(/[.*+?^${}()|[\]\\\/]/g, '\\$&');
  function findRe(q, o){
    o = o || {};
    if(!q) return { re: null, error: '' };
    let src = o.re ? q : reEsc(q);
    if(o.ww) src = '(?<!\\w)(?:' + src + ')(?!\\w)';
    try{ return { re: new RegExp(src, 'gm' + (o.cs ? '' : 'i')), error: '' }; }
    catch(_){ return { re: null, error: 'Invalid regular expression' }; }
  }
  /* Every match as [start, end]. EMPTY matches are skipped (`^`, `a*`): they cannot be drawn or
   * selected, and counting them made "3 of 17" name matches nobody could see. Bounded, and says so. */
  const FIND_MAX = 20000;
  function findAll(text, q, o, max){
    // `max ?? FIND_MAX`, never `||`: a caller with no room left passes 0, and `||` read that as
    // "no limit" — a parallel search lane could then add 20,000 hits past its 2,000 cap.
    const c = findRe(q, o), out = [], lim = max ?? FIND_MAX;
    if(!c.re) return { ranges: out, error: c.error, capped: false };
    let m;
    while((m = c.re.exec(text)) !== null){
      if(!m[0]){ c.re.lastIndex++; continue; }
      if(out.length >= lim) return { ranges: out, error: '', capped: true };
      out.push([m.index, m.index + m[0].length]);
    }
    return { ranges: out, error: '', capped: false };
  }
  /* `$1`, `$<name>`, `$&`, `$$` — JavaScript's own replacement syntax, expanded by hand because a
   * match is re-run at one position rather than through String.replace. `$12` with one group is
   * `$1` then "2", as in JS. Plain-text mode never expands anything: there `$1` is two characters. */
  function expand(tpl, m){
    return String(tpl).replace(/\$(\$|&|<([^>]*)>|\d\d?)/g, (all, k, name) => {
      if(k === '$') return '$';
      if(k === '&') return m[0];
      if(name !== undefined) return m.groups && name in m.groups ? (m.groups[name] || '') : all;
      const g = (n) => (m[n] == null ? '' : m[n]);
      if(+k >= 1 && +k < m.length) return g(+k);
      if(k.length === 2 && +k[0] >= 1 && +k[0] < m.length) return g(+k[0]) + k[1];
      return all;
    });
  }
  function replacementAt(text, s, q, o, repl){
    const re = o && o.re ? findRe(q, o).re : null;
    if(!re) return String(repl);
    re.lastIndex = s;
    const m = re.exec(text);
    return m && m.index === s ? expand(repl, m) : String(repl);
  }
  /* Built from the SAME match list the count came from — never a second String.replace, which
   * would also rewrite the empty matches findAll deliberately does not count. */
  function replaceAll(text, q, o, repl){
    const f = findAll(text, q, o, Infinity);
    let out = '', last = 0;
    for(const [s, e] of f.ranges){ out += text.slice(last, s) + replacementAt(text, s, q, o, repl); last = e; }
    return { text: f.ranges.length ? out + text.slice(last) : text, count: f.ranges.length, error: f.error };
  }
  /* Which match to go to from a caret position, WRAPPING: forward is the first starting at or
   * after `pos` (else the first), backward the last starting before it (else the last). */
  function pick(ranges, pos, dir){
    const n = ranges.length;
    if(!n) return -1;
    if(dir < 0){ for(let i = n - 1; i >= 0; i--) if(ranges[i][0] < pos) return i; return n - 1; }
    for(let i = 0; i < n; i++) if(ranges[i][0] >= pos) return i;
    return 0;
  }
  const step = (i, n, dir) => (n ? ((i < 0 ? (dir < 0 ? 0 : -1) : i) + dir + n) % n : -1);
  /* Search in files: matches as lines. Only a WINDOW of each line is kept — a minified file is one
   * line of a megabyte, and two thousand hits must not each hold a copy of it. */
  function grep(text, q, o, max){
    const f = findAll(text, q, o, max), hits = [];
    let line = 1, ls = 0;
    for(const [s, e] of f.ranges){
      for(let nl = text.indexOf('\n', ls); nl >= 0 && nl < s; nl = text.indexOf('\n', ls)){ line++; ls = nl + 1; }
      let le = text.indexOf('\n', s); if(le < 0) le = text.length;
      hits.push({ line, s, e, pre: text.slice(Math.max(ls, s - 40), s), mid: text.slice(s, Math.min(e, s + 200)),
                  post: e < le ? text.slice(e, Math.min(le, e + 80)) : '' });
    }
    return { hits, error: f.error, capped: f.capped };
  }
  window.PCCodeFind = { findRe, findAll, expand, replaceAll, replacementAt, pick, step, grep, FIND_MAX };

  // ══════════════════════════════════════════════════════════════════════════════════════════════
  // State — see the header. Everything here is painted from, and mirrored to localStorage.
  // ══════════════════════════════════════════════════════════════════════════════════════════════

  const S = {
    ready: false,
    root: '', hostRoot: '', engines: {}, gate: '', // hostRoot: desktop project chosen by native picker
    cwd: '', tree: [], treeErr: '', treeBusy: false,
    expanded: {},                            // dir path → true (the tree remembers what you opened)
    open: [],                                // [{path, lang, text, disk, mtime, sel, scroll}]
    active: -1,
    termOpen: false, termH: 260, sideW: 250,
    gitOpen: false, git: null, gitBusy: false, gitDiff: null,
    status: '', statusKind: '',
    busy: false,
    // The find bar and Search in files. `at` is where the next search starts from (the caret).
    find: { open: false, repl: false, q: '', r: '', cs: false, ww: false, re: false, at: 0 },
    sfOpen: false, sf: { q: '', cs: false, ww: false, re: false, busy: false, results: [], note: '', err: '' },
  };
  let _gitDiffSeq = 0;

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
  /* A file handed in by Files is already the authoritative in-memory state.  On the first Code
   * paint, restore() must not replace it with whichever tab localStorage held yesterday. */
  let _incoming = false;
  function persist(){
    /* Written whole, never merged. Two renderers of the same origin can both hold this key during a
     * monitor handoff, and a read-modify-write between them would interleave two editors' tabs. The
     * window being LOOKED AT is the one that writes, and the last write wins — which is what the
     * person means by "this is where I was". */
    try{
      const f = S.find, q = S.sf;
      const slim = { cwd: S.cwd, hostRoot:S.hostRoot, active: S.active, gitOpen:S.gitOpen, sfOpen:S.sfOpen,
                     find: { open:f.open, repl:f.repl, q:f.q, r:f.r, cs:f.cs, ww:f.ww, re:f.re, at:f.at },
                     sf: { q:q.q, cs:q.cs, ww:q.ww, re:q.re },
                     termOpen: S.termOpen, termH: S.termH,
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
        host: d.host || null,
        blob: d.blob || null,
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
      S.hostRoot = typeof v.hostRoot === 'string' ? v.hostRoot : '';
      S.gitOpen = !!v.gitOpen;
      S.sfOpen = !!v.sfOpen && !S.gitOpen;
      for(const [k, o] of [['find', v.find], ['sf', v.sf]]) if(o && typeof o === 'object')
        for(const key of Object.keys(S[k])) if(typeof o[key] === typeof S[k][key]) S[k][key] = o[key];
      S.termOpen = !!v.termOpen;
      S.termH = Math.max(120, Math.min(900, Number(v.termH) || 260));
      S.sideW = Math.max(150, Math.min(600, Number(v.sideW) || 250));
      S.expanded = (v.expanded && typeof v.expanded === 'object') ? v.expanded : {};
      S.open = Array.isArray(v.open) ? v.open.filter(d => d && typeof d.path === 'string').map(d => ({
        path: d.path, lang: d.lang || langOf(d.path), text: typeof d.text === 'string' ? d.text : null,
        disk: typeof d.disk === 'string' ? d.disk : null, mtime: Number(d.mtime) || 0,
        host: d.host && typeof d.host.path === 'string' ? {path:d.host.path} : null,
        blob: d.blob && typeof d.blob.sha === 'string' ? {
          sha:d.blob.sha, name:String(d.blob.name||d.path), mime:String(d.blob.mime||''),
          enc:String(d.blob.enc||'0'),
          sync:d.blob.sync && typeof d.blob.sync.key === 'string'
            ? {key:d.blob.sync.key, path:String(d.blob.sync.path||'')} : null,
        } : null,
        sel: d.sel && typeof d.sel === 'object' ? d.sel : { s: 0, e: 0 }, scroll: Number(d.scroll) || 0,
      })) : [];
      S.active = (Number.isInteger(v.active) && v.active >= 0 && v.active < S.open.length) ? v.active : (S.open.length ? 0 : -1);
    }catch(_){ /* a corrupt blob is not worth refusing to open the editor over */ }
  }

  function init(){
    const PC = window.__PC;
    if(!PC){ return setTimeout(init, 50); }
    const { $, enc, toast, authFetch, ensureAiSession, uiPrompt, uiConfirm } = PC;
    const inView = () => window.__PC.VIEW === 'code';

    // ---- server ------------------------------------------------------------------------------

    /* NOTHING HERE MAY WAIT FOR EVER.
     *
     * `ensureAiSession` can be waiting on a SIGNER — a phone, an extension prompt nobody saw — and
     * a signer that never answers is not an error, it is silence. `render()` paints a spinner,
     * awaits loadConfig, and only paints again afterwards, so one unanswered signature turned this
     * whole screen into a black box with a circle in it. loadConfig's own catch is careful and was
     * never reached, because a promise that never settles is not a rejection.
     *
     * A timeout is the only thing that turns "never answers" back into something a catch can see. */
    function _bounded(p, ms, what){
      if(!p || typeof p.then !== 'function') return Promise.resolve(p);
      return Promise.race([p, new Promise((_r, rej) => setTimeout(
        () => rej(new Error((what || 'this node') + ' did not answer — is your signer awake?')),
        ms || 15000))]);
    }

    async function api(path, opts){
      try{ await _bounded(ensureAiSession && ensureAiSession(), 8000, 'the signer'); }catch(_){}
      const r = await _bounded(authFetch('/api/code' + path, opts), 20000, 'this node');
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

    function missingPathError(e){
      /* Electron may preserve ENOENT as a structured error code while replacing the message with
       * "Error invoking remote method".  Looking at the prose alone leaves a deleted saved
       * workspace installed forever. */
      return !!e && (e.code === 'ENOENT' || (e.cause && e.cause.code === 'ENOENT') ||
        /ENOENT|no such file or directory/i.test(String(e.message || e)));
    }

    async function loadConfig(){
      /* A packaged desktop edits the folder the person selected. A clean profile has selected
       * nothing, so it must not fall through to /api/code/config and quietly open PosterChan's own
       * server checkout. */
      if(window.pcHost && window.pcHost.pickDirectory){
        S.engines={};S.root=S.hostRoot||'No folder open';S.gate='';S.ready=true;return;
      }
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
        if(S.hostRoot){
          const h=window.pcHost;if(!h||!h.list)throw new Error('this desktop build cannot browse projects');
          const t=await h.list(path||S.hostRoot);S.cwd=t.path||S.hostRoot;
          S.tree=(t.entries||[]).map(e=>({name:e.name,path:e.path,dir:!!e.dir,lang:langOf(e.name)}));
          S.root=S.hostRoot; S.gate='';
        }else{
        const t = await api('/tree?path=' + encodeURIComponent(path || ''));
        S.cwd = t.path || '';
        S.tree = t.entries || [];
        if(t.truncated) status('This folder has more files than the tree will show', 'warn');
        }
      }catch(e){
        if(S.hostRoot && missingPathError(e)){
          /* A selected folder can be deleted, unmounted, or be a temporary installed-test folder
             that has already been cleaned up. Never reopen a dead path forever from localStorage. */
          S.hostRoot='';S.root='No folder open';S.cwd='';S.tree=[];
          S.treeErr='That project folder is no longer available — choose another folder';
        }else{
        // A FAILED LISTING KEEPS THE LAST GOOD ONE. Blanking the tree on a transient error makes an
        // unreachable node look like an empty workspace — the same rule the file screens follow.
        S.treeErr = e.message || 'Could not read that folder';
        }
      }
      S.treeBusy = false;
      save();
      if(inView()) paint();
    }

    async function openHostFolder(desc){
      const path=String(typeof desc==='string'?desc:(desc&&desc.path)||'').trim();
      const h=window.pcHost;
      if(!path)return false;
      if(!h||!h.list){status('this build cannot browse local folders','err');return false;}
      /* Validate before dropping open buffers. Files may hand us a stale shortcut or an unmounted
       * drive; failing to open that must not also discard the project that is already open. */
      let t;
      try{t=await h.list(path);}
      catch(e){status((e&&e.message)||'Could not open that folder','err');return false;}
      S.hostRoot=path;S.cwd=t.path||path;S.root=path;S.gate='';
      S.tree=(t.entries||[]).map(e=>({name:e.name,path:e.path,dir:!!e.dir,lang:langOf(e.name)}));
      S.treeErr='';S.open=[];S.active=-1;S.gitOpen=false;cancelGitDiff();S.git=null;
      _sfSeq++;Object.assign(S.sf,{busy:false,results:[],note:'',err:''});
      _incoming=true;
      save(true);
      if(inView())paint();
      return true;
    }

    async function loadGit(){
      // A native desktop has no implicit workspace. Calling gitStatus("") makes Electron fall back
      // to its own process cwd, which exposed PosterChan's repository as if the person had opened
      // it. Source Control, Explorer and the terminal must all belong to the folder they chose.
      if(window.pcHost&&pcHost.pickDirectory&&!S.hostRoot){
        S.git=null;S.gitBusy=false;status('Choose a working directory to use Source Control','');
        paint();return;
      }
      S.gitBusy = true; paint();
      try{ S.git = S.hostRoot&&window.pcHost&&pcHost.gitStatus
          ? await pcHost.gitStatus(S.hostRoot) : await api('/git/status'); status('Git status refreshed'); }
      catch(e){ S.git = { error: e.message || String(e), files: [] }; }
      S.gitBusy = false; paint();
    }

    async function gitAct(action, paths, message){
      try{
        status(action + '…');
        if(S.hostRoot&&window.pcHost&&pcHost.gitAction)await pcHost.gitAction(S.hostRoot,action,paths||[],message||'');
        else await post('/git/action', { action, paths: paths || [], message: message || '' });
        await loadGit();
        if(action === 'pull') await loadTree(S.cwd);
        status(action + ' complete', 'ok');
      }catch(e){ status((e && e.message) || (action + ' failed'), 'err'); }
    }

    /* ONE transport for a diff, used by the pane AND by the discard measurement. Two callers
     * fetching the same patch by two routes is how a dialog ends up describing something other
     * than what the pane is showing. Returns `{text, ok, error}` — `ok:false` is "could not ask",
     * which is never the same answer as an empty diff. */
    async function fetchDiff(path){
      try{
        const d=S.hostRoot&&window.pcHost&&pcHost.gitDiff ? await pcHost.gitDiff(S.hostRoot,path)
          : await api('/git/diff?path='+encodeURIComponent(path));
        return {text:(d&&d.diff)||'', ok:true, error:''};
      }catch(e){ return {text:'', ok:false, error:(e&&e.message)||String(e)}; }
    }

    /* Diff requests can resolve out of order, and changing back to Explorer does not cancel fetch.
     * Only the latest still-visible request may own the editor pane; otherwise a slow diff for A
     * replaces a newer B diff, or reappears over the editor after Explorer was selected. */
    function cancelGitDiff(){ _gitDiffSeq++; S.gitDiff=null; }
    async function loadGitDiff(path){
      const seq=++_gitDiffSeq;
      S.gitDiff={path,text:'',error:'',busy:true}; paint();
      const r=await fetchDiff(path);
      if(seq!==_gitDiffSeq || !S.gitOpen) return false;
      S.gitDiff={path,text:r.ok?r.text:'',error:r.ok?'':r.error,busy:false};
      paint();return true;
    }

    /* A SOURCE CONTROL PATH IS RELATIVE TO THE REPOSITORY, AND THE EDITOR OPENS SOMETHING ELSE.
     *
     * The native bridge answers with an absolute repository root and reads absolute paths; the node
     * route answers with a root relative to the configured workspace and reads workspace-relative
     * paths. Handing either one the bare porcelain path opens the wrong file whenever the folder
     * somebody picked is not itself the top of the repository — silently, because a path that does
     * not resolve reads as "could not open that file", which looks like a permissions problem. */
    function gitFilePath(rel){
      const p=String(rel||''); if(!p) return '';
      const g=S.git||{};
      if(S.hostRoot){
        const base=String(g.root||S.hostRoot).replace(/\/+$/,'');
        return base ? base+'/'+p : p;
      }
      const base=String(g.repo||'').replace(/^\/+|\/+$/g,'');
      return base ? base+'/'+p : p;
    }

    /* Put the caret on a 1-based line. Answers the 0-based row it chose, or -1.
     *
     * The offset is computed from the BUFFER, never from the DOM, because at the moment this runs
     * the textarea usually does not exist: the pane is still showing the patch and the paint that
     * mounts the editor happens afterwards. `S` is what every repaint paints from, so a position
     * written anywhere else is lost by the very next render — the rule the header states. */
    function gotoLine(n){
      const d=doc(); if(!d||typeof d.text!=='string') return -1;
      const lines=d.text.split('\n');
      const idx=Math.max(0,Math.min(lines.length-1,(Number(n)||1)-1));
      let off=0; for(let i=0;i<idx;i++) off+=lines[i].length+1;
      d.sel={s:off,e:off+(lines[idx]||'').length};
      save();
      return idx;
    }

    /* And SCROLL there, which can only be done once the textarea is mounted — a caret set on line
     * 400 of a file displayed from line 1 is a selection nobody can see, which is indistinguishable
     * from a click that did nothing. Line height is a CSS fact (`.pcc-layer` is `white-space:pre`,
     * so one line is exactly one line tall), so the arithmetic is exact rather than an estimate. */
    function scrollToLine(idx){
      const ta=$('#pcc-ta'), d=doc();
      if(!ta||!d||idx<0) return;
      let lh=0;
      try{ lh=parseFloat(getComputedStyle(ta).lineHeight)||0; }catch(_){}
      if(!lh) lh=ta.scrollHeight/Math.max(1,(ta.value.match(/\n/g)||[]).length+1);
      // Three lines of lead-in, so the line lands in the reading position rather than hard against
      // the top edge, where it reads as the beginning of the file.
      d.scroll=Math.max(0,(idx-3)*lh);
      ta.scrollTop=d.scroll;
      syncScroll();
      save();
    }

    /* Clicking a line of a diff opens the file THERE. Source Control is left behind on purpose:
     * the person asked to look at the code, and leaving the patch mounted over the editor is the
     * stale-pane failure the discard path already had to be taught about. */
    async function openDiffAt(rel, line){
      const target=gitFilePath(rel);
      if(!target){ status('That change has no file to open','err'); return false; }
      const ok=await openPath(target);
      if(!ok) return false;
      S.gitOpen=false; cancelGitDiff();
      await hydrate(doc());
      const idx=gotoLine(line);
      if(inView()) paint();
      restoreCaret();
      scrollToLine(idx);
      save(true);
      return true;
    }

    /* THE DESTRUCTIVE ACTION MEASURES WHAT IT WOULD LOSE BEFORE IT ASKS.
     *
     * See `discardPlan`. The order here is the whole point: read the patch, build the sentence from
     * what came back, and only then open the dialog. A confirmation written before the measurement
     * can only be generic, and a generic confirmation on a routine action is a confirmation people
     * click. If the read fails, the dialog says so and offers "Discard anyway" — refusing outright
     * would strand somebody whose repository is fine and whose diff simply timed out. */
    async function discardFile(path){
      const row=((S.git&&S.git.files)||[]).find(f=>f.path===path);
      /* Read FRESH, never from the patch already on screen. That one was fetched when somebody
       * clicked the file, and the file can have been edited and saved in this very editor since —
       * a dialog that describes the older patch understates exactly the work it is about to
       * destroy, and it does so on the reading somebody is most likely to trust. */
      const r=await fetchDiff(path);
      const plan=discardPlan({path,xy:row&&row.xy,diff:r.text,measured:r.ok,error:r.error});
      if(!await uiConfirm(plan.message,{ok:plan.ok,cancel:'Keep it',danger:true})) return false;
      /* Clear the diff BEFORE gitAct: gitAct finishes by loadGit(), and loadGit's paint is the
       * final repaint for this action. Clearing it afterwards changed state but left the old diff
       * visibly mounted until somebody clicked Explorer or another file. The disk restore had
       * succeeded while Code still showed the discarded patch — exactly the kind of stale UI
       * that makes a destructive Source Control button impossible to trust. */
      if(S.gitDiff && S.gitDiff.path===path) cancelGitDiff();
      await gitAct('restore',[path]);
      return true;
    }

    /* A DOCUMENT THAT IS NOT A FILE ON THIS NODE.
     *
     * Files → Blossom holds content-addressed BLOBS, not paths: there is nothing for `/api/code/file`
     * to open and nothing for it to save to. So a blob rides in as a buffer with a `blob` descriptor
     * instead of a workspace path, and saving it goes back the way it came — re-uploaded, with the
     * drive index re-pointed at the new hash. Everything else about the buffer (highlighting, tabs,
     * the caret, dirty tracking) is the same object the rest of this file already understands.
     *
     * The editor knows nothing about Blossom: the round trip lives in app.js beside the drive index
     * (`PC.saveBlobDoc`), which is where the encryption, the folder and the index all are. */
    function openBlob(desc){
      if(!desc || !desc.sha) return false;
      const name = desc.name || 'document';
      const sync = desc.sync && typeof desc.sync.key === 'string'
        ? { key:desc.sync.key, path:String(desc.sync.path||'') } : null;
      const at = S.open.findIndex(d => d.blob && (sync
        ? d.blob.sync && d.blob.sync.key === sync.key && d.blob.sync.path === sync.path
        : !d.blob.sync && d.blob.sha === desc.sha));
      if(at >= 0){ S.active = at; _incoming = true; save(true); if(inView()) paint(); return true; }
      const text = String(desc.text == null ? '' : desc.text);
      S.open.push({ path: name, blob: { sha: desc.sha, name, mime: desc.mime || '', enc: desc.enc || '0', sync },
                    lang: langOf(name), text, disk: text, mtime: 0, sel: { s: 0, e: 0 }, scroll: 0 });
      S.active = S.open.length - 1;
      _incoming = true;
      status('');
      // The view switch follows synchronously. A debounced write lets its first render restore a
      // stale tab before this buffer reaches storage; flush now as the cross-renderer fallback.
      save(true); if(inView()) paint();
      return true;
    }

    /* A FILE ON THIS COMPUTER, opened in the editor. Same buffer shape as everything else — the
     * only difference is where its bytes came from and where Save puts them back. */
    async function openHostFile(desc){
      if(!desc || !desc.path) return false;
      const at = S.open.findIndex(d => d.host && d.host.path === desc.path);
      if(at >= 0){ S.active = at; save(); if(inView()) paint(); return true; }
      const H = window.PCHostFiles;
      if(!H || !H.readText){ status('this build cannot open a local file', 'err'); return false; }
      status('Opening ' + desc.path + '…');
      try{
        const f = await H.readText(desc.path);
        const name = String(desc.path).split('/').pop() || desc.path;
        S.open.push({ path: name, host: { path: f.path || desc.path }, lang: langOf(name),
                      text: f.text, disk: f.text, mtime: f.mtime || 0, sel: { s:0, e:0 }, scroll: 0 });
        S.active = S.open.length - 1;
        _incoming = true;
        status('');
      }catch(e){ status((e && e.message) || 'Could not open that file', 'err'); return false; }
      save(); if(inView()) paint();
      return true;
    }

    /* Answers WHETHER it opened. A caller that goes on to do something to the buffer — the diff's
     * "open this line" is the one that exists today — must not act on the tab that happened to be
     * active when the open failed, which is what an unconditional `undefined` had it doing. */
    async function openPath(path){
      if(S.hostRoot) return openHostFile({path});
      const at = S.open.findIndex(d => d.path === path);
      if(at >= 0){ S.active = at; save(); paint(); return true; }
      status('Opening ' + path + '…');
      let ok = false;
      try{
        const f = await api('/file?path=' + encodeURIComponent(path));
        S.open.push({ path: f.path, lang: f.lang || langOf(f.path), text: f.text, disk: f.text,
                      mtime: f.mtime || 0, sel: { s: 0, e: 0 }, scroll: 0 });
        S.active = S.open.length - 1;
        status('');
        ok = true;
      }catch(e){ status(e.message || 'Could not open that file', 'err'); }
      save(); paint();
      return ok;
    }

    /* A buffer restored from localStorage with `text:null` was too big to persist — it is clean, so
     * disk is the truth. Fetched lazily when it is first LOOKED at rather than all at once on
     * restore: reopening a window with nine tabs should cost one request, not nine. */
    async function hydrate(d){
      if(!d || d.text !== null) return;
      /* A BLOB HAS NO PATH TO RE-READ. Asked for one, `/api/code/file?path=<a file name>` resolves
       * against the workspace and 400s — so a restored blob buffer would come back as an error
       * about a file that was never on this node. Its text was persisted with it, or it is gone. */
      if(d.blob){ d.text = d.text || ''; d.disk = d.text; return; }
      /* A LOCAL FILE IS RE-READ FROM THE DISK, not from `/api/code/file` — that resolves against the
       * server's workspace and would answer about a different file, or 400. */
      if(d.host){
        try{ const H = window.PCHostFiles;
             const f = H && H.readText ? await H.readText(d.host.path) : null;
             d.text = f ? f.text : ''; d.disk = d.text; d.mtime = (f && f.mtime) || 0; }
        catch(_){ d.text = ''; d.disk = ''; status('Could not re-open ' + d.path, 'err'); }
        return;
      }
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
        if(d.host){
          /* A FILE ON THIS COMPUTER. Straight back to the disk it came from — no Blossom, no
           * manifest, no server. `mtime` is a compare-and-swap in the bridge: a terminal sitting
           * beside this editor is the likeliest thing to have changed the file, and overwriting
           * that silently is how somebody loses work they did in the other window. */
          const H = window.PCHostFiles;
          if(!H || !H.writeText) throw new Error('this build cannot save a local file');
          let r;
          try{ r = await H.writeText(d.host.path, d.text, d.mtime || 0); }
          catch(e){
            const m = String((e && e.message) || e);
            if(m.indexOf('changed-on-disk') >= 0)
              throw new Error(d.path + ' changed on disk since you opened it — reload it to see the new version');
            throw e;
          }
          d.disk = d.text; d.mtime = (r && r.mtime) || 0;
          status('Saved ' + d.path, 'ok');
          S.busy = false; save(); paint(); return;
        }
        if(d.blob){
          /* Back to the drive it came from. A new hash is a NEW BLOB — content addressing means an
           * edit cannot overwrite the old bytes — so app.js re-points the index and the old blob is
           * left recoverable, exactly as the office editor's save does. */
          const saver = (window.__PC && window.__PC.saveBlobDoc);
          if(!saver) throw new Error('this build cannot save back to Files');
          const sha = await saver(d.blob, d.text);
          if(sha) d.blob.sha = sha;
          d.disk = d.text;
          status('Saved ' + d.blob.name + ' to Files', 'ok');
          S.busy = false; save(); paint(); return;
        }
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
        /* A selected desktop project belongs to THIS computer. Sending its display name to the
         * node workspace can either fail or, worse, load a same-named file from PosterChan's own
         * checkout into this tab. Keep Reload on the same authority/path as Open and Save. */
        let f;
        if(d.host){
          const H = window.PCHostFiles;
          if(!H || !H.readText) throw new Error('this build cannot reload a local file');
          f = await H.readText(d.host.path);
        }else{
          f = await api('/file?path=' + encodeURIComponent(d.path));
        }
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
      if(S.hostRoot){
        const root=S.hostRoot.replace(/\/+$/,''),rel=S.cwd.slice(root.length).split('/').filter(Boolean);
        let acc=root,out=['<button class="pcc-crumb" data-go="'+enc(root)+'">'+enc(root.split('/').pop()||root)+'</button>'];
        for(const p of rel){acc+='/'+p;out.push('<span class="pcc-sep">/</span><button class="pcc-crumb" data-go="'+enc(acc)+'">'+enc(p)+'</button>');}
        return out.join('');
      }
      const parts = S.cwd ? S.cwd.split('/') : [];
      let acc = '';
      const out = ['<button class="pcc-crumb" data-go="">workspace</button>'];
      for(const p of parts){
        acc = acc ? acc + '/' + p : p;
        out.push('<span class="pcc-sep">/</span><button class="pcc-crumb" data-go="' + enc(acc) + '">' + enc(p) + '</button>');
      }
      return out.join('');
    }

    // Has a folder actually been chosen? On a packaged desktop nothing is open until somebody picks
    // one — deliberately, so Code cannot come up sitting in PosterChan's own checkout.
    const noFolder = () => !!(window.pcHost && window.pcHost.pickDirectory) && !S.hostRoot;

    function treeHtml(){
      if(S.treeErr) return '<div class="pcc-note err">' + enc(S.treeErr) + '</div>';
      /* "This folder is empty" was drawn for BOTH an empty folder and no folder at all, and the two
       * need opposite things from the reader: one is a fact about a project, the other is the app
       * waiting to be told which project. Nothing on the screen distinguished them, so a fresh
       * install looked like a broken tree. */
      if(noFolder()) return '<div class="pcc-note">No folder is open.<br><br>' +
        '<button class="btn btn-neon small" id="pcc-open-empty">Open Folder…</button></div>';
      if(!S.tree.length) return '<div class="pcc-note">This folder is empty</div>';
      return S.tree.map(e => {
        const path = e.path || (S.cwd ? S.cwd + '/' + e.name : e.name);
        const isOpen = S.open.some(d => d.path === path);
        return '<button class="pcc-item' + (isOpen ? ' on' : '') + '" data-' + (e.dir ? 'dir' : 'file') +
               '="' + enc(path) + '" title="' + enc(path) + '">' +
               '<span class="pcc-ic">' + icon(e) + '</span><span class="pcc-nm">' + enc(e.name) + '</span></button>';
      }).join('');
    }

    function gitHtml(){
      if(S.gitBusy) return '<div class="pcc-note"><div class="spinner"></div></div>';
      const g=S.git;
      if(!g) return '<div class="pcc-note">Choose a working directory to use Source Control.</div>';
      if(g.error) return '<div class="pcc-note err">' + enc(g.error) + '</div>';
      const files=g.files||[];
      return '<div class="pcc-git-head"><b>' + enc(g.branch||'Git') + '</b><small>' +
        (g.nostr?'Nostr remote · built in':enc(g.origin||'local repository')) + '</small></div>' +
        '<div class="pcc-git-actions"><button data-git-act="pull">Pull</button><button data-git-act="push">Push</button></div>' +
        (files.length?files.map(f=>'<div class="pcc-git-file"><button data-git-diff="'+enc(f.path)+'"><code>'+enc(f.xy)+'</code><span>'+enc(f.path)+'</span></button><button title="'+(f.xy[0]!==' '?'Unstage':'Stage')+'" data-git-act="'+(f.xy[0]!==' '?'unstage':'stage')+'" data-git-path="'+enc(f.path)+'">'+(f.xy[0]!==' '?'−':'+')+'</button><button class="pcc-git-danger" title="'+(f.xy==='??'?'Delete file':'Discard changes')+'" aria-label="'+(f.xy==='??'?'Delete untracked file ':'Discard changes in ')+enc(f.path)+'" data-git-restore="'+enc(f.path)+'">'+(f.xy==='??'?'🗑':'↶')+'</button></div>').join(''):'<div class="pcc-note">Working tree clean</div>') +
        '<div class="pcc-git-commit"><input id="pcc-git-message" placeholder="Commit message" maxlength="5000"><button data-git-act="commit">Commit</button></div>';
    }

    /* THE DIFF IS A LIST OF PLACES IN A FILE, so every row that has a place is a button that goes
     * there. It used to be one escaped <pre>: correct, unreadable at a glance, and — the part
     * that mattered — a dead end. Somebody looking at a change they wanted to fix had to read a
     * line number off a hunk header, count rows down from it, switch to Explorer, find the file in
     * the tree and scroll to it. Everything needed to do that in one click was already on screen.
     *
     * Rows are BUTTONS, not one click handler on the block: a diff is exactly the surface where
     * somebody selects text in order to copy it, and a block-level handler turns the end of every
     * such drag into a navigation. Meta and "no newline" rows stay inert — they name no line.
     *
     * One element per line means a patch is BOUNDED the way the highlighter is (see HL_MAX): a
     * whole-repository diff truncated at half a megabyte is tens of thousands of rows, and building
     * that many nodes to answer one click is the same trade the colouring already refuses. The cut
     * is SAID, never silent — a patch that quietly stops halfway reads as a smaller change than it
     * is, which on this screen is the one misreading that matters.
     */
    const DIFF_ROW_MAX = 4000;

    function diffRowsHtml(text){
      const all = parseDiff(text);
      const rows = all.slice(0, DIFF_ROW_MAX);
      const cut = all.length - rows.length;
      return rows.map(r => {
        const cls = 'pcc-dl pcc-dl-' + r.type;
        if(r.type === 'meta' || r.type === 'note' || !r.line)
          return '<div class="' + cls + '"><span class="pcc-dn"></span><code>' + enc(r.text) + '</code></div>';
        const t = r.type === 'del'
          ? 'Open line ' + r.line + ', where this line used to be'
          : 'Open line ' + r.line;
        return '<button class="' + cls + '" data-diff-line="' + r.line + '" title="' + enc(t) + '">' +
               '<span class="pcc-dn">' + (r.type === 'del' ? '' : r.line) + '</span>' +
               '<code>' + enc(r.text) + '</code></button>';
      }).join('') + (cut ? '<div class="pcc-dl pcc-dl-meta"><span class="pcc-dn"></span><code>' +
        enc('… ' + cut + ' more lines in this patch — open the file to read the rest') +
        '</code></div>' : '');
    }

    function diffHtml(){
      const d=S.gitDiff;
      if(!d) return editorHtml();
      const head = '<section class="pcc-diff-view" aria-label="Diff for '+enc(d.path)+'">' +
        '<header><b>'+enc(d.path)+'</b><span>'+(d.busy?'Loading…':'Click a line to open it')+'</span>' +
        '<button id="pcc-diff-close" title="Close diff" aria-label="Close diff">×</button></header>';
      if(d.busy) return head + '<div class="pcc-note"><div class="spinner"></div></div></section>';
      if(d.error) return head + '<div class="pcc-note err">'+enc(d.error)+'</div></section>';
      if(!d.text) return head + '<div class="pcc-note">No changes to display</div></section>';
      return head + '<div class="pcc-git-diff" id="pcc-diffbody">'+diffRowsHtml(d.text)+'</div></section>';
    }

    function activityHtml(){
      return '<nav class="pcc-activity" aria-label="Code views">' +
        '<button data-code-view="explorer" class="'+(S.gitOpen||S.sfOpen?'':'on')+'" title="Working Directory" aria-label="Working Directory"><svg class="ic"><use href="#i-folder"></use></svg></button>' +
        '<button data-code-view="git" class="'+(S.gitOpen?'on':'')+'" title="Source Control" aria-label="Source Control"><svg class="ic"><use href="#i-git"></use></svg>' +
          (S.git&&S.git.files&&S.git.files.length?'<em>'+S.git.files.length+'</em>':'')+'</button>' +
        '<button data-code-view="search" class="'+(S.sfOpen?'on':'')+'" title="Search in files (Ctrl+Shift+H)" aria-label="Search in files"><svg class="ic"><use href="#i-search"></use></svg></button></nav>';
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
               '<pre class="pcc-layer pcc-fm" id="pcc-fm" aria-hidden="true"></pre>' +
               '<textarea class="pcc-layer pcc-ta" id="pcc-ta" spellcheck="false" autocapitalize="off" ' +
                 'autocorrect="off" autocomplete="off" wrap="off" aria-label="' + enc(d.path) + '">' +
                 esc(d.text) + '</textarea>' +
             '</div>' + (S.find.open ? findBarHtml() : '') + '</div>';
    }

    function toolbarHtml(){
      const d = doc();
      const eng = d ? (S.engines[d.lang] || (d.lang === 'json' ? 'json' : '')) : '';
      return '<div class="pcc-bar">' +
        '<button class="btn btn-ghost pcc-b" id="pcc-open-folder">' +
          (noFolder() ? 'Open Folder…' : 'Change Working Directory') + '</button>' +
        '<button class="btn btn-neon pcc-b" id="pcc-save"' + (d && dirty(d) ? '' : ' disabled') + '>Save</button>' +
        '<button class="btn btn-ghost pcc-b" id="pcc-fmt"' + (d && eng ? '' : ' disabled') + ' title="' +
          (eng ? 'Beautify with ' + enc(eng) : 'No formatter on this node for this language') + '">Format</button>' +
        '<button class="btn btn-ghost pcc-b" id="pcc-reload"' + (d ? '' : ' disabled') + '>Reload</button>' +
        '<button class="btn btn-ghost pcc-b" id="pcc-find-b"' + (d && !S.gitDiff ? '' : ' disabled') +
          ' title="Find and replace (Ctrl+F, Ctrl+H)">Find</button>' +
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
            activityHtml() +
            '<aside class="pcc-side" id="pcc-side">' +
              '<div class="pcc-crumbs">' + (S.gitOpen?'Source Control':S.sfOpen?'Search':crumbs()) + '</div>' +
              '<div class="pcc-tree" id="pcc-tree">' + (S.gitOpen?gitHtml():S.sfOpen?sfHtml():treeHtml()) + '</div>' +
              '<div class="pcc-root" title="' + enc(S.root) + '">' + enc(S.root) + '</div>' +
            '</aside>' +
            '<div class="pcc-grip pcc-grip-v" id="pcc-gripv" role="separator" aria-label="Resize file tree"></div>' +
            '<section class="pcc-pane">' +
              '<div class="pcc-tabs" id="pcc-tabs">' + tabsHtml() + '</div>' +
              toolbarHtml() +
              (S.gitDiff?diffHtml():editorHtml()) +
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
      S.find.at = ta.selectionStart;
      d.scroll = ta.scrollTop;
      save();
    }

    function syncScroll(){
      const ta = $('#pcc-ta'), hl = $('#pcc-hl'), g = $('#pcc-gutter'), fm = $('#pcc-fm');
      if(!ta) return;
      for(const l of [hl, fm]) if(l){ l.scrollTop = ta.scrollTop; l.scrollLeft = ta.scrollLeft; }
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
      if(S.find.open) refind();
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

      /* The editor's shortcuts, on the whole screen — Ctrl+F must work from the tree or the find
       * box too, not only from inside the file. NOT from the terminal panel: there Ctrl+H is
       * backspace and Ctrl+F is forward-char, and taking them would break the shell. */
      const root = $('.pcc');
      if(root) root.addEventListener('keydown', (ev) => {
        if(ev.target.closest && ev.target.closest('#pcc-termhost')) return;
        const k = String(ev.key || '').toLowerCase(), mod = (ev.ctrlKey || ev.metaKey) && !ev.altKey;
        const can = doc() && !S.gitDiff;
        if(mod && k === 'f' && !ev.shiftKey){ if(can){ ev.preventDefault(); openFind(false); } }
        else if(mod && k === 'h' && ev.shiftKey){ ev.preventDefault(); openSearch(); }
        else if(mod && k === 'h'){ if(can){ ev.preventDefault(); openFind(true); } }
        else if(k === 'f3' && can){ ev.preventDefault(); if(S.find.open) nav(ev.shiftKey ? -1 : 1); else openFind(false); }
        else if(k === 'escape' && S.find.open && ev.target.id === 'pcc-ta'){ ev.preventDefault(); closeFind(); }
      });
      on('#pcc-find-b', 'click', () => openFind(false));
      wireFind();
      wireSearch();

      // Tree + breadcrumbs: ONE delegated listener, so a repaint cannot leave a dead button behind.
      const side = $('#pcc-side');
      if(side) side.addEventListener('click', (ev) => {
        const b = ev.target.closest && ev.target.closest('[data-dir],[data-file],[data-go],[data-sf]');
        if(!b) return;
        if(b.hasAttribute('data-sf')){ const [fi, hi] = b.getAttribute('data-sf').split(':').map(Number); return openHit(fi, hi); }
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
      on('#pcc-open-empty', 'click', () => { const b=$('#pcc-open-folder'); if(b) b.click(); });
      on('#pcc-open-folder', 'click', async()=>{
        const h=window.pcHost;
        if(h&&h.pickDirectory){
          const picked=await h.pickDirectory();if(!picked)return;
          await openHostFolder(picked);return;
        }
        /* Browser builds cannot invoke an OS folder picker. The server API already confines paths
         * to its configured workspace, so accept a workspace-relative directory and let /tree
         * validate it. Empty means the workspace root. */
        const current=S.cwd||'';
        const picked=await uiPrompt('Working directory (relative to the workspace root)',
                                    {value:current, ok:'Open folder'});
        if(picked===null)return;
        S.hostRoot='';S.gitOpen=false;S.gitDiff=null;
        await loadTree(String(picked).trim().replace(/^\/+|\/+$/g,''));save(true);
      });
      on('#pcc-fmt', 'click', formatDoc);
      on('#pcc-reload', 'click', reloadDoc);
      document.querySelectorAll('[data-code-view]').forEach(b=>b.addEventListener('click',()=>{
        const git=b.dataset.codeView==='git';
        if(b.dataset.codeView==='search') return openSearch();
        S.gitOpen=git; S.sfOpen=false;
        if(!git)cancelGitDiff();
        save(true);
        paint();
        if(git)loadGit();
      }));
      on('#pcc-term', 'click', () => { S.termOpen = !S.termOpen; save(); paint(); });

      document.querySelectorAll('[data-git-act]').forEach(b=>b.addEventListener('click',()=>{
        const a=b.dataset.gitAct, path=b.dataset.gitPath;
        const msg=a==='commit'?(($('#pcc-git-message')||{}).value||''):'';
        gitAct(a,path?[path]:[],msg);
      }));
      document.querySelectorAll('[data-git-diff]').forEach(b=>b.addEventListener('click',async()=>{
        await loadGitDiff(b.dataset.gitDiff);
      }));
      document.querySelectorAll('[data-git-restore]').forEach(b=>b.addEventListener('click',()=>{
        discardFile(b.dataset.gitRestore);
      }));
      on('#pcc-diff-close','click',()=>{cancelGitDiff();paint();});
      // ONE delegated listener over the whole patch — the rows are rebuilt by every repaint, and a
      // per-row binding leaves the ones drawn by the next paint dead.
      const diffBody=$('#pcc-diffbody');
      if(diffBody) diffBody.addEventListener('click',(ev)=>{
        const b=ev.target.closest&&ev.target.closest('[data-diff-line]');
        if(!b||!S.gitDiff) return;
        openDiffAt(S.gitDiff.path, Number(b.getAttribute('data-diff-line'))||1);
      });

      const ta = $('#pcc-ta');
      if(ta){
        ta.addEventListener('input', onInput);
        ta.addEventListener('keydown', onKey);
        ta.addEventListener('scroll', () => { syncScroll(); capture(); });
        // `select` and `click` as well as `keyup`: a mouse drag moves the caret without a key ever
        // being pressed, and that position is as much "where I was" as a typed one.
        ['keyup', 'click', 'select'].forEach(e => ta.addEventListener(e, () => { capture(); showPos(); }));
        restoreCaret();
        if(S.find.open) refind();
      }

      grips();
    }

    // ---- find / replace ------------------------------------------------------------------------

    /* THE MATCHES ARE A THIRD LAYER (#pcc-fm) between the colours and the textarea: the same text in
     * transparent ink with a <mark> round each match, so a highlight never disturbs the syntax
     * colouring, and it scrolls with the other two through syncScroll. What it draws is derived —
     * recomputed from `S.find` and the buffer on every repaint and keystroke, never kept in the DOM.
     *
     * EVERY EDIT GOES THROUGH setValue, with the textarea focused for the instant of the write. That
     * is what puts a Replace on the browser's own undo stack (Replace all is ONE insertText, so ONE
     * Ctrl+Z) — and it is not optional: execCommand writes into whatever has focus, which while
     * somebody is typing a replacement is the replace box, not the file. */
    const F = S.find;
    let _fr = { ranges: [], error: '', capped: false }, _fi = -1;
    const fOpts = () => ({ cs: F.cs, ww: F.ww, re: F.re });
    const OPTS = [['cs', 'Aa', 'Match case'], ['ww', 'ab', 'Whole word'], ['re', '.*', 'Regular expression']];
    const optsHtml = (o, attr) => OPTS.map(([k, t, l]) => '<button class="pcc-fo' + (o[k] ? ' on' : '') + '" ' + attr +
      '="' + k + '" title="' + l + '" aria-label="' + l + '" aria-pressed="' + !!o[k] + '">' + t + '</button>').join('');
    const oneLine = (t) => (t && t.indexOf('\n') < 0 && t.length < 200 ? t : '');

    function findBarHtml(){
      const inp = (id, ph, v) => '<input id="' + id + '" placeholder="' + ph + '" aria-label="' + ph +
        '" spellcheck="false" autocomplete="off" autocapitalize="off" value="' + enc(v) + '">';
      return '<div class="pcc-find" id="pcc-find" role="search">' +
        '<button class="pcc-fb" id="pcc-f-mode" title="Toggle replace (Ctrl+H)" aria-label="Toggle replace" aria-expanded="' +
          F.repl + '">' + (F.repl ? '▾' : '▸') + '</button><div class="pcc-frows">' +
        '<div class="pcc-frow">' + inp('pcc-f-q', 'Find', F.q) + optsHtml(F, 'data-fo') +
          '<span class="pcc-fn" id="pcc-f-n" aria-live="polite"></span>' +
          '<button class="pcc-fb" id="pcc-f-prev" title="Previous match (Shift+Enter)" aria-label="Previous match">↑</button>' +
          '<button class="pcc-fb" id="pcc-f-next" title="Next match (Enter)" aria-label="Next match">↓</button>' +
          '<button class="pcc-fb" id="pcc-f-x" title="Close (Esc)" aria-label="Close find">×</button></div>' +
        (F.repl ? '<div class="pcc-frow">' + inp('pcc-f-r', 'Replace', F.r) +
          '<button class="pcc-fb pcc-fw" id="pcc-f-r1" title="Replace this match (Enter)">Replace</button>' +
          '<button class="pcc-fb pcc-fw" id="pcc-f-ra" title="Replace every match (Ctrl+Alt+Enter)">All</button></div>' : '') +
        '</div></div>';
    }

    function refind(){
      const d = doc();
      _fr = F.open && d && typeof d.text === 'string' ? findAll(d.text, F.q, fOpts()) : { ranges: [], error: '', capped: false };
      _fi = pick(_fr.ranges, F.at, 1);
      paintMarks();
    }
    function paintMarks(){
      const fm = $('#pcc-fm'), n = $('#pcc-f-n'), d = doc();
      if(fm){
        let h = '', last = 0;
        if(F.open && d && _fr.ranges.length){
          _fr.ranges.forEach(([s, e], i) => {
            h += esc(d.text.slice(last, s)) + '<mark' + (i === _fi ? ' class="cur"' : '') + '>' + esc(d.text.slice(s, e)) + '</mark>';
            last = e;
          });
          h += esc(d.text.slice(last)) + '\n';
        }
        fm.innerHTML = h;
      }
      if(n){
        const none = !!F.q && !_fr.ranges.length;
        n.textContent = _fr.error || (!F.q ? '' : none ? 'No results'
          : (_fi + 1) + ' of ' + _fr.ranges.length + (_fr.capped ? '+' : ''));
        n.classList.toggle('err', !!_fr.error || none);
      }
      syncScroll();
    }
    /* Select a match and bring it into view. The <mark> is measured rather than the line counted:
     * it is exact in BOTH directions, and a match 300 columns into a long line is otherwise a
     * selection nobody can see. */
    function goMatch(i){
      const r = _fr.ranges[i], ta = $('#pcc-ta'), d = doc();
      if(!r || !d) return paintMarks();
      _fi = i; F.at = r[0]; d.sel = { s: r[0], e: r[1] };
      if(ta) try{ ta.setSelectionRange(r[0], r[1]); }catch(_){}
      paintMarks();
      const m = $('#pcc-fm mark.cur');
      if(ta && m){
        const t = m.offsetTop, l = m.offsetLeft;
        if(t < ta.scrollTop + 40 || t + m.offsetHeight > ta.scrollTop + ta.clientHeight - 20)
          ta.scrollTop = Math.max(0, t - ta.clientHeight / 3);
        if(l < ta.scrollLeft || l + m.offsetWidth > ta.scrollLeft + ta.clientWidth - 20)
          ta.scrollLeft = Math.max(0, l - ta.clientWidth / 3);
        d.scroll = ta.scrollTop;
        syncScroll();
      }
      showPos(); save();
    }
    /* From the current match, step and wrap; from anywhere else (the caret moved, the text changed)
     * go to the nearest match in that direction — never skip the one right after the caret. */
    function nav(dir){
      refind();
      const d = doc(), n = _fr.ranges.length, r = _fr.ranges[_fi];
      if(!n || !d) return;
      goMatch(r && d.sel && d.sel.s === r[0] && d.sel.e === r[1] ? step(_fi, n, dir) : pick(_fr.ranges, F.at, dir));
    }
    function openFind(repl){
      const d = doc(), ta = $('#pcc-ta');
      if(!d) return;
      // Prefill only from a selection somebody MADE: with focus in the find box the file's selection is
      // the current match, and reading it back would overwrite a regex with its own escaped result.
      // …and not when that selection IS the current match either (F3 in the file selects it with focus left there).
      const cur = _fr.ranges && _fr.ranges[_fi];
      const isMatch = ta && cur && ta.selectionStart === cur[0] && ta.selectionEnd === cur[1];
      if(ta && document.activeElement === ta && !isMatch){ const t = oneLine(ta.value.slice(ta.selectionStart, ta.selectionEnd)); if(t) F.q = F.re ? reEsc(t) : t; capture(); }
      if(!F.open || (repl && !F.repl) || !$('#pcc-find')){ F.open = true; F.repl = F.repl || !!repl; save(); paint(); }
      else{ const q = $('#pcc-f-q'); if(q) q.value = F.q; }
      refind();
      if(_fr.ranges.length) goMatch(_fi);
      const box = $(repl && F.q ? '#pcc-f-r' : '#pcc-f-q');
      if(box){ box.focus(); box.select(); }
    }
    function closeFind(){
      F.open = false; save(); paint();
      const ta = $('#pcc-ta');
      if(ta){ ta.focus({ preventScroll: true }); restoreCaret(); }
    }
    function edit(next, s){
      const ta = $('#pcc-ta'), back = document.activeElement;
      if(!ta) return;
      ta.focus({ preventScroll: true });
      setValue(next, s);
      if(back && back !== ta && back.focus) back.focus({ preventScroll: true });
    }
    // The first press on a match that is not selected SELECTS it, the way VS Code does: a replace
    // should never land somewhere the person has not been shown.
    function replaceOne(){
      const d = doc();
      refind();
      const r = _fr.ranges[_fi];
      if(!d || !r) return;
      if(!(d.sel && d.sel.s === r[0] && d.sel.e === r[1])) return goMatch(_fi);
      const rep = replacementAt(d.text, r[0], F.q, fOpts(), F.r);
      edit(d.text.slice(0, r[0]) + rep + d.text.slice(r[1]), r[0] + rep.length);
      refind();                                   // F.at is now just past the replacement → the NEXT match
      if(_fr.ranges.length) goMatch(_fi);
    }
    function replaceAllNow(){
      const d = doc();
      if(!d) return;
      const first = findAll(d.text, F.q, fOpts(), 1).ranges[0], r = replaceAll(d.text, F.q, fOpts(), F.r);
      if(r.error || !r.count) return status(r.error || 'Nothing to replace', r.error ? 'err' : 'warn');
      edit(r.text, first[0]);
      refind();
      status('Replaced ' + r.count + ' occurrence' + (r.count === 1 ? '' : 's'), 'ok');
    }
    function wireFind(){
      const bar = $('#pcc-find'), q = $('#pcc-f-q'), rb = $('#pcc-f-r');
      if(!bar || !q) return;
      const again = () => { save(); refind(); if(_fr.ranges.length) goMatch(_fi); };
      q.addEventListener('input', () => { F.q = q.value; again(); });
      if(rb) rb.addEventListener('input', () => { F.r = rb.value; save(); });
      bar.addEventListener('keydown', (ev) => {
        if(ev.key === 'Escape'){ ev.preventDefault(); ev.stopPropagation(); return closeFind(); }
        if(ev.key !== 'Enter' || ev.target.tagName !== 'INPUT') return;
        ev.preventDefault();
        if(ev.target === rb) return (ev.ctrlKey || ev.metaKey) && ev.altKey ? replaceAllNow() : replaceOne();
        nav(ev.shiftKey ? -1 : 1);
      });
      bar.addEventListener('click', (ev) => {
        const b = ev.target.closest && ev.target.closest('button');
        if(!b) return;
        const o = b.getAttribute('data-fo');
        if(o){ F[o] = !F[o]; b.classList.toggle('on', F[o]); b.setAttribute('aria-pressed', F[o]); return again(); }
        if(b.id === 'pcc-f-mode'){
          F.repl = !F.repl; save(); paint();
          const box = $(F.repl ? '#pcc-f-r' : '#pcc-f-q'); if(box) box.focus();
          return;
        }
        const fn = { 'pcc-f-prev': () => nav(-1), 'pcc-f-next': () => nav(1), 'pcc-f-x': closeFind,
                     'pcc-f-r1': replaceOne, 'pcc-f-ra': replaceAllNow }[b.id];
        if(fn) fn();
      });
    }

    // ---- search in files -------------------------------------------------------------------------

    /* THE SAME LISTING AND READING THE EXPLORER USES — pcHost.list/readText for a folder on this
     * computer, /api/code/tree + /file for the node's workspace — walked breadth-first. There is no
     * index to go stale; the price is a read per file, so the walk is BOUNDED (files, hits, size)
     * and every bound that bit is SAID in the summary: a search that silently stopped half way reads
     * as "it is not in the other half". An OPEN buffer is searched instead of its file on disk, so
     * unsaved edits are found and the offsets of a hit are offsets into what the tab holds.
     *
     * Replace-in-files is deliberately NOT offered: it would write files nobody has open through a
     * path with no undo stack. Open the hit and use Replace all, which Ctrl+Z can take back. */
    const SF_SKIP = /^(?:\.git|node_modules|__pycache__|\.mypy_cache|\.pytest_cache|\.cache|venv|venv-unified|\.venv|dist|build|\.gradle|\.idea)$/;
    const SF_BIN = /\.(?:png|jpe?g|gif|webp|bmp|ico|icns|pdf|zip|gz|tgz|bz2|xz|zst|7z|rar|tar|jar|apk|aab|class|so|o|a|dll|exe|bin|iso|img|dmg|woff2?|ttf|otf|eot|mp[34]|m4a|webm|mkv|mov|avi|ogg|opus|flac|wav|pyc|db|sqlite3?|gguf|safetensors|pt|onnx|npy)$/i;
    const SF_FILES = 4000, SF_HITS = 2000, SF_BYTES = 1024 * 1024;
    let _sfSeq = 0;
    const sfRel = (p) => (S.hostRoot && p.indexOf(S.hostRoot) === 0 ? p.slice(S.hostRoot.replace(/\/+$/, '').length + 1) : p);

    function sfResHtml(){
      const Q = S.sf;
      const note = Q.err || Q.note;
      return (note ? '<div class="pcc-sf-note' + (Q.err ? ' err' : '') + '">' + enc(note) + '</div>' : '') +
        Q.results.map((f, fi) => {
          const rel = sfRel(f.path), cut = rel.lastIndexOf('/');
          return '<div class="pcc-sf-file" title="' + enc(f.path) + '"><b>' + enc(rel.slice(cut + 1)) + '</b><small>' +
            enc(cut > 0 ? rel.slice(0, cut) : '') + '</small><em>' + f.hits.length + '</em></div>' +
            f.hits.map((h, hi) => '<button class="pcc-sf-hit" data-sf="' + fi + ':' + hi + '" title="Line ' + h.line +
              '"><span class="pcc-dn">' + h.line + '</span><code>' + enc(h.pre.replace(/^\s+/, '')) + '<mark>' +
              enc(h.mid.replace(/\n/g, '⏎')) + '</mark>' + enc(h.post) + '</code></button>').join('');
        }).join('');
    }
    function sfHtml(){
      return '<div class="pcc-sf"><div class="pcc-frow"><input id="pcc-sf-q" placeholder="Search in files" ' +
        'aria-label="Search in files" enterkeyhint="search" spellcheck="false" autocomplete="off" autocapitalize="off" value="' +
        enc(S.sf.q) + '">' + optsHtml(S.sf, 'data-sfo') + '</div><div id="pcc-sf-res">' + sfResHtml() + '</div></div>';
    }
    function sfPaint(){ const el = $('#pcc-sf-res'); if(el) el.innerHTML = sfResHtml(); }

    async function runSearch(){
      const Q = S.sf, seq = ++_sfSeq, o = { cs: Q.cs, ww: Q.ww, re: Q.re }, host = !!S.hostRoot;
      Object.assign(Q, { results: [], err: '', note: '', busy: false });
      if(!Q.q) return sfPaint();
      if(noFolder()){ Q.err = 'Open a folder to search its files'; return sfPaint(); }
      Q.err = findRe(Q.q, o).error;
      if(Q.err) return sfPaint();
      Q.busy = true; Q.note = 'Searching…'; sfPaint();
      const bufs = {};
      S.open.forEach(d => { if(typeof d.text === 'string' && !d.blob) bufs[d.host ? d.host.path : d.path] = d.text; });
      const files = [], dirs = [host ? S.hostRoot : ''];
      let skipped = 0, more = false, hits = 0, done = 0;
      while(dirs.length){
        if(files.length >= SF_FILES){ more = true; break; }
        const dir = dirs.shift();
        let t;
        try{ t = host ? await window.pcHost.list(dir) : await api('/tree?path=' + encodeURIComponent(dir)); }
        catch(_){ skipped++; continue; }
        if(seq !== _sfSeq) return;
        if(t.truncated) more = true;
        for(const e of t.entries || []){
          const p = e.path || (t.path ? t.path + '/' + e.name : e.name);
          if(e.dir){ if(!SF_SKIP.test(e.name) && !e.link) dirs.push(p); }
          else if(SF_BIN.test(e.name) || e.size > SF_BYTES || e.broken) skipped++;
          else files.push(p);
        }
      }
      let next = 0;
      // The query as it was when the search STARTED — the box can change under a running search.
      const qq = Q.q;
      const lane = async () => {
        while(next < files.length && hits < SF_HITS){
          const p = files[next++];
          let text = bufs[p];
          try{
            if(typeof text !== 'string') text = (host ? await window.PCHostFiles.readText(p) : await api('/file?path=' + encodeURIComponent(p))).text;
          }catch(_){ skipped++; continue; }
          if(seq !== _sfSeq) return;
          done++;
          if(typeof text !== 'string' || text.indexOf('\0') >= 0){ skipped++; continue; }
          if(SF_HITS - hits <= 0) break;                    // another lane filled it while this one read
          const g = grep(text, qq, o, SF_HITS - hits);
          if(g.hits.length){ hits += g.hits.length; Q.results.push({ path: p, hits: g.hits }); }
          if(done % 40 === 0){ Q.note = 'Searching… ' + done + ' of ' + files.length + ' files'; sfPaint(); }
        }
      };
      await Promise.all([1, 2, 3, 4, 5, 6].map(lane));
      if(seq !== _sfSeq) return;
      Q.results.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
      const pl = (n, w) => n + ' ' + w + (n === 1 ? '' : 's');
      Q.busy = false;
      Q.note = (hits ? pl(hits, 'result') + ' in ' + pl(Q.results.length, 'file') : 'No results') + ' · ' + pl(done, 'file') + ' searched' +
        (skipped ? ', ' + skipped + ' skipped (binary, over 1 MB or unreadable)' : '') +
        (hits >= SF_HITS ? ' — stopped at ' + SF_HITS + ' results' : '') +
        (more ? ' — this folder has more files than one search reads' : '');
      sfPaint();
    }
    function openSearch(){
      const ta = $('#pcc-ta');
      if(ta && document.activeElement === ta){ const t = oneLine(ta.value.slice(ta.selectionStart, ta.selectionEnd)); if(t) S.sf.q = S.sf.re ? reEsc(t) : t; capture(); }
      S.sfOpen = true; S.gitOpen = false; cancelGitDiff(); save(true); paint();
      const q = $('#pcc-sf-q'); if(q){ q.focus(); q.select(); }
    }
    function wireSearch(){
      const q = $('#pcc-sf-q');
      if(!q) return;
      q.addEventListener('input', () => { S.sf.q = q.value; save(); });
      q.addEventListener('keydown', (ev) => { if(ev.key === 'Enter'){ ev.preventDefault(); runSearch(); } });
      document.querySelectorAll('[data-sfo]').forEach(b => b.addEventListener('click', () => {
        const k = b.getAttribute('data-sfo');
        S.sf[k] = !S.sf[k]; b.classList.toggle('on', S.sf[k]); b.setAttribute('aria-pressed', S.sf[k]); save();
        if(S.sf.q) runSearch();
      }));
    }
    /* A hit opens its file WITH THE MATCH SELECTED and scrolled to — the offsets are into the text
     * that was searched, which for an open tab is the tab itself. */
    async function openHit(fi, hi){
      const f = S.sf.results[fi], h = f && f.hits[hi];
      if(!h || !await openPath(f.path)) return;
      const d = doc();
      await hydrate(d);
      const len = (d.text || '').length;
      d.sel = { s: Math.min(h.s, len), e: Math.min(h.e, len) };
      F.at = d.sel.s;
      if(inView()) paint();
      restoreCaret();
      scrollToLine(h.line - 1);
      const ta = $('#pcc-ta');
      if(ta){ ta.focus({ preventScroll: true }); try{ ta.setSelectionRange(d.sel.s, d.sel.e); }catch(_){} }
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
        if(!_incoming) restore();  // ← never overwrite a file Files just handed us with an old tab
        _incoming = false;
        paint();                   // spinner, from the same paint path as everything else
        /* AND THE LAST PAINT ALWAYS HAPPENS. Even with the awaits bounded above, anything that
         * throws between the spinner and the repaint leaves the spinner standing — the screen then
         * says "loading" about something that already gave up. */
        try{
          await loadConfig();
          if(!S.gate && (!window.pcHost || !window.pcHost.pickDirectory || S.hostRoot)) await loadTree(S.cwd);
          /* A monitor handoff restores the selected activity before this renderer has queried Git.
           * Hydrate that destination too; otherwise the persisted Source Control view misleadingly
           * says to choose a directory even though its local workspace was restored successfully. */
          if(!S.gate&&S.gitOpen)await loadGit();
        }catch(e){
          S.ready = true;
          if(!S.gate) S.gate = 'Could not open this node: ' + ((e && e.message) || e);
        }
      }
      paint();
    }

    window.PCCode = {
      render,
      // For tests and for anything that wants to open a file from elsewhere in the app.
      open: openPath,
      openBlob,
      openHostFile,
      openHostFolder,
      _state: S,
      _missingPathError: missingPathError,
      _loadGitDiff: loadGitDiff,
      _cancelGitDiff: cancelGitDiff,
      _openDiffAt: openDiffAt,
      _openFind: openFind,
      _openSearch: openSearch,
      _runSearch: runSearch,
      _discardFile: discardFile,
      _gitFilePath: gitFilePath,
      _gotoLine: gotoLine,
      _scrollToLine: scrollToLine,
      _parseDiff: parseDiff,
      _diffCounts: diffCounts,
      _discardPlan: discardPlan,
      _diffRowsHtml: diffRowsHtml,
      _highlight: highlight,
      _langOf: langOf,
    };
  }
  init();
})();
