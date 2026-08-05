/* URL tracking removal — strip the surveillance off links before you post them.
 *
 * DELIBERATELY DOM-free, dependency-free and OFFLINE: it takes a string and returns a string, so
 * tests/test_url_clean.py runs THIS file under node, and the composer can clean a draft with no
 * server, no relay and no network round trip. That last part is the whole point — the alternative
 * design (ask the node's LLM which parameters look like trackers) would put a model in charge of
 * REWRITING a URL, and a model that guesses wrong produces a link that 404s or silently points
 * somewhere else. A tracking parameter is a known finite list, not a judgement call, so this is a
 * table.
 *
 * What it does, in order, per link:
 *   1. UNWRAP a click-wrapper that carries the real URL in a parameter (google /url?q=,
 *      l.facebook.com/l.php?u=, Outlook safelinks, …) and undo google/ampproject AMP prefixes.
 *      Recursive, bounded — wrappers nest (a safelink around a google /url around the article).
 *   2. DROP known tracking query parameters: a global list (utm_*, fbclid, gclid, …) plus
 *      per-host lists for parameters that are only tracking ON that host.
 *   3. A couple of PATH rules where the tracker isn't in the query at all (Amazon's /ref=… ).
 *
 * What it deliberately does NOT do:
 *   - resolve short links (bit.ly, t.co). That needs a network fetch, which tells the tracker you
 *     were here — from the server, on your behalf — before you have even posted the link.
 *   - strip anything it does not have a rule for. An unknown parameter is assumed load-bearing.
 *     `nytimes.com/…?unlocked_article_code=…` IS the gift link; `?si=` on Spotify is not. Guessing
 *     from the shape of the name ("looks random, must be tracking") breaks the first kind.
 *   - touch a link it did not change: if no rule fires, the ORIGINAL substring is returned byte for
 *     byte, never a re-serialized one. Round-tripping through URL() alone rewrites escaping and
 *     case, which would show up as a diff on every link in the post and make the feature look like
 *     it mangles things.
 */
