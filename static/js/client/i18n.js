/* THE UI IN ANOTHER LANGUAGE — Arabic (RTL) and Japanese, with English the default.
 *
 * WHY THIS IS A RUNTIME LAYER AND NOT t() AT EVERY CALL SITE. Every user-facing string in this
 * client lives inside an HTML template literal, across ~57,000 lines of JS. Wrapping them at the
 * source means a mechanical rewrite of code INSIDE template literals — and a regex that gets one
 * match wrong there does not produce a mistranslation, it produces a client that does not boot. The
 * catalogue is the same either way; only the moment of substitution differs. So the substitution
 * happens against the DOM, which also means a screen written next month is translated with no
 * further work, and a contributor never has to remember to wrap anything.
 *
 * ENGLISH SOURCE STRINGS ARE THE KEYS, gettext-style. Three things fall out of that and all of them
 * matter: English needs no catalogue and no code changes (it is the identity mapping), an
 * untranslated string falls back to the English the developer wrote rather than to a bare key like
 * `nav.settings`, and a half-finished catalogue degrades into a half-English screen instead of a
 * screen full of dotted identifiers.
 *
 * WHAT KEEPS IT OFF USER CONTENT. The filter is CATALOGUE MEMBERSHIP: a text node is replaced only
 * if its exact trimmed text is a string this app ships. A post is not in the catalogue, so a post is
 * not touched — no allow-list of containers to keep in step with the renderers, which is the part
 * that would rot. The container skip-list below is the second guard, for the case membership cannot
 * see: somebody writing a note whose entire body is the word "Save".
 *
 * ENGLISH COSTS NOTHING. At 'en' no catalogue is fetched, no observer is installed and no node is
 * walked — `setLang('en')` tears the whole thing down. That is deliberate: this is the default for
 * almost every user, and an i18n layer that taxes the default is a performance bug wearing a
 * feature's clothes.
 */
