/* PosterChan client icon sprite.
 *
 * This USED to be inline in templates/client.html. That put the symbols and the code referencing them
 * on two different cache clocks: the shell is served by the app behind nginx proxy_cache (~30s) and the
 * service worker, while app.js comes from router.lan's own /static clone with no-cache. Any deploy that
 * ADDED a symbol therefore had a window where app.js asked for a `#i-name` the visitor's cached shell
 * did not have yet — and a <use> pointing at a missing symbol renders NOTHING, silently, at 0x0. That
 * shipped as "some buttons on the posts have no icons", fixed by a refresh.
 *
 * Living in /static, this file carries the same ?v= cache-buster and the same no-cache policy as app.js,
 * so symbols and their callers can no longer drift apart.
 *
 * Loaded as a BLOCKING classic script at the top of <body>, before the nav markup is parsed, so the
 * static `<use>` references in the shell resolve on first paint with no icon flash.
 *
 * Every shape strokes `currentColor` on a 24x24 grid and carries no hardcoded colour, so an icon takes
 * the colour of whatever row it sits in and works on all seven themes with zero per-theme CSS.
 */
(function(){
  var SPRITE = `
<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false"><defs>
  <symbol id="i-ai" viewBox="0 0 24 24"><path d="M12 3.2l1.8 4.9 4.9 1.8-4.9 1.8L12 16.6l-1.8-4.9L5.3 9.9l4.9-1.8z"/><path d="M18.4 15.2l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z"/></symbol>
  <symbol id="i-globe" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.8"/><path d="M3.2 12h17.6"/><path d="M12 3.2c2.4 2.6 3.7 5.6 3.7 8.8S14.4 18.2 12 20.8c-2.4-2.6-3.7-5.6-3.7-8.8S9.6 5.8 12 3.2z"/></symbol>
  <symbol id="i-bell" viewBox="0 0 24 24"><path d="M18 9a6 6 0 10-12 0c0 5.2-2 6.6-2 6.6h16S18 14.2 18 9z"/><path d="M13.7 19.2a2 2 0 01-3.4 0"/></symbol>
  <symbol id="i-mail" viewBox="0 0 24 24"><rect x="3" y="5.2" width="18" height="13.6" rx="2.6"/><path d="M3.7 7.4l7.2 5.1a2 2 0 002.2 0l7.2-5.1"/></symbol>
  <symbol id="i-bookmark" viewBox="0 0 24 24"><path d="M7 3.4h10a1 1 0 011 1v16.2l-6-4-6 4V4.4a1 1 0 011-1z"/></symbol>
  <symbol id="i-phone" viewBox="0 0 24 24"><path d="M6.4 3.4h3l1.5 4-2 1.5a12.2 12.2 0 006.2 6.2l1.5-2 4 1.5v3a2 2 0 01-2.2 2A17.2 17.2 0 014.4 5.6a2 2 0 012-2.2z"/></symbol>
  <symbol id="i-live" viewBox="0 0 24 24"><circle cx="12" cy="12" r="2.4" fill="currentColor" stroke="none"/><path d="M7.6 7.6a6.3 6.3 0 000 8.8M16.4 16.4a6.3 6.3 0 000-8.8"/><path d="M4.7 4.7a10.4 10.4 0 000 14.6M19.3 19.3a10.4 10.4 0 000-14.6"/></symbol>
  <symbol id="i-translate" viewBox="0 0 24 24"><path d="M3 5.6h8.6M7.3 3.8v1.8M10 5.6c-.6 4.3-3.1 7.7-6.2 9.3M4.8 10.2c1.1 2.5 3.3 4.6 6.2 5.6"/><path d="M12.4 20.2l4-9 4 9M14 17.3h4.8"/></symbol>
  <symbol id="i-draft" viewBox="0 0 24 24"><path d="M4 20h4L18.4 9.6a2.4 2.4 0 10-3.4-3.4L4.6 16.6z"/><path d="M13.6 7.6l3.4 3.4"/></symbol>
  <symbol id="i-compass" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.8"/><path d="M15.6 8.4l-2 5.2-5.2 2 2-5.2z"/></symbol>
  <symbol id="i-news" viewBox="0 0 24 24"><path d="M16 6H5.4A1.4 1.4 0 004 7.4v10.2A1.4 1.4 0 005.4 19H17"/><path d="M16 6v11.6a1.4 1.4 0 002.8 0V9.6H16"/><path d="M7 9.4h6M7 12.4h6M7 15.4h4"/></symbol>
  <symbol id="i-chart" viewBox="0 0 24 24"><path d="M4 4v15.6h16"/><path d="M7 15l3.6-4.2 3 2.6L19.4 7"/><path d="M19.4 7h-3.2M19.4 7v3.2"/></symbol>
  <symbol id="i-article" viewBox="0 0 24 24"><path d="M13.2 3.2H7a2 2 0 00-2 2v13.6a2 2 0 002 2h10a2 2 0 002-2V9z"/><path d="M13.2 3.2V9H19"/><path d="M8.6 13h6.8M8.6 16.4h4.4"/></symbol>
  <symbol id="i-bag" viewBox="0 0 24 24"><path d="M5.2 8h13.6l-1 12.2H6.2z"/><path d="M9 10.2V7a3 3 0 016 0v3.2"/></symbol>
  <symbol id="i-tv" viewBox="0 0 24 24"><rect x="3" y="7.4" width="18" height="12.4" rx="2.4"/><path d="M8 3.4l4 4 4-4"/></symbol>
  <symbol id="i-users" viewBox="0 0 24 24"><circle cx="9.6" cy="8.4" r="3.3"/><path d="M3.8 19.6c0-3.2 2.6-5.2 5.8-5.2s5.8 2 5.8 5.2"/><path d="M16.4 6.2a3.3 3.3 0 010 5.8M18.2 14.6c1.6.8 2.6 2.2 2.6 4.2"/></symbol>
  <symbol id="i-chat" viewBox="0 0 24 24"><path d="M20.4 12.2c0 3.9-3.7 7.1-8.2 7.1a9.8 9.8 0 01-2.6-.35L4.6 20.6l1.3-3.5a6.8 6.8 0 01-2.3-4.9c0-3.9 3.7-7.1 8.2-7.1s8.6 3.2 8.6 7.1z"/></symbol>
  <symbol id="i-magnet" viewBox="0 0 24 24"><path d="M6 4.2v8a6 6 0 0012 0v-8h-4v8a2 2 0 11-4 0v-8z"/><path d="M6 8.6h4M14 8.6h4"/></symbol>
  <symbol id="i-git" viewBox="0 0 24 24"><circle cx="7" cy="5.8" r="2.4"/><circle cx="7" cy="18.2" r="2.4"/><circle cx="17" cy="9.8" r="2.4"/><path d="M7 8.2v7.6"/><path d="M17 12.2c0 2.9-2.8 3.3-5.6 4"/></symbol>
  <symbol id="i-leaf" viewBox="0 0 24 24"><path d="M20 4c-9 0-15 3.9-15 9.8A5 5 0 0010 19c6 0 10-6.2 10-15z"/><path d="M5 19.4c3-4.2 6.2-6.8 10.2-8.6"/></symbol>
  <symbol id="i-bars" viewBox="0 0 24 24"><path d="M4 20h16"/><rect x="5.8" y="11" width="3.6" height="6"/><rect x="14.6" y="6.6" width="3.6" height="10.4"/></symbol>
  <symbol id="i-gamepad" viewBox="0 0 24 24"><rect x="2.6" y="7.4" width="18.8" height="9.6" rx="4.6"/><path d="M7 10.6v3.4M5.3 12.3h3.4"/><circle cx="15.8" cy="11.4" r=".95" fill="currentColor" stroke="none"/><circle cx="18" cy="13.8" r=".95" fill="currentColor" stroke="none"/></symbol>
  <symbol id="i-pawn" viewBox="0 0 24 24"><circle cx="12" cy="6.8" r="2.6"/><path d="M9.6 9.2c0 2-1.1 2.9-1.6 4.6h8c-.5-1.7-1.6-2.6-1.6-4.6"/><path d="M8 13.8h8l1.3 6H6.7z"/></symbol>
  <symbol id="i-hash" viewBox="0 0 24 24"><path d="M9 3.6v16.8M15 3.6v16.8M3.6 9h16.8M3.6 15h16.8"/></symbol>
  <symbol id="i-target" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.6"/><circle cx="12" cy="12" r="4.4"/><circle cx="12" cy="12" r=".9" fill="currentColor" stroke="none"/></symbol>
  <symbol id="i-discs" viewBox="0 0 24 24"><rect x="3.4" y="3.4" width="17.2" height="17.2" rx="2.6"/><circle cx="8.6" cy="8.6" r="2"/><circle cx="15.4" cy="8.6" r="2"/><circle cx="8.6" cy="15.4" r="2"/><circle cx="15.4" cy="15.4" r="2"/></symbol>
  <symbol id="i-cards" viewBox="0 0 24 24"><rect x="9" y="4.8" width="10" height="14.4" rx="2"/><path d="M6.6 17.6L4.3 8.9a1.7 1.7 0 011.2-2.1l3.2-.9"/></symbol>
  <symbol id="i-spade" viewBox="0 0 24 24"><path d="M12 3.4C9.5 7 5.2 9 5.2 12.6A3.4 3.4 0 008.6 16c1.3 0 2.2-.6 2.7-1.3-.2 2.6-1 3.8-2.1 4.9h5.6c-1.1-1.1-1.9-2.3-2.1-4.9.5.7 1.4 1.3 2.7 1.3a3.4 3.4 0 003.4-3.4C18.8 9 14.5 7 12 3.4z"/></symbol>
  <symbol id="i-folder" viewBox="0 0 24 24"><path d="M3.4 7.4a2 2 0 012-2h3.5l2 2.5h7.7a2 2 0 012 2v8.7a2 2 0 01-2 2h-13.2a2 2 0 01-2-2z"/></symbol>
  <symbol id="i-flower" viewBox="0 0 24 24"><circle cx="12" cy="12" r="2.2"/><circle cx="12" cy="7.2" r="3"/><circle cx="12" cy="16.8" r="3"/><circle cx="7.2" cy="12" r="3"/><circle cx="16.8" cy="12" r="3"/></symbol>
  <symbol id="i-music" viewBox="0 0 24 24"><path d="M9.2 17.6V6.4l9.6-2v11"/><circle cx="6.6" cy="17.6" r="2.6"/><circle cx="16.2" cy="15.4" r="2.6"/></symbol>
  <symbol id="i-gear" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12.2 2.6h-.4a2 2 0 00-2 2v.2a2 2 0 01-1 1.7l-.4.3a2 2 0 01-2 0l-.2-.1a2 2 0 00-2.7.7l-.2.4a2 2 0 00.7 2.7l.2.1a2 2 0 011 1.7v.5a2 2 0 01-1 1.8l-.2.1a2 2 0 00-.7 2.7l.2.4a2 2 0 002.7.7l.2-.1a2 2 0 012 0l.4.3a2 2 0 011 1.7v.2a2 2 0 002 2h.4a2 2 0 002-2v-.2a2 2 0 011-1.7l.4-.3a2 2 0 012 0l.2.1a2 2 0 002.7-.7l.2-.4a2 2 0 00-.7-2.7l-.2-.1a2 2 0 01-1-1.8v-.5a2 2 0 011-1.7l.2-.1a2 2 0 00.7-2.7l-.2-.4a2 2 0 00-2.7-.7l-.2.1a2 2 0 01-2 0l-.4-.3a2 2 0 01-1-1.7v-.2a2 2 0 00-2-2z"/></symbol>
  <symbol id="i-home" viewBox="0 0 24 24"><path d="M3.2 10.6L12 3.4l8.8 7.2"/><path d="M5.6 9.4v10.2a1 1 0 001 1h10.8a1 1 0 001-1V9.4"/><path d="M9.6 20.6v-6h4.8v6"/></symbol>
  <symbol id="i-pen" viewBox="0 0 24 24"><path d="M11 4.4H6a2 2 0 00-2 2v11.4a2 2 0 002 2h11.4a2 2 0 002-2v-5"/><path d="M18.3 2.9a2 2 0 012.8 2.8l-8.6 8.6-3.5 1 1-3.5z"/></symbol>
  <symbol id="i-menu" viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h16"/></symbol>
  <symbol id="i-user" viewBox="0 0 24 24"><circle cx="12" cy="8" r="3.6"/><path d="M4.6 20c0-3.6 3.4-5.7 7.4-5.7s7.4 2.1 7.4 5.7"/></symbol>
  <symbol id="i-logout" viewBox="0 0 24 24"><path d="M9.4 20.6H6a2 2 0 01-2-2V5.4a2 2 0 012-2h3.4"/><path d="M15.8 16.2l4.2-4.2-4.2-4.2"/><path d="M20 12H9.6"/></symbol>
  <!-- platform marks for the "Get the app" links -->
  <symbol id="i-android" viewBox="0 0 24 24"><path d="M5.6 11.4a6.4 6.4 0 0112.8 0z"/><path d="M8.2 7.6L6.9 5.5M15.8 7.6l1.3-2.1"/><circle cx="9.7" cy="9.5" r=".85" fill="currentColor" stroke="none"/><circle cx="14.3" cy="9.5" r=".85" fill="currentColor" stroke="none"/><rect x="5.6" y="12.9" width="12.8" height="6.6" rx="2"/></symbol>
  <symbol id="i-windows" viewBox="0 0 24 24"><path d="M3.6 6.3l7-1v6.2h-7zM12.2 5.1l8.2-1.2v7.6h-8.2zM3.6 12.6h7v6.2l-7-1zM12.2 12.6h8.2v7.6l-8.2-1.2z"/></symbol>
  <symbol id="i-apple" viewBox="0 0 24 24"><path d="M15.9 12.5c0-2 1.6-3 1.7-3.1-.9-1.4-2.4-1.6-2.9-1.6-1.2-.1-2.4.7-3 .7s-1.6-.7-2.6-.7c-1.3 0-2.6.8-3.3 2-1.4 2.4-.4 6 1 8 .7 1 1.5 2.1 2.5 2 1-.04 1.4-.65 2.6-.65s1.5.65 2.6.63c1.1-.02 1.8-1 2.4-2 .8-1.1 1.1-2.2 1.1-2.3-.02 0-2.1-.8-2.1-3z"/><path d="M13.9 6.2c.5-.6.9-1.5.8-2.4-.8.03-1.7.5-2.3 1.2-.5.6-.9 1.5-.8 2.4.9.07 1.8-.4 2.3-1.2z"/></symbol>
  <symbol id="i-linux" viewBox="0 0 24 24"><path d="M12 3.2c-2.1 0-3.5 1.7-3.5 3.9 0 1.2-.3 2-1 3-1 1.5-1.9 3.2-1.9 5.2 0 2.9 2.9 5.4 6.4 5.4s6.4-2.5 6.4-5.4c0-2-.9-3.7-1.9-5.2-.7-1-1-1.8-1-3 0-2.2-1.4-3.9-3.5-3.9z"/><circle cx="10.5" cy="7.4" r=".8" fill="currentColor" stroke="none"/><circle cx="13.5" cy="7.4" r=".8" fill="currentColor" stroke="none"/><path d="M12 8.7l-1.1 1.5h2.2z"/></symbol>
  <!-- ACTION VERBS. The nav has been on this sprite for a while; the buttons never were, so every
       action in the app labelled itself with an emoji (🚀 Post, 💾 Save, ⬇ Download, 📋 Copy link…).
       Emoji are the wrong weight beside the UI type, render differently on every platform, and cannot
       inherit colour or state — which is why they read as unstyled. Same 24-grid + currentColor stroke
       as the rest of the sprite, so they take the theme for free. -->
  <symbol id="i-send" viewBox="0 0 24 24"><path d="M21 3.8L10.6 14.2"/><path d="M21 3.8l-6.6 17.4-3.8-7-7-3.8z"/></symbol>
  <symbol id="i-download" viewBox="0 0 24 24"><path d="M12 3.6v11.2"/><path d="M7.8 10.8L12 15l4.2-4.2"/><path d="M4.4 19.4h15.2"/></symbol>
  <symbol id="i-cloud" viewBox="0 0 24 24"><path d="M7.2 19.2a4.2 4.2 0 01-.4-8.38 5.6 5.6 0 0110.83-1.35A3.9 3.9 0 0117.6 19.2z"/><path d="M12 11.4v5.2"/><path d="M9.9 14.5L12 16.6l2.1-2.1"/></symbol>
  <symbol id="i-link" viewBox="0 0 24 24"><path d="M10.2 13.8a3.8 3.8 0 005.7.4l2.6-2.6a3.8 3.8 0 00-5.37-5.37l-1.5 1.48"/><path d="M13.8 10.2a3.8 3.8 0 00-5.7-.4l-2.6 2.6a3.8 3.8 0 005.37 5.37l1.48-1.48"/></symbol>
  <symbol id="i-film" viewBox="0 0 24 24"><rect x="3" y="4.6" width="18" height="14.8" rx="2.2"/><path d="M7.4 4.6v14.8M16.6 4.6v14.8M3 12h18M3 8.3h4.4M3 15.7h4.4M16.6 8.3H21M16.6 15.7H21"/></symbol>
  <symbol id="i-reply" viewBox="0 0 24 24"><path d="M9.4 6.6L4 12l5.4 5.4"/><path d="M4 12h9.6a6 6 0 016 6v1.4"/></symbol>
  <symbol id="i-image" viewBox="0 0 24 24"><rect x="3.2" y="4.8" width="17.6" height="14.4" rx="2.4"/><circle cx="8.8" cy="10" r="1.7"/><path d="M3.6 16.6l4.6-4.2 3.6 3.2 3.2-2.8 5.4 4.6"/></symbol>
  <symbol id="i-text" viewBox="0 0 24 24"><path d="M5 6.4V4.8h14v1.6"/><path d="M12 4.8v14.4"/><path d="M9 19.2h6"/></symbol>
  <symbol id="i-play" viewBox="0 0 24 24"><path d="M8.4 5.6l10 6.4-10 6.4z"/></symbol>
  <symbol id="i-trash" viewBox="0 0 24 24"><path d="M4.4 6.6h15.2"/><path d="M9.4 6.6V4.8a1.2 1.2 0 011.2-1.2h2.8a1.2 1.2 0 011.2 1.2v1.8"/><path d="M6.4 6.6l.9 12a1.6 1.6 0 001.6 1.5h6.2a1.6 1.6 0 001.6-1.5l.9-12"/><path d="M10.4 10.4v6M13.6 10.4v6"/></symbol>
  <symbol id="i-paperclip" viewBox="0 0 24 24"><path d="M19.6 11.2l-7.9 7.9a4.6 4.6 0 01-6.5-6.5l8.3-8.3a3.1 3.1 0 014.4 4.4l-8.3 8.3a1.6 1.6 0 01-2.2-2.2l7.5-7.5"/></symbol>
  <symbol id="i-share" viewBox="0 0 24 24"><circle cx="17.6" cy="5.8" r="2.6"/><circle cx="6.4" cy="12" r="2.6"/><circle cx="17.6" cy="18.2" r="2.6"/><path d="M8.7 10.8l6.6-3.6M8.7 13.2l6.6 3.6"/></symbol>
  <symbol id="i-arrow-left" viewBox="0 0 24 24"><path d="M19.4 12H4.6"/><path d="M10.6 5.8L4.4 12l6.2 6.2"/></symbol>
  <symbol id="i-volume" viewBox="0 0 24 24"><path d="M5 9.4h3.2L12.6 6v12l-4.4-3.4H5z"/><path d="M15.8 9.6a3.6 3.6 0 010 4.8M18.2 7.2a7 7 0 010 9.6"/></symbol>
  <symbol id="i-eye" viewBox="0 0 24 24"><path d="M2.4 12S5.9 5.8 12 5.8 21.6 12 21.6 12 18.1 18.2 12 18.2 2.4 12 2.4 12z"/><circle cx="12" cy="12" r="3"/></symbol>
  <symbol id="i-mic" viewBox="0 0 24 24"><rect x="9.2" y="3" width="5.6" height="10.4" rx="2.8"/><path d="M5.8 11.6a6.2 6.2 0 0012.4 0"/><path d="M12 17.8v3.2M9 21h6"/></symbol>
  <symbol id="i-zap" viewBox="0 0 24 24"><path d="M13.4 2.6L4.8 13.6h5.6l-.8 7.8 8.6-11h-5.6z"/></symbol>
  <symbol id="i-clock" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.8"/><path d="M12 6.9V12l3.4 2"/></symbol>
  <symbol id="i-key" viewBox="0 0 24 24"><circle cx="8" cy="12" r="4"/><path d="M12 12h9.4"/><path d="M18 12v3.2M20.8 12v2.4"/></symbol>
  <symbol id="i-camera" viewBox="0 0 24 24"><path d="M4.4 7.6h3l1.4-2.2h6.4l1.4 2.2h3a1.8 1.8 0 011.8 1.8v8.2a1.8 1.8 0 01-1.8 1.8H4.4a1.8 1.8 0 01-1.8-1.8V9.4a1.8 1.8 0 011.8-1.8z"/><circle cx="12" cy="13.2" r="3.4"/></symbol>
  <symbol id="i-check" viewBox="0 0 24 24"><path d="M4.6 12.6l4.8 4.8 10-10.8"/></symbol>
  <symbol id="i-shuffle" viewBox="0 0 24 24"><path d="M3.6 6.6h3.6l9.2 10.8h4"/><path d="M3.6 17.4h3.6l3.4-4"/><path d="M13.8 8.2l2.6-.8 4-.8"/><path d="M18.2 4.4l2.2 2.2-2.2 2.2M18.2 15.2l2.2 2.2-2.2 2.2"/></symbol>
  <symbol id="i-shield" viewBox="0 0 24 24"><path d="M12 3l7.4 3.2v5.4c0 4.6-3.2 8.2-7.4 10.4-4.2-2.2-7.4-5.8-7.4-10.4V6.2z"/></symbol>
  <symbol id="i-upload" viewBox="0 0 24 24"><path d="M12 20.4V9.2"/><path d="M7.8 13.4L12 9.2l4.2 4.2"/><path d="M4.4 4.6h15.2"/></symbol>
  <symbol id="i-repost" viewBox="0 0 24 24"><path d="M4.6 8.4A3 3 0 017.6 5.4h9.2"/><path d="M14.2 2.8l2.8 2.6-2.8 2.6"/><path d="M19.4 15.6a3 3 0 01-3 3H7.2"/><path d="M9.8 21.2L7 18.6l2.8-2.6"/></symbol>
  <symbol id="i-quote" viewBox="0 0 24 24"><path d="M20.4 12.2c0 3.9-3.7 7.1-8.2 7.1a9.8 9.8 0 01-2.6-.35L4.6 20.6l1.3-3.5a6.8 6.8 0 01-2.3-4.9c0-3.9 3.7-7.1 8.2-7.1s8.6 3.2 8.6 7.1z"/></symbol>
  <symbol id="i-wand" viewBox="0 0 24 24"><path d="M4.4 19.6L15.2 8.8"/><path d="M13.4 7l3.6 3.6"/><path d="M18.4 3.2l.7 1.9 1.9.7-1.9.7-.7 1.9-.7-1.9-1.9-.7 1.9-.7z"/><path d="M7.6 3.6l.5 1.4 1.4.5-1.4.5-.5 1.4-.5-1.4-1.4-.5 1.4-.5z"/></symbol>
  <symbol id="i-broom" viewBox="0 0 24 24"><path d="M14.6 3.4l6 6"/><path d="M13.2 6.6l-8 8"/><path d="M9.4 12.4l2.2 2.2-4.6 5.8H4.2l-.6-3z"/><path d="M11.6 14.6l3.8-3.8 3.4 3.4-3.8 3.8z"/></symbol>
  <symbol id="i-monitor" viewBox="0 0 24 24"><rect x="2.8" y="4.4" width="18.4" height="12.4" rx="2"/><path d="M8.4 20.4h7.2M12 16.8v3.6"/></symbol>
  <symbol id="i-bandage" viewBox="0 0 24 24"><rect x="2.2" y="8.4" width="19.6" height="7.2" rx="3.6" transform="rotate(-45 12 12)"/><path d="M9.4 9.4l5.2 5.2"/></symbol>
  <symbol id="i-palette" viewBox="0 0 24 24"><path d="M12 3.2a8.8 8.8 0 000 17.6c1.4 0 2-.9 2-1.8 0-.9-.7-1.5-.7-2.3 0-.8.6-1.4 1.5-1.4h1.8a4.2 4.2 0 004.2-4.2c0-4.4-4-7.9-8.8-7.9z"/><circle cx="8.2" cy="10" r="1.1" fill="currentColor" stroke="none"/><circle cx="12" cy="7.6" r="1.1" fill="currentColor" stroke="none"/><circle cx="15.8" cy="10" r="1.1" fill="currentColor" stroke="none"/></symbol>
  <symbol id="i-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></symbol>
  <symbol id="i-refresh" viewBox="0 0 24 24"><path d="M20.4 12a8.4 8.4 0 01-14.5 5.8L3.6 15.6"/><path d="M3.6 12a8.4 8.4 0 0114.5-5.8l2.3 2.2"/><path d="M3.6 20.2v-4.6h4.6M20.4 3.8v4.6h-4.6"/></symbol>
  <symbol id="i-bug" viewBox="0 0 24 24"><rect x="7.6" y="7.8" width="8.8" height="11.4" rx="4.4"/><path d="M9.4 6.6a2.6 2.6 0 015.2 0"/><path d="M7.6 11.4H4.2M16.4 11.4h3.4M7.6 15.6H4.6M16.4 15.6h3M8.6 8.4L6.2 5.6M15.4 8.4l2.4-2.8M8.8 18.8l-2 2.2M15.2 18.8l2 2.2"/></symbol>
  <symbol id="i-flag" viewBox="0 0 24 24"><path d="M5.4 21V3.6"/><path d="M5.4 4.6h11.8l-1.8 3.6 1.8 3.6H5.4z"/></symbol>
  <symbol id="i-relay" viewBox="0 0 24 24"><circle cx="12" cy="12" r="2.4"/><path d="M7.4 7.4a6.5 6.5 0 000 9.2M16.6 16.6a6.5 6.5 0 000-9.2"/><path d="M4.4 4.4a10.7 10.7 0 000 15.2M19.6 19.6a10.7 10.7 0 000-15.2"/></symbol>
  <symbol id="i-layout" viewBox="0 0 24 24"><rect x="3.2" y="4.4" width="17.6" height="15.2" rx="2.2"/><path d="M3.2 9.6h17.6M10.4 9.6v10"/></symbol>
  <!-- Transport, editing and direction marks — the second conversion pass (media player, meme
       builder, streams, find bar). Same rules as everything above: 24x24, currentColor, no fill. -->
  <symbol id="i-grid" viewBox="0 0 24 24"><rect x="3.4" y="3.4" width="17.2" height="17.2" rx="2.2"/><path d="M3.4 9.2h17.2M3.4 14.8h17.2M9.2 3.4v17.2M14.8 3.4v17.2"/></symbol>
  <symbol id="i-tag" viewBox="0 0 24 24"><path d="M3.4 11.2V4.4a1 1 0 011-1h6.8a2 2 0 011.4.6l7.4 7.4a2 2 0 010 2.8l-6.4 6.4a2 2 0 01-2.8 0L4 12.6a2 2 0 01-.6-1.4z"/><circle cx="8" cy="8" r="1.5"/></symbol>
  <symbol id="i-branch" viewBox="0 0 24 24"><circle cx="7" cy="5.8" r="2.4"/><circle cx="7" cy="18.2" r="2.4"/><circle cx="17" cy="5.8" r="2.4"/><path d="M7 8.2v7.6"/><path d="M17 8.2c0 4.4-3.6 5.4-7 6.4"/></symbol>
  <symbol id="i-stop" viewBox="0 0 24 24"><rect x="6.4" y="6.4" width="11.2" height="11.2" rx="1.8"/></symbol>
  <symbol id="i-minimize" viewBox="0 0 24 24"><path d="M5.5 17.5h13"/></symbol>
  <symbol id="i-expand" viewBox="0 0 24 24"><path d="M14.4 3.6h6v6M9.6 20.4h-6v-6"/><path d="M20.4 3.6l-7 7M3.6 20.4l7-7"/></symbol>
  <symbol id="i-prev" viewBox="0 0 24 24"><path d="M18.4 5.6v12.8L9.2 12z"/><path d="M6.4 5.4v13.2"/></symbol>
  <symbol id="i-next" viewBox="0 0 24 24"><path d="M5.6 5.6v12.8L14.8 12z"/><path d="M17.6 5.4v13.2"/></symbol>
  <symbol id="i-star" viewBox="0 0 24 24"><path d="M12 3.6l2.6 5.7 6.2.7-4.6 4.2 1.3 6.1L12 17.2l-5.5 3.1 1.3-6.1-4.6-4.2 6.2-.7z"/></symbol>
  <symbol id="i-swap" viewBox="0 0 24 24"><path d="M4.4 8.8h13.2M14.4 5.6l3.2 3.2-3.2 3.2"/><path d="M19.6 15.2H6.4M9.6 12l-3.2 3.2 3.2 3.2"/></symbol>
  <symbol id="i-heart" viewBox="0 0 24 24"><path d="M12 20.2l-7.2-7.1a4.6 4.6 0 116.5-6.5l.7.7.7-.7a4.6 4.6 0 116.5 6.5z"/></symbol>
  <symbol id="i-undo" viewBox="0 0 24 24"><path d="M4.6 8.6h9.8a5.4 5.4 0 010 10.8H7"/><path d="M8 4.6L4.4 8.6 8 12.6"/></symbol>
  <symbol id="i-redo" viewBox="0 0 24 24"><path d="M19.4 8.6H9.6a5.4 5.4 0 000 10.8H17"/><path d="M16 4.6l3.4 4-3.4 4"/></symbol>
  <symbol id="i-scissors" viewBox="0 0 24 24"><circle cx="7" cy="17.8" r="2.6"/><circle cx="17" cy="17.8" r="2.6"/><path d="M15.6 16L6.4 4M8.4 16l9.2-12"/></symbol>
  <symbol id="i-arrow-down" viewBox="0 0 24 24"><path d="M12 4.2v15.6"/><path d="M6 13.8l6 6 6-6"/></symbol>
  <symbol id="i-chevron-up" viewBox="0 0 24 24"><path d="M6 14.6l6-6 6 6"/></symbol>
  <symbol id="i-chevron-down" viewBox="0 0 24 24"><path d="M6 9.4l6 6 6-6"/></symbol>
  <symbol id="i-arrows-h" viewBox="0 0 24 24"><path d="M3.4 12h17.2"/><path d="M7 8.4L3.4 12 7 15.6"/><path d="M17 8.4l3.6 3.6-3.6 3.6"/></symbol>
  <symbol id="i-nsfw" viewBox="0 0 24 24"><path d="M3.4 3.4l17.2 17.2"/><path d="M10.6 6.3A9.6 9.6 0 0112 6.2c5 0 9 5.8 9 5.8a17.4 17.4 0 01-3 3.5"/><path d="M6.5 8.2A17.4 17.4 0 003 12s4 5.8 9 5.8a9.4 9.4 0 004-.9"/><path d="M9.9 10.1a3 3 0 004.2 4.2"/></symbol>
  <!-- the AI welcome card's capability set -->
  <symbol id="i-robot" viewBox="0 0 24 24"><rect x="3.6" y="7.6" width="16.8" height="11.6" rx="3"/><path d="M12 4v3.6"/><circle cx="12" cy="3.2" r="1.2"/><circle cx="8.8" cy="12.6" r="1.15" fill="currentColor" stroke="none"/><circle cx="15.2" cy="12.6" r="1.15" fill="currentColor" stroke="none"/><path d="M9.4 16.2h5.2"/></symbol>
  <symbol id="i-headphones" viewBox="0 0 24 24"><path d="M4 14.4v-2a8 8 0 0116 0v2"/><rect x="2.8" y="14" width="4.6" height="6.2" rx="2"/><rect x="16.6" y="14" width="4.6" height="6.2" rx="2"/></symbol>
  <symbol id="i-search" viewBox="0 0 24 24"><circle cx="10.6" cy="10.6" r="6.6"/><path d="M15.4 15.4l5 5"/></symbol>
  <symbol id="i-compress" viewBox="0 0 24 24"><path d="M4 4.6h16"/><path d="M4 19.4h16"/><path d="M12 7.4v3.2M12 16.6v-3.2"/><path d="M9.2 9.6L12 6.8l2.8 2.8M9.2 14.4L12 17.2l2.8-2.8"/></symbol>
  <symbol id="i-circle-crop" viewBox="0 0 24 24"><circle cx="12" cy="12" r="6.6"/><path d="M7.4 2.8v4.6H2.8M21.2 16.6h-4.6v4.6"/></symbol>
  <symbol id="i-speech" viewBox="0 0 24 24"><path d="M4.4 5.6h11.2a2 2 0 012 2v5a2 2 0 01-2 2H9.2l-4 3.2v-3.2a2 2 0 01-.8-1.6v-5.4a2 2 0 011-2z"/><path d="M19.2 8.6a4.4 4.4 0 010 6.8"/></symbol>
  <symbol id="i-pin" viewBox="0 0 24 24"><path d="M9 3.6h6l-.8 5.4 3.2 3.2H6.6l3.2-3.2z"/><path d="M12 12.2v8.2"/></symbol>
  <symbol id="i-chevron-right" viewBox="0 0 24 24"><path d="M9.4 6l6 6-6 6"/></symbol>
  <symbol id="i-chevron-left" viewBox="0 0 24 24"><path d="M14.6 6l-6 6 6 6"/></symbol>
  <symbol id="i-restore" viewBox="0 0 24 24"><path d="M4.2 12a7.8 7.8 0 107.8-7.8c-2.6 0-4.9 1.3-6.3 3.2"/><path d="M3.6 4.2v4.6h4.6"/></symbol>
  <symbol id="i-flip-h" viewBox="0 0 24 24"><path d="M12 3.4v17.2"/><path d="M9 6.6L4 12l5 5.4z"/><path d="M15 6.6l5 5.4-5 5.4z"/></symbol>
  <symbol id="i-flip-v" viewBox="0 0 24 24"><path d="M3.4 12h17.2"/><path d="M6.6 9L12 4l5.4 5z"/><path d="M6.6 15L12 20l5.4-5z"/></symbol>
  <symbol id="i-fit" viewBox="0 0 24 24"><rect x="3.4" y="5.4" width="17.2" height="13.2" rx="2"/><path d="M8.6 9.4h6.8v5.2H8.6z"/></symbol>
  <symbol id="i-resize" viewBox="0 0 24 24"><path d="M20.4 9.6v-6h-6"/><path d="M3.6 14.4v6h6"/><path d="M20.4 3.6l-7.2 7.2M3.6 20.4l7.2-7.2"/></symbol>
  <symbol id="i-coin" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.8"/><path d="M9.6 15.4h4.3a2.1 2.1 0 000-4.3h-3.8a2.1 2.1 0 010-4.3h4.3"/><path d="M12 5.2v13.6"/></symbol>
  <!-- FILLED status glyphs. These render at 11-13px in the sidebar footer, where a 1.7px stroke
       on a 24-grid scales to under a pixel and reads as a grey smudge. They live here so the
       sprite stays the single source of truth, but opt out of the stroke system via CSS
       (.wot-ico/.live-ico/.relay-ico/.stream-ico/.call-ico set fill:currentColor;stroke:none). -->
  <symbol id="i-wot" viewBox="0 0 24 24"><path d="M12 2l7 3v6c0 4.7-3.1 8.3-7 11-3.9-2.7-7-6.3-7-11V5l7-3z"/></symbol>
  <symbol id="i-livedot" viewBox="0 0 24 24"><circle cx="12" cy="12" r="6"/></symbol>
  <symbol id="i-relay-dot" viewBox="0 0 24 24"><circle cx="12" cy="12" r="2.6"/><path d="M6.3 6.3a8 8 0 000 11.4l1.5-1.5a6 6 0 010-8.5L6.3 6.3zm11.4 0l-1.5 1.4a6 6 0 010 8.5l1.5 1.5a8 8 0 000-11.4z"/></symbol>
  <symbol id="i-stream" viewBox="0 0 24 24"><path d="M4 5h13a2 2 0 012 2v3l3-2v8l-3-2v3a2 2 0 01-2 2H4a2 2 0 01-2-2V7a2 2 0 012-2z"/></symbol>
  <symbol id="i-call" viewBox="0 0 24 24"><path d="M6.6 10.8c1.4 2.8 3.8 5.1 6.6 6.6l2.2-2.2c.3-.3.7-.4 1-.2 1.1.4 2.3.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1C10.6 21 3 13.4 3 4c0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.2.2 2.4.6 3.6.1.4 0 .7-.2 1l-2.3 2.2z"/></symbol>
  <symbol id="i-quotes" viewBox="0 0 24 24"><path d="M9.6 5.8H5.2a1.2 1.2 0 00-1.2 1.2v4.4a1.2 1.2 0 001.2 1.2h4.4V7a1.2 1.2 0 00-1.2-1.2z"/><path d="M9.6 12.6c0 3-1.6 4.8-4.4 5.6"/><path d="M19.6 5.8h-4.4A1.2 1.2 0 0014 7v4.4a1.2 1.2 0 001.2 1.2h4.4V7a1.2 1.2 0 00-1.2-1.2z"/><path d="M19.6 12.6c0 3-1.6 4.8-4.4 5.6"/></symbol>
  <symbol id="i-close" viewBox="0 0 24 24"><path d="M6.4 6.4l11.2 11.2M17.6 6.4L6.4 17.6"/></symbol>
  <symbol id="i-smile" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.8"/><path d="M8.6 14.2a4.4 4.4 0 006.8 0"/><circle cx="9.3" cy="9.8" r=".95" fill="currentColor" stroke="none"/><circle cx="14.7" cy="9.8" r=".95" fill="currentColor" stroke="none"/></symbol>
</defs></svg>`;
  function inject(){
    var host = document.body || document.documentElement;
    host.insertAdjacentHTML('afterbegin', SPRITE);
  }
  inject();
  // ICO(name, cls) -> the same reference the static markup uses, for JS-rendered rows. Kept beside the
  // sprite so the helper can never load out of step with what it points at.
  window.ICO = function(n, cls){
    return '<svg class="ic' + (cls ? ' ' + cls : '') + '" aria-hidden="true"><use href="#i-' + n + '"></use></svg>';
  };
})();
