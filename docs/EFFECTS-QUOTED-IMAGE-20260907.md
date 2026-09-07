# Effects on quoted images

The reported post `0000cd75c4d2107b8cb789dc9d8b5408ff0896572233b0a728ddd936ba3ce214`
contains a `q` reference to `ad1c9afe92bbd19446edf4e1785de24dd29ee9d91fdb2de2f60a1d04d6a53dea`.
The quoted post carries the displayed PNG; the outer post has no direct image.
Effects previously inspected only the outer event and incorrectly reported no image.

The action now prioritizes the selected post's own image, then follows its explicit
quote references through cached or fetched events. It never uses reply parents as
image sources. A visited set and an eight-event depth bound stop cyclic or excessive
chains. Generated effects retain the originally selected post and author as their
reply target. Existing AI access checks remain in the shared studio launcher.
Service-worker cache advances to 1682.

Validation:

- The new runtime test fails against the old code on the reported cached-quote case.
- All 15 actual-action scenarios pass with the fix: cached/fetched/nested quotes,
  direct/imeta/Blossom images, original reply identity, cycles/depth bounds, absent
  images, invalid references, relay failure and reply-parent exclusion.
- Those scenarios plus the 12 mobile/desktop emoji layout tests passed together
  (13 pytest cases in 8.76s).
- The reported PNG and the production studio proxy both return HTTP 200 and decode
  as 754×1200 PNG images (524032 bytes).
- Independent read-only review found no material blockers and confirmed all 15
  runtime scenarios. Syntax and whitespace checks pass.
- Full client validation is running; the unchanged backend already passed 7811 tests
  plus 519 subtests in the preceding release.
