# Web Search

A search engine inside the client (sidebar → **Web Search**, mobile ☰ More → Web Search), on top of
whatever SearXNG this node uses. Four things a SearXNG page does not do:

| | |
|---|---|
| **Save to Notes** | A result, an article, a summary or the overview → the encrypted notebook. The TEXT is saved, not a bookmark, so it survives the page going away. |
| **Share** | Straight into the composer, with the link. |
| **Summarize a link** | The node reads the page and summarizes it. |
| **AI overview** | The Google/Bing answer-box: your query answered from the top results, **with numbered citations** back to them. |

Everything else is the point of a search engine and works the way you expect: tabs (Web / News /
Images / Videos / Science / Tech / Files), a time filter, More results, and related searches.

**It keeps your place.** The query, the filters, the results, the overview and both scroll positions
live in module state, not in the DOM — `#feed` is one element every view shares and app.js blanks it
on entry, so anything left in the page is gone the moment you glance at Messages. Leaving and coming
back repaints all of it with no refetch.

**Results open IN the app — as the PAGE.** Clicking a result (anywhere on the card) frames the real
page, laid out the way its author laid it out, with `← Results` returning to the exact offset you
left. **Reader** toggles to extracted text for a page that is mostly ads. A browser tab is a one-way
door on a phone — in the PWA/APK coming back is often a cold restart, i.e. the results are gone —
but **Open** is always in the bar, and ctrl/⌘/middle-click on the title still gets a real tab. The
Android back button closes the reader before it leaves the view (`PCWebSearch.readerOpen()`).

The page is framed from `/api/websearch/page` on **our own** origin, not pointed at the site: most
sites refuse to be framed at all (`X-Frame-Options` / `frame-ancestors`). That endpoint strips
everything that executes — scripts, inline handlers, `javascript:` urls, forms, nested frames — and
serves a CSP with no `script-src` at all, so the frame lays itself out and does nothing else. CSS,
images and fonts are kept and absolutised, which is the difference between "the page" and a naked
wall of text; they load from the site itself, so this is **not** an anonymising proxy and does not
pretend to be. Links inside are rewritten back through the endpoint, so following one stays in the
app.

## As your browser's search engine

`https://<node>/search?q=%s` opens the client straight into Web Search with that query, and
`/opensearch.xml` (linked from the client shell) is what makes a browser OFFER to add it — so
Chrome/Firefox can search this node from the URL bar instead of Google.

## Where a node searches

ONE resolution order (`search_service.resolve_searxng_url`), shared by **every** consumer — the AI's
web-search tool, the news digests, the bots (`bot_manager_service` hands them the resolved value as
`SEARXNG_URL`), and this screen:

0. **Admin → Tools → "Web search enabled"**. Off = this node makes no search requests at all. It is a
   real switch because *clearing the URL is not one* — that falls through to the steps below.
1. **Admin → Tools → SearXNG URL**, if set.
2. **The SearXNG bundled with this node**, if one answers — `http://127.0.0.1:8899` (host install) or
   `http://searxng:8080` (compose). The probe requires **200 on `/healthz` AND JSON from `/config`**:
   `status < 500` alone let an unrelated listener's 404 pass, and the node then adopted it as its
   search backend with the public fallback never tried. Cached for 5 minutes. The port comes from
   `searxng/port`, which the installer writes — an env var set at install time never reaches the
   app's own systemd service.
3. **A public instance** (`https://searx.tiekoetter.com`), as a last resort.

Step 3 is a fallback, not a plan: measured from a server, it answers **429 Too Many Requests** to
both its JSON and its HTML endpoint, because public instances rate-limit clients that don't look like
a browser. Which is why step 2 exists.

This replaced a hardcoded `https://search.poster.place` default — so every node that never filled the
field in was silently searching through one particular deployment's box, with nothing to say so, and
that box was a single point of failure for everyone's AI web search, news digests and bots.

### Run one

```
./install.sh --searxng                 # host install: a systemd service on 127.0.0.1:8899
docker compose --profile cpu up -d     # compose: comes up with every AI backend profile
```

It runs as **`posterchanai-searxng.service`**, like every other service here — `systemctl status
posterchanai-searxng`, `journalctl -u posterchanai-searxng -f`, one restart policy — rather than a
detached container no unit file knows about. The container runs with `--network host`, which is not a
shortcut: it has to reach this node's HTTP proxy on `127.0.0.1` (see Tor below), and that proxy binds
to loopback. It is also installed by default on a **fresh install** and re-run on **upgrade**
(`./install.sh` → option 6).

The page is branded — PosterChan logo, "PosterChan Search", dark theme (`ui.theme_args.simple_style`)
— because `http://127.0.0.1:8899` is what an operator sees when they go looking.

**Binding is set with `GRANIAN_HOST`/`GRANIAN_PORT`, not `SEARXNG_BIND_ADDRESS` or
`server.bind_address`** — this image serves through granian, which reads neither of the latter two.
Measured, not assumed: with `SEARXNG_BIND_ADDRESS=127.0.0.1` set and the settings file saying
`0.0.0.0`, `ss -ltn` showed `*:8899` — in the host namespace that is an unauthenticated,
limiter-disabled metasearch instance listening on every interface of the box.