(function(){
  'use strict';

  var LOCALES = {
    en: { name: 'English',  dir: 'ltr' },
    ar: { name: 'العربية',  dir: 'rtl' },
    ja: { name: '日本語',    dir: 'ltr' },
  };
  var KEY = 'pc_lang';

  var cur = 'en';         // the active locale
  var cat = null;         // English source string → translation, for `cur`
  var obs = null;         // the MutationObserver, only while a non-English locale is on
  var queued = null;      // rAF handle: mutations are batched, never handled one at a time

  /* Nodes whose TEXT is somebody's, not ours. Membership already covers the ordinary case; this is
   * for the collision — a note, a DM or a profile field whose whole content happens to equal a UI
   * string. `contenteditable` and the form fields are here for a different reason: rewriting text a
   * user is in the middle of typing is a bug in any language. */
  var SKIP = ['.txt', '.note .body', '.dm-msg', '.msg-body', '.mail-body', '.article-body',
              '[contenteditable]', '[data-user-text]', 'textarea', 'input', 'code', 'pre',
              'script', 'style', 'noscript'];
  var SKIP_SEL = SKIP.join(',');

  /* ATTRIBUTES ARE SKIPPED BY A DIFFERENT RULE, and conflating the two silently costs most of the
   * feature. A field is on the list above to protect what somebody TYPED — but its `placeholder` is
   * a string this app shipped, and nearly every input and textarea here has one: the composer, the
   * search box, every settings field. Reusing the text rule left all of them in English while
   * everything around them translated, which reads as a half-finished translation rather than as a
   * bug. So attributes skip only the containers that hold a PERSON's words. */
  var SKIP_ATTR_SEL = ['.txt', '.note .body', '.dm-msg', '.msg-body', '.mail-body',
                       '.article-body', '[contenteditable]', '[data-user-text]'].join(',');

  /* Attributes that are read by a person. `value` is deliberately absent: on an <input> it is user
   * data, and the one place it is a label (a submit button) is vanishingly rare here. */
  var ATTRS = ['placeholder', 'title', 'aria-label', 'alt', 'data-label'];

  function stored(){
    try{ return localStorage.getItem(KEY) || ''; }catch(e){ return ''; }
  }

  /* The catalogue lookup, and the only thing other code ever needs. Exported as PCI18N.t and as a
   * bare `t` for call sites that DO want to translate at the source — a string built by
   * concatenation never reaches the DOM as one text node, so those have to ask. */
  function t(s){
    if(!cat || typeof s !== 'string') return s;
    var hit = cat[s];
    if(hit) return hit;
    // A trailing-space / leading-space variant is the same string to a reader. Cheap second try
    // rather than a catalogue that has to carry every whitespace variant of every phrase.
    var trimmed = s.trim();
    if(trimmed !== s && cat[trimmed]) return s.replace(trimmed, cat[trimmed]);
    return s;
  }

  function skipped(el, sel){
    try{ return !!(el && el.closest && el.closest(sel || SKIP_SEL)); }catch(e){ return false; }
  }

  /* One pass over a subtree. A TreeWalker rather than querySelectorAll('*') + childNodes: the
   * feed holds thousands of nodes and this runs on every mutation batch, so the walk has to visit
   * text nodes directly and reject whole subtrees at the filter rather than after descending. */
  function walk(root){
    if(!cat || !root) return;
    if(root.nodeType === 3){ subText(root); return; }
    if(root.nodeType !== 1) return;

    var w;
    try{
      w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
        acceptNode: function(n){
          if(!n.nodeValue || !n.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        }
      });
    }catch(e){ return; }

    var batch = [], n;
    while((n = w.nextNode())) batch.push(n);
    for(var i = 0; i < batch.length; i++) subText(batch[i]);

    // Attributes, on the root and everything under it.
    subAttrs(root);
    var els;
    try{ els = root.querySelectorAll('[placeholder],[title],[aria-label],[alt],[data-label]'); }
    catch(e){ els = []; }
    for(var j = 0; j < els.length; j++) subAttrs(els[j]);
  }

  function subText(node){
    var raw = node.nodeValue;
    var key = raw.trim();
    if(!key || !cat[key]) return;                 // membership IS the filter — see the header
    if(skipped(node.parentNode)) return;
    // Preserve the surrounding whitespace: markup relies on it for spacing between inline elements,
    // and collapsing it here moves things on screen for reasons nobody would ever connect to
    // translation.
    node.nodeValue = raw.replace(key, cat[key]);
  }

  function subAttrs(el){
    if(!el || el.nodeType !== 1 || !el.getAttribute) return;
    if(skipped(el, SKIP_ATTR_SEL)) return;
    for(var i = 0; i < ATTRS.length; i++){
      var a = ATTRS[i], v;
      try{ v = el.getAttribute(a); }catch(e){ continue; }
      if(!v) continue;
      var k = v.trim();
      if(!cat[k]) continue;
      try{ el.setAttribute(a, cat[k]); }catch(e){}
    }
  }

  /* Mutations are batched into one rAF. The feed rebuilds itself in bursts — a timeline draw is
   * hundreds of records in one tick — and translating per-record would walk the same subtree
   * repeatedly for no benefit. */
  function onMutations(recs){
    if(!cat) return;
    var pending = [];
    for(var i = 0; i < recs.length; i++){
      var r = recs[i];
      if(r.type === 'childList'){
        for(var j = 0; j < r.addedNodes.length; j++) pending.push(r.addedNodes[j]);
      }else if(r.type === 'attributes' && r.target){
        subAttrs(r.target);
      }
    }
    if(!pending.length) return;
    if(queued) return;
    queued = requestAnimationFrame(function(){
      queued = null;
      for(var k = 0; k < pending.length; k++) walk(pending[k]);
    });
  }

  function observe(){
    if(obs || !document.body) return;
    try{
      obs = new MutationObserver(onMutations);
      obs.observe(document.body, { childList: true, subtree: true,
                                   attributes: true, attributeFilter: ATTRS });
    }catch(e){ obs = null; }
  }
  function unobserve(){
    if(!obs) return;
    try{ obs.disconnect(); }catch(e){}
    obs = null;
    if(queued){ try{ cancelAnimationFrame(queued); }catch(e){} queued = null; }
  }

  /* The catalogue's home. `__PC_API_BASE__` being DEFINED means a bundled build (desktop/APK), where
   * the file sits beside the other client assets rather than on an instance — and its VALUE being
   * empty means there is no server to ask, which is exactly when the bundled copy is the only copy.
   * Getting this wrong is the bug that has bitten every asset in this client at least once. */
  function catalogueUrl(lang){
    var ver = '';
    try{ ver = window.__VER ? ('?v=' + window.__VER) : ''; }catch(e){}
    return '/static/i18n/' + lang + '.json' + ver;
  }

  function load(lang){
    return fetch(catalogueUrl(lang), { cache: 'force-cache' })
      .then(function(r){ if(!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function(j){ return (j && typeof j === 'object') ? (j.strings || j) : null; });
  }

  /* Direction and language on <html>, which is what makes RTL work at all: the CSS logical
   * properties this client uses resolve against it, and so do the browser's own text runs, caret
   * movement and scrollbar side. `lang` is not decoration either — it picks the font the system
   * falls back to for CJK, where the same codepoint is drawn differently in Japanese and Chinese. */
  var RTL_CSS_ID = 'pc-rtl-css';

  /* The RTL corrections are their OWN stylesheet, linked only while the interface is right-to-left.
   * Everything else is already direction-agnostic (the physical properties were converted to
   * logical ones, which resolve identically in LTR), so this file is only the short list of things
   * that must not flip — technical identifiers, code, directional arrows. Keeping it out of the
   * main stylesheet is the same rule as the rest of this module: the default locale pays nothing. */
  function rtlSheet(on){
    try{
      var link = document.getElementById(RTL_CSS_ID);
      if(on && !link){
        link = document.createElement('link');
        link.id = RTL_CSS_ID;
        link.rel = 'stylesheet';
        link.href = '/static/css/rtl.css' + (window.__VER ? ('?v=' + window.__VER) : '');
        (document.head || document.documentElement).appendChild(link);
      }else if(!on && link && link.parentNode){
        link.parentNode.removeChild(link);
      }
    }catch(e){}
  }

  function applyDir(lang){
    var L = LOCALES[lang] || LOCALES.en;
    try{
      document.documentElement.setAttribute('lang', lang);
      document.documentElement.setAttribute('dir', L.dir);
      // A hook for the few rules that cannot be expressed as logical properties (a background
      // gradient's angle, an icon that must not mirror).
      document.documentElement.classList.toggle('rtl', L.dir === 'rtl');
    }catch(e){}
    rtlSheet(L.dir === 'rtl');
  }

  function setLang(lang){
    if(!LOCALES[lang]) lang = 'en';
    cur = lang;
    try{ localStorage.setItem(KEY, lang); }catch(e){}
    applyDir(lang);

    if(lang === 'en'){
      // Nothing to translate, and nothing left running. A reload is NOT required to go back to
      // English — but it IS required to leave it, because switching away can only translate what is
      // on screen now, and everything already rendered in English stays English until redrawn.
      cat = null;
      unobserve();
      return Promise.resolve(true);
    }

    return load(lang).then(function(strings){
      if(!strings) throw new Error('empty catalogue');
      cat = strings;
      walk(document.body);
      observe();
      return true;
    }).catch(function(e){
      // A catalogue that will not load must leave the app in English rather than half-applied.
      try{ console.warn('[i18n] could not load', lang, e); }catch(e2){}
      cat = null; unobserve(); applyDir('en'); cur = 'en';
      return false;
    });
  }

  function boot(){
    var lang = stored();
    if(!lang || lang === 'en'){ applyDir('en'); return; }
    setLang(lang);
  }

  window.PCI18N = {
    t: t,
    locales: LOCALES,
    get lang(){ return cur; },
    set: setLang,
    // For a caller that built a string by concatenation, which never reaches the DOM as one node.
    refresh: function(root){ walk(root || document.body); },
  };
  window.t = t;

  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