(function(root){
  'use strict';

  // ── Global tracking parameters ───────────────────────────────────────────────────────────────
  // Matched case-insensitively — `UTM_Source` and `WT.mc_id` both occur in the wild. Everything
  // here is analytics/attribution on EVERY host; anything that is only a tracker on one site goes
  // in HOST_PARAMS below, so a site that happens to use the same name for real content is safe.
  const PARAMS = new Set([
    // click ids
    'fbclid','gclid','gclsrc','gbraid','wbraid','dclid','msclkid','twclid','yclid','ttclid','epik',
    'igshid','igsh','mibextid','li_fat_id','irclickid','irgwc','awc','rb_clickid','ysclid','yadclid',
    'srsltid','gad_source','gad_campaignid','_branch_match_id','_branch_referrer','wickedid','otc',
    // campaign / email
    'mkt_tok','mc_cid','mc_eid','ml_subscriber','ml_subscriber_hash','vero_conv','vero_id',
    'oly_anon_id','oly_enc_id','_openstat','xtor','cmpid','ncid','icid','s_cid','sc_campaign',
    'sc_channel','sc_content','sc_medium','sc_outcome','sc_geo','sc_country','soc_src','soc_trk',
    'elq','elqtrack','elqtrackid','elqaid','elqat','elqcampaignid','hsctatracking',
    '__hssc','__hstc','__hsfp','_hsenc','_hsmi',
    // page-level analytics beacons
    '_ga','_gl','wt.mc_id','wt_mc','wtrid','guccounter','guce_referrer','guce_referrer_sig',
    'fb_action_ids','fb_action_types','fb_source','fb_ref','fbc','fbp',
  ]);
  // Prefix rules — whole FAMILIES of parameters, all of them analytics.
  //   utm_ Urchin/GA · mtm_/pk_/piwik_/matomo_ Matomo · hsa_ HubSpot ads · ns_ Comscore ·
  //   at_custom BBC · pd_rd_/pf_rd_ Amazon widget attribution · __cft__/__tn__ Facebook.
  const PREFIXES = ['utm_','mtm_','pk_','piwik_','matomo_','hsa_','ns_','at_custom','oly_',
                    'pd_rd_','pf_rd_','__cft__','__tn__','vero_'];

  // ── Per-host tracking parameters ─────────────────────────────────────────────────────────────
  // Keys are registrable hosts WITHOUT a leading `www.`; a rule applies to the host and every
  // subdomain of it (`m.youtube.com` matches `youtube.com`). These names are ONLY safe to drop
  // here — `si` is a share token on YouTube and a session id somewhere else, `source` is analytics
  // on Medium and a real query parameter on plenty of sites.
  const HOST_PARAMS = {
    'youtube.com':      ['si','pp','feature','kw','ab_channel','app'],
    'youtu.be':         ['si','pp','feature'],
    'spotify.com':      ['si','nd','_branch_match_id'],
    'twitter.com':      ['s','t','cxt','ref_src','ref_url','src'],
    'x.com':            ['s','t','cxt','ref_src','ref_url','src'],
    'instagram.com':    ['igshid','igsh'],
    'facebook.com':     ['ref','fref','refsrc','hc_ref','eav','dti','comment_tracking',
                         'action_history','tracking','referral_code','referral_story_type',
                         'video_source','ls_ref','pageid','ftentidentifier','padding','eid','rdid'],
    'reddit.com':       ['share_id','correlation_id','ref','ref_source','rdt','chainedposts',
                         'post_fullname','rdt_cid'],
    'tiktok.com':       ['is_from_webapp','sender_device','web_id','_r','_t','refer','checksum'],
    'linkedin.com':     ['trk','trackingid','originalsubdomain','lipi','licu','refid','midtoken',
                         'midsig','eid'],
    'medium.com':       ['source','sk','gi'],
    'substack.com':     ['r','showwelcome','triedredirect','post_id','publication_id','isfreemail'],
    'nytimes.com':      ['smid','smtyp','partner','referringSource'],  // NOT unlocked_article_code
    'bbc.co.uk':        ['at_medium','at_campaign','at_link_type','at_bbc_team','xtor'],
    'bbc.com':          ['at_medium','at_campaign','at_link_type','at_bbc_team','xtor'],
    'google.com':       ['ei','ved','usg','sa','oq','sclient','client','gs_lcp','gs_lp','sxsrf',
                         'uact','bih','biw','source','sourceid','aqs','ie','gs_ssp','sca_esv'],
    'bing.com':         ['qs','form','sp','pq','sc','cvid','ghc','ck','lq','sk'],
    'duckduckgo.com':   ['ia','iax','atb'],
    'amazon.com':       ['_encoding','qid','sr','sprefix','crid','dib','dib_tag','content-id','th',
                         'linkcode','creative','creativeasin','camp','smid','tag','ref','ref_'],
    'ebay.com':         ['_trkparms','_trksid','mkevt','mkcid','mkrid','campid','toolid','customid',
                         'siteid','norover','mkscid'],
    'aliexpress.com':   ['spm','scm','scm_id','algo_pvid','algo_expid','btsid','ws_ab_test',
                         'terminal_id','pdp_npi','curpageloguid','_randl_currency','_randl_shipto',
                         'aff_platform','aff_trace_key','aff_fcid','aff_fsk','aff_request_id'],
    'etsy.com':         ['click_key','click_sum','ref','frs','sts','organic_search_click'],
    'imdb.com':         ['ref_'],
    'wish.com':         ['share'],
    'stackoverflow.com':['r'],
  };
  // Amazon and Google run on ~20 country domains each and behave the same on all of them. These are
  // ENUMERATED rather than matched with /^amazon\./ or /google\./, because a pattern like that also
  // fires on `amazon.example.com` and `google.com.evil.com` — an unrelated host whose first label
  // happens to be the brand — and would then apply Amazon's path rewrite to somebody else's URL.
  const AMAZON_HOSTS = ['amazon.com','amazon.co.uk','amazon.de','amazon.ca','amazon.fr','amazon.it',
    'amazon.es','amazon.co.jp','amazon.com.au','amazon.in','amazon.com.br','amazon.com.mx',
    'amazon.nl','amazon.se','amazon.pl','amzn.to','amzn.eu'];
  const GOOGLE_HOSTS = ['google.com','google.co.uk','google.ca','google.de','google.fr','google.co.jp',
    'google.com.au','google.co.in','google.it','google.es','google.nl','google.pl','google.com.br'];
  AMAZON_HOSTS.forEach(h => { HOST_PARAMS[h] = HOST_PARAMS['amazon.com']; });
  GOOGLE_HOSTS.forEach(h => { HOST_PARAMS[h] = HOST_PARAMS['google.com']; });
  ['ebay.co.uk','ebay.de','ebay.com.au','ebay.ca','ebay.fr','ebay.it','ebay.es'].forEach(h => { HOST_PARAMS[h] = HOST_PARAMS['ebay.com']; });
  HOST_PARAMS['music.youtube.com'] = HOST_PARAMS['youtube.com'];
  HOST_PARAMS['open.spotify.com']  = HOST_PARAMS['spotify.com'];

  // ── Click wrappers ───────────────────────────────────────────────────────────────────────────
  // host (+ optional path prefix) → the parameter holding the destination. The wrapper's OWN
  // parameters are the tracking; the destination inside is what the person meant to share.
  const REDIRECTORS = [
    { host:'youtube.com',           path:'/redirect',   param:['q'] },
    { host:'l.facebook.com',        path:'/l.php',      param:['u'] },
    { host:'lm.facebook.com',       path:'/l.php',      param:['u'] },
    { host:'m.facebook.com',        path:'/l.php',      param:['u'] },
    { host:'l.instagram.com',       path:'/',           param:['u'] },
    { host:'l.messenger.com',       path:'/l.php',      param:['u'] },
    { host:'out.reddit.com',        path:'/',           param:['url'] },
    { host:'t.umblr.com',           path:'/redirect',   param:['z'] },
    { host:'steamcommunity.com',    path:'/linkfilter', param:['url'] },
    { host:'vk.com',                path:'/away.php',   param:['to'] },
    { host:'away.vk.com',           path:'/away.php',   param:['to'] },
    { host:'slack-redir.net',       path:'/link',       param:['url'] },
    { host:'linkedin.com',          path:'/redir/redirect', param:['url'] },
    { host:'getpocket.com',         path:'/redirect',   param:['url'] },
    { host:'href.li',               path:'/',           param:null },   // href.li/?<the whole url>
  ];
  // /url?q= exists on every google country domain, same as the search parameters above.
  GOOGLE_HOSTS.forEach(h => REDIRECTORS.push({ host:h, path:'/url', param:['q','url'] }));

  const _lc = s => (s||'').toLowerCase();

  /** The host key a rule table is looked up under: `m.youtube.com` → `youtube.com`. Walks the
   *  labels rather than guessing a public suffix, so `bbc.co.uk` and `amazon.com.au` both work. */
  function _hostKeys(host){
    const h = _lc(host).replace(/^www\./,'');
    const out = [h], parts = h.split('.');
    for(let i=1; i<parts.length-1; i++) out.push(parts.slice(i).join('.'));
    return out;
  }

  function _isTracker(name, hostList){
    const k = _lc(name);
    if(PARAMS.has(k)) return true;
    for(const p of PREFIXES) if(k.startsWith(p)) return true;
    return hostList.indexOf(k) !== -1;
  }

  /** Outlook / Proofpoint / AMP wrap the URL in a shape URL() alone can't reach. */
  function _wrappedSpecial(raw, u, hostKeys){
    const host = _lc(u.hostname);
    // …safelinks.protection.outlook.com/?url=<encoded>&data=…
    if(/(^|\.)safelinks\.protection\.outlook\.com$/.test(host)) return u.searchParams.get('url');
    // urldefense.com/v3/__<url>__;<base64>!!…  — the destination sits between the __ markers.
    if(/(^|\.)urldefense\.(com|proofpoint\.com)$/.test(host)){
      const m = raw.match(/__(https?:\/\/.+?)__;/);
      if(m) return m[1];
    }
    // AMP: google.<tld>/amp/s/<host/path> and <pub>.cdn.ampproject.org/v/s/<host/path>. The `s`
    // means the destination's origin is https; without it, http. The google host is checked against
    // the enumerated list, NOT a /google\./ pattern — see the note on GOOGLE_HOSTS.
    let m;
    if(hostKeys.some(h => GOOGLE_HOSTS.indexOf(h) !== -1)){
      m = raw.match(/^https?:\/\/[^/]+\/amp\/(s\/)?(.+)$/i);
      if(m) return (m[1] ? 'https://' : 'http://') + m[2];
    }
    m = raw.match(/^https?:\/\/[^/]*\.cdn\.ampproject\.org\/[vc]\/(s\/)?(.+)$/i);
    if(m) return (m[1] ? 'https://' : 'http://') + m[2];
    return null;
  }

  /** The destination inside a click wrapper, or null.
   *  Every path out of here is validated to be an http(s) URL: a wrapper whose parameter holds a
   *  relative path, a `javascript:` payload or plain garbage must leave the link ALONE, not replace
   *  the user's URL with that string. */
  function _unwrap(raw, u, hostKeys){
    const ok = v => (typeof v === 'string' && /^https?:\/\//i.test(v)) ? v : null;
    const special = _wrappedSpecial(raw, u, hostKeys);
    if(special) return ok(special);
    const host = _lc(u.hostname).replace(/^www\./,'');
    for(const r of REDIRECTORS){
      if(host !== r.host && !host.endsWith('.'+r.host)) continue;
      if(r.path !== '/' && u.pathname.indexOf(r.path) !== 0) continue;
      if(!r.param){   // href.li puts the destination in the query with no key at all
        const q = u.search.replace(/^\?/,'');
        if(!/^https?(:|%3a)/i.test(q)) return null;
        let dec = q; try { dec = decodeURIComponent(q); } catch(_){}   // a stray % is not a reason to throw
        return ok(dec);
      }
      for(const p of r.param){
        const v = ok(u.searchParams.get(p));
        if(v) return v;
      }
    }
    return null;
  }

  /** Path-level trackers — the ones that aren't in the query at all. */
  function _cleanPath(hostKeys, path){
    // Amazon: /Some-Product-Title/dp/B0XXXXXXXX/ref=sr_1_3?… — the /ref= segment is the referrer
    // beacon and the human-readable title slug is optional. /dp/<ASIN> always resolves.
    if(hostKeys.some(h => AMAZON_HOSTS.indexOf(h) !== -1)){
      const m = path.match(/\/(?:dp|gp\/product)\/([A-Z0-9]{10})(?:[/?]|$)/i);
      if(m) return '/dp/' + m[1];
      return path.replace(/\/ref=[^/]*/g, '');
    }
    if(hostKeys.indexOf('imdb.com') !== -1) return path.replace(/\/\?ref_=[^/]*$/, '/');
    return path;
  }

  /** A fragment that is nothing but a tracker (`#Echobox=1699…`, `#utm_source=…`) is dropped;
   *  anything else — an anchor, a scroll-to-text, an SPA route — is left completely alone.
   *  Deliberately checks the GLOBAL list only, never the host lists: those names were chosen as
   *  query parameters on that site, and `#s=…` or `#t=…` in a fragment is far more likely to be an
   *  application's own routing state than the tracker of the same name. */
  function _cleanHash(hash){
    if(!hash || hash.length < 2) return hash;
    const m = hash.slice(1).match(/^([A-Za-z0-9_.\-]+)=[^&]*$/);
    if(!m) return hash;
    return (_lc(m[1]) === 'echobox' || _isTracker(m[1], [])) ? '' : hash;
  }

  /**
   * Clean one URL. Returns the input UNCHANGED (same string object semantics: byte-identical) when
   * no rule fires or it isn't an http(s) URL we can parse.
   */
  function clean(url, _depth){
    const depth = _depth || 0;
    if(typeof url !== 'string' || !/^https?:\/\//i.test(url)) return url;
    // EVERYTHING below is inside the catch, not just the parse. This runs on the post path with the
    // setting on, so a throw here — a stray `%` reaching decodeURIComponent, a host URL() dislikes —
    // would not mangle a link, it would stop the post from being published at all. Returning the
    // original URL is always a safe answer.
    try {
      const u = new URL(url);
      const hostKeys = _hostKeys(u.hostname);

      if(depth < 4){
        const inner = _unwrap(url, u, hostKeys);
        // Guard against a wrapper that unwraps to itself (a malformed ?u=<same url>), which would
        // otherwise recurse to the depth cap on every link in every draft.
        if(inner && inner !== url) return clean(inner, depth + 1);
      }

      let hostList = [];
      for(const k of hostKeys) if(HOST_PARAMS[k]) hostList = hostList.concat(HOST_PARAMS[k].map(_lc));

      // Filter the RAW query text rather than URLSearchParams, so surviving parameters keep their
      // exact original escaping (`%20` does not become `+`, `%7E` does not become `~`).
      const rawQ = u.search.replace(/^\?/,'');
      const parts = rawQ ? rawQ.split('&') : [];
      const kept = parts.filter(p => {
        if(!p) return false;
        const name = p.split('=')[0].replace(/\+/g,' ');
        let dec = name; try { dec = decodeURIComponent(name); } catch(_){}
        return !_isTracker(dec, hostList);
      });

      const path = _cleanPath(hostKeys, u.pathname);
      const hash = _cleanHash(u.hash);

      if(kept.length === parts.length && path === u.pathname && hash === u.hash) return url;   // nothing to do

      return u.protocol + '//' + u.host + path + (kept.length ? '?' + kept.join('&') : '') + hash;
    } catch(_){ return url; }
  }

  // A URL inside prose ends where the sentence does. Trailing `.,;:!?` and a closing bracket that
  // was never opened inside the URL belong to the writing, not the link — Wikipedia titles like
  // `…/Foo_(disambiguation)` do open one, so a blanket trim would break exactly those.
  const URL_RE = /https?:\/\/[^\s<>"'`\\]+/gi;
  function _trimTrailing(s){
    let end = s.length;
    for(;;){
      const c = s[end-1];
      if(c === undefined) break;
      if('.,;:!?'.indexOf(c) !== -1){ end--; continue; }
      if(c === ')' || c === ']' || c === '}'){
        const open = c === ')' ? '(' : c === ']' ? '[' : '{';
        const body = s.slice(0, end);
        if(body.split(open).length > body.split(c).length) break;   // balanced — part of the URL
        end--; continue;
      }
      break;
    }
    return s.slice(0, end);
  }

  /**
   * Clean every link in a block of text.
   * → { text, count, changes: [[before, after], …] }
   * Text with no links, or no trackers in them, comes back identical (`count === 0`).
   */
  function cleanText(text){
    if(typeof text !== 'string' || !text) return { text: text || '', count: 0, changes: [] };
    const changes = [];
    const out = text.replace(URL_RE, m => {
      const core = _trimTrailing(m), tail = m.slice(core.length);
      const c = clean(core);
      if(c === core) return m;
      changes.push([core, c]);
      return c + tail;
    });
    return { text: out, count: changes.length, changes };
  }

  const API = { clean, cleanText, PARAMS, PREFIXES, HOST_PARAMS, REDIRECTORS,
                _hostKeys, _trimTrailing, URL_RE };
  root.PCUrlClean = API;
  if(typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof globalThis !== 'undefined' ? globalThis : this);
