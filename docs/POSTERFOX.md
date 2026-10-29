# PosterFox

PosterFox is PosterChanOS's browser: Mozilla's Firefox, built from Mozilla's source the way Gentoo builds it, with a
PosterChan look and a few defaults changed.

- **Looks like a PosterChan desktop window:** cyberpunk colours, the focused-window neon ring, and minimise /
  maximise / close on the right.
- **Telemetry compiled out.**
- **uBlock Origin and Privacy Badger** built in.
- **Private by default:** no history, everything cleared on exit, and every tab its own container.
- **PosterChan AI** in Firefox's AI sidebar.
- **Your existing Firefox profile carries over.**

It lives in its own repository:

- `nostr://npub1fdtthaqujtjcd6yfy7kt0zpkadyl9vvypq00s5nztnmche74d0tqv6uwwr/relay.poster.place/posterfox`
- https://github.com/loblawbob873-svg/posterfox (mirror)

**Maintaining it — updates, changes, the build VM, signing, what goes wrong and how to fix it — is documented in
that repository's [`docs/MAINTAINING.md`](https://github.com/loblawbob873-svg/posterfox/blob/main/docs/MAINTAINING.md).**

What lives in *this* repository:

- `os/overlay/www-client/posterfox/`: the package. It is **generated** by the posterfox repo's `tools/update.sh` /
  `tools/release.sh` from Gentoo's own Firefox ebuild. Never edit it by hand; the next update overwrites it.
- `static/js/client/app.js`: `/client?pcai=1&q=<prompt>` is how PosterFox's AI sidebar opens AI chat with the
  prompt in the box, unsent.
- `os/firefox-theme/`: the cyberpunk theme. PosterFox builds it unsigned into its application scope; firefox-bin
  uses the copy signed by addons.mozilla.org.

PosterChanOS machines install `firefox-bin` until the first signed PosterFox binary is published and checked; see
"Not done yet" in MAINTAINING.md.
