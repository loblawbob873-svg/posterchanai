/* PosterFox default preferences. DEFAULTS, not locks: anything here can still be changed in about:config, like any
 * preference on the person's own machine. Telemetry is not here because it is not built (USE=telemetry is gone). */

// The cyberpunk theme and PosterChan Connect ship in the APPLICATION scope (browser/extensions/), unsigned -- this
// build allows that scope (--with-unsigned-addon-scopes=app,system). 15 would install them DISABLED, waiting for a
// click nobody knows to make; 11 = profile, user and system still ask, the app's own add-ons do not.
pref("extensions.autoDisableScopes", 11);
pref("extensions.activeThemeID", "cyberpunk-theme@poster.place");

// The AI sidebar (Firefox's own chatbot panel). The provider is THIS computer's PosterChan instance, set at startup
// by posterfox.cfg from ~/.config/posterchan-desktop/config.json -- here only the switch.
pref("browser.ml.chat.enabled", true);
pref("browser.ml.chat.sidebar", true);
pref("browser.ml.chat.shortcuts", true);

// Quiet by default: no sponsored anything, no "what's new" pages, no default-browser nagging.
// THE NEW TAB'S SHORTCUTS ARE POSTERCHAN'S ("remove the wikipedia, youtube, reddit and add-ons links on new tab page and
// replace it with posterchan git, poster.place, posterfox git page, posterchan passwords extension page", 2026-10-29).
// Firefox takes its defaults from Mozilla's remote settings, which is switched off; these four are the defaults and are
// pinned with their names. A shortcut the person pins or removes is theirs (a profile's own value wins over a default).
pref("browser.topsites.useRemoteSetting", false);
pref("browser.topsites.contile.enabled", false);
pref("browser.newtabpage.activity-stream.default.sites", "https://poster.place/,https://poster.place/r/npub1fdtthaqujtjcd6yfy7kt0zpkadyl9vvypq00s5nztnmche74d0tqv6uwwr/posterchanai,https://poster.place/r/npub1fdtthaqujtjcd6yfy7kt0zpkadyl9vvypq00s5nztnmche74d0tqv6uwwr/posterfox,https://addons.mozilla.org/firefox/addon/posterchan-passwords/");
pref("browser.newtabpage.pinned", "[{\"url\": \"https://poster.place/\", \"label\": \"poster.place\"}, {\"url\": \"https://poster.place/r/npub1fdtthaqujtjcd6yfy7kt0zpkadyl9vvypq00s5nztnmche74d0tqv6uwwr/posterchanai\", \"label\": \"PosterChan Git\"}, {\"url\": \"https://poster.place/r/npub1fdtthaqujtjcd6yfy7kt0zpkadyl9vvypq00s5nztnmche74d0tqv6uwwr/posterfox\", \"label\": \"PosterFox Git\"}, {\"url\": \"https://addons.mozilla.org/firefox/addon/posterchan-passwords/\", \"label\": \"PosterChan Passwords\"}]");
pref("browser.newtabpage.activity-stream.showSponsored", false);
pref("browser.newtabpage.activity-stream.showSponsoredTopSites", false);
pref("browser.newtabpage.activity-stream.feeds.section.topstories", false);
pref("browser.shell.checkDefaultBrowser", false);
pref("browser.aboutwelcome.enabled", false);
pref("browser.urlbar.suggest.quicksuggest.sponsored", false);
pref("browser.urlbar.suggest.quicksuggest.nonsponsored", false);
pref("app.shield.optoutstudies.enabled", false);
pref("browser.discovery.enabled", false);

// The PosterChan look: chrome stylesheets come from posterfox.cfg; the window draws its own title bar like every
// PosterChan window does.
pref("browser.tabs.inTitlebar", 1);
pref("browser.compactmode.show", true);

// PRIVATE BY DEFAULT (asked 2026-10-28): no history, everything cleared on exit, every tab its own container.
// Not Firefox's "Never remember history" (permanent private browsing): containers do not work in private browsing.
// So: no history or form history kept, the shutdown sanitizer (policies.json SanitizeOnShutdown) clears cookies, site
// data, cache, downloads, sessions and site settings, and the Tab Containers add-on gives every tab its own
// container. Saved passwords are NOT cleared. These are defaults: anyone can change them in Settings.
pref("places.history.enabled", false);
pref("browser.formfill.enable", false);
pref("privacy.userContext.enabled", true);
pref("privacy.userContext.ui.enabled", true);
pref("browser.sessionstore.resume_from_crash", false);
pref("browser.startup.page", 1);

// TOR TABS FAIL CLOSED (extensions/tab-containers/tor.js): when a proxy cannot be reached Firefox may otherwise retry the
// request DIRECTLY -- for a Tor tab that is the one thing that must never happen. And WebRTC in a page loaded through a
// proxy uses only that proxy, so a Tor tab's page cannot reveal the address over UDP.
pref("network.proxy.failover_direct", false);
pref("media.peerconnection.ice.proxy_only_if_behind_proxy", true);