Then leave Admin → Tools **empty** — that is what selects it. Both paths generate from ONE template,
`docker/searxng/settings.yml`, so they cannot drift. Two things it gets right and a hand-rolled
instance usually doesn't:

* **`search.formats: [html, json]`.** SearXNG ships JSON **off**, and with it off every search here
  gets a 403 with an HTML body — which every caller reads as "no results" rather than as a
  misconfiguration. This is the single most likely reason an instance "doesn't work" with PosterChan.
* **`server.limiter: false`.** The only client is this node's app over loopback; the limiter is what
  makes public instances 429 us in the first place.

Note that **`secret_key` is the only setting this image maps to an environment variable** (checked in
`searx/settings_defaults.py`) — `SEARXNG_SEARCH_FORMATS` and friends do nothing, which is why both
paths ship a settings *file*. The host copy lives in `searxng/settings.yml` (gitignored, per node,
never overwritten once written — `SEARXNG_FORCE_SETTINGS=1` regenerates it).

## Tor

Two different hops, each proxied where it belongs:

* **The app → a REMOTE instance** goes through the built-in HTTP proxy (Tor1 ⇄ Tor2 round-robin) and
  falls back to a direct connection when the proxy can't be reached — `proxy_utils.afallback_transport`,
  the same pattern as the rest of the app's outbound HTTP.
  **Anything private is exempt, not just loopback**: Tor cannot route RFC1918, and the proxy answers an
  unroutable target with a 502 *response*, which the fallback transport does not retry (it only falls
  back on connect-level failures, so a delivered request is never re-sent). So `_is_local_base`
  resolves the host and treats loopback/private/link-local — and `.lan`/`.local` names — as direct.
  Without that, an ordinary self-hosted `http://192.168.0.85:8888` fails every request and reports
  "no results".
* **The bundled instance → the ENGINES** can go through the proxy's **fallback listener**
  (`proxy_fallback_port`, default **8119**: Tor1 → Tor2 → **direct**), but it is **off by default**,
  and that is a measurement rather than a preference. Through Tor the default engine set does not slow
  down, it stops answering: Brave and Google CSE return "too many requests", DuckDuckGo "access
  denied", Startpage a CAPTCHA, and SearXNG then suspends each engine for up to an hour. Measured on
  one node, same query, same minute: **25 results direct, 0 through Tor with all four engines
  suspended**. Search engines block exit nodes; timeout tuning doesn't change that (though a Tor run
  does also need `request_timeout: 12.0` — the 3s default times out on its own).

  Opt in with `SEARXNG_TOR=1 ./install.sh --searxng`, which probes the proxy and writes
  `outgoing.proxies` into `settings.yml`.

That second listener exists because the main `:8118` is **Tor-only by design** — torrent traffic
shares it and a silent direct connection there would be an IP leak. SearXNG has no fallback of its
own, so pointed at `:8118` one Tor outage turns every search (AI lookups, news, bots, this screen)
into a timeout that reads as "no results". **Never point torrent traffic at 8119.**

Force the installer either way with `SEARXNG_TOR=1` / `SEARXNG_TOR=0`.

**The proxy line is re-decided on every install and upgrade**, not frozen at first install. It has to
be: on a fresh install `setup_searxng` runs before the app (and therefore its proxy) has ever
started, so the probe always says "no proxy" — frozen, that would pin the node's engine requests to
direct, from the operator's real IP, forever. An existing `settings.yml` is otherwise left alone.
After turning the proxy on for the first time: restart posterchanai (that is what opens `:8119`),
then re-run `./install.sh --searxng`.

## Endpoints

All under `/api/websearch/*`, all authenticated. The three LLM paths are gated on the same `can_ai`
flag as chat (one shared GPU), serialized behind one semaphore, and answered from a 15-minute cache —
clicking ✨ twice on the same query is normal, not exotic.

| | |
|---|---|
| `GET /search` | One page of results. No LLM, no page fetching — any logged-in user. |
| `GET /read` | A page's extracted text, for the reader. Returns TEXT, not a proxied page. |
| `POST /summarize` | One link, read and summarized. |
| `POST /overview` | The overview. **Re-runs the search server-side** rather than accepting results from the client, so the page cannot choose what the model reads. |

Result URLs come from a third-party search engine, i.e. they are attacker-influencable by
definition — every fetch goes through `search_service.fetch_url_content`, so the SSRF guard
(`is_safe_url`) is the same one every other URL-reading path uses. A blocked URL comes back as a
readable message next to an "open the original" link, not as an empty article.

**The guard runs per REDIRECT HOP.** `fetch_url_content` follows redirects by hand
(`follow_redirects=False` + a five-hop loop) because the checker only ever saw the first URL: with
httpx following, `https://attacker.example/r` → 302 → `http://169.254.169.254/latest/meta-data/` was
fetched and its body handed back to the caller and to the model. That was always latent; Web Search
is what made a URL the node did not choose into an input.

## Tests

```
venv-unified/bin/python -m unittest tests.test_websearch          # resolution order, transport, router
venv-unified/bin/python scripts/check_websearch_mobile.py         # phone/desktop layout + keeps-your-place
```

`check_client_mobile.py` never opens this screen, so the second one is not optional before a deploy.
It drives the real `websearch.js` against a stubbed `window.__PC` — no server, no relay, no login —
and reproduces `#feed` as the app's real scroll container, without which the "keeps your place"
assertions pass for the wrong reason.
