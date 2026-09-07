# Emoji pack picker visibility — 2026-09-07

Pack tabs now wrap on narrow screens. A bounded, visibly scrollable tab area keeps larger pack collections discoverable without displacing the emoji grid. The picker stays within the viewport when switching from a short Recent list to a full pack or searching across packs. Placement continues to use the original message anchor and existing responsive sheet behavior. Service worker cache advances from 1680 to 1681.

Validation:

- Original CSS failed the real-browser regression: the 320px picker used non-wrapping tabs and a hidden horizontal scrollbar.
- CSS-only candidate passed 9/12 cases; three anchored cases exposed viewport overflow after selecting a larger pack. Central placement after replacement rendering fixes both tab and search growth.
- Final actual Chrome matrix: **12 passed in 9.26s**, `/tmp/pc-emoji-picker-touch-green.log`. Covers 320/375/1280px, anchored message and responsive sheet contexts, 2 and 12 custom packs, Recent/Emoji/DRC_emojo/Monero-XMR, search growth and clearing, loaded XMR images, and selection callbacks. Mobile cases use trusted CDP touch events; desktop uses trusted pointer events. Assertions reject obscured targets and off-viewport popovers.
- Existing Concord social emoji, shared reaction picker, custom reaction wire and UI tests: **68 passed in 1.82s**.
- JavaScript syntax and `git diff --check` passed.
- Visual review of 320px anchored and 375px sheet screenshots confirmed visible Monero labels, bounded tab scrolling and usable emoji grids. Screenshots from the first passing matrix are retained in `/tmp/pc-emoji-picker-screenshots/`.

The browser harness runs the actual picker/placement/button functions and complete stylesheet. Pack contents are deterministic fixtures with 24 local XMR SVG images per pack; it does not modify installed packs or test public image hosting. Mobile coverage is browser viewport/touch emulation, not a physical-device claim. No signer, transport or pack-management logic changes are included.
