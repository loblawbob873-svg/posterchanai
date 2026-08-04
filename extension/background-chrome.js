/* Chrome's background entry point.
 *
 * Firefox MV3 takes `"background": {"scripts": [...]}` and runs them as an event page. Chrome MV3
 * takes exactly one `service_worker` and REFUSES to load an extension that lists `scripts` — which is
 * the only thing that stopped this extension installing in Chrome at all. Everything else was already
 * portable: every file aliases `const B = browser ?? chrome`, and none of the background code touches
 * the DOM, localStorage or XMLHttpRequest, none of which exist in a service worker.
 *
 * So Chrome gets a one-line worker that pulls in the same three files, in the same order, that the
 * Firefox manifest lists. There is no second copy of any logic — build.sh stages both manifests over
 * one set of sources, so a fix cannot land in one browser and not the other.
 *
 * Classic worker, NOT a module: importScripts() is unavailable in a module worker, and going to
 * `"type": "module"` would mean rewriting three files (one of them shared with the app) as modules
 * for no gain.
 */
importScripts('vendor/nostr.bundle.js', 'vaultcore.js', 'background.js');
