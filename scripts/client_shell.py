"""Render the same checked-out client shell for Android and desktop, without network access."""
import os
import re
import subprocess
import sys


def render(source_root, ver='0'):
    src = str(source_root)
    html = open(os.path.join(src, 'templates', 'client.html'), encoding='utf-8').read()

    # ---- render the four template values ------------------------------------------------------------
    # `nostr_only` is FALSE here on purpose: false is the branch that KEEPS every nav item in the markup,
    # which is what lets applyInstanceGating() hide or restore them per instance at runtime. Baking true
    # would delete the buttons from the bundle and no instance could ever bring them back.
    html = re.sub(r'\{%\s*if not nostr_only\s*%\}(.*?)\{%\s*endif\s*%\}', r'\1', html, flags=re.S)
    html = re.sub(r'\{%\s*if nostr_only\s*%\}.*?\{%\s*endif\s*%\}', '', html, flags=re.S)
    # `secure` gates the upgrade-insecure-requests CSP. Always drop it: the page is served from a secure
    # app:// origin, but the INSTANCE may legitimately be cleartext (an .onion is plain HTTP by design, and
    # so is a LAN box), and that CSP would silently rewrite every fetch/WebSocket/<img> to https://<host>,
    # which does not exist. Same reasoning as the APK's strip, which learned it the hard way.
    html = re.sub(r'\{%\s*if secure\s*%\}.*?\{%\s*endif\s*%\}', '', html, flags=re.S)
    # `meta` is the server-rendered LINK PREVIEW card (og:/twitter: tags), and a bundle has no use for
    # one: this page is loaded off disk from an app:// origin, so nothing ever crawls it and there is no
    # URL for a card to describe. Dropped whole, and the <title> falls back to the app's own name — the
    # same value the server sends for every route that supplies no card.
    #
    # NESTING-AWARE, unlike the two `re.sub` strips above. Those are correct only because their blocks
    # contain no inner `{% if %}`; a non-greedy `.*?` stops at the FIRST `{% endif %}`, so on a block
    # that nests (this one does — og:image is emitted only when there IS one) it would delete the head
    # and leave the tail plus a dangling `{% endif %}` behind. The guard below would then fail the build,
    # which is the right outcome and a confusing way to learn it.
    def _drop_block(src, cond):
        open_re = re.compile(r'\{%\s*if\s+' + re.escape(cond) + r'\s*%\}')
        any_if = re.compile(r'\{%\s*if\b')
        any_end = re.compile(r'\{%\s*endif\s*%\}')
        while True:
            m = open_re.search(src)
            if not m:
                return src
            i, depth = m.end(), 1
            while depth:
                nxt_if, nxt_end = any_if.search(src, i), any_end.search(src, i)
                if not nxt_end:
                    raise SystemExit('build-www: unclosed {%% if %s %%} in client.html' % cond)
                if nxt_if and nxt_if.start() < nxt_end.start():
                    depth += 1
                    i = nxt_if.end()
                else:
                    depth -= 1
                    i = nxt_end.end()
            src = src[:m.start()] + src[i:]


    html = _drop_block(html, 'meta')
    # Jinja COMMENTS are template syntax too, and the guard below does not look for them — so one would
    # ship into the bundle as literal text, land in <head>, and be relocated into the page body by the
    # parser where a reader can see it. Cheap to strip, invisible when it works, ugly when it does not.
    html = re.sub(r'\{#.*?#\}', '', html, flags=re.S)
    html = html.replace('{{ meta.title if meta else "PosterChan · Nostr" }}', 'PosterChan · Nostr')
    html = html.replace('{{ default_theme|default("cyberpunk") }}', 'cyberpunk')
    html = html.replace('{{ ver }}', ver)
    # The build stamp. Substituted with the CHECKOUT's own commit rather than left for the stamping step
    # below, because this file renders client.html locally and then hard-fails on any tag it does not
    # know — so a new `{{ … }}` in the template breaks every desktop platform at once until it is listed
    # here. That is exactly what happened: three builds, five retries each, all red, and no new Windows
    # app for hours while the Android one sailed through (mobile FETCHES a rendered page, so it never
    # sees a raw tag). If a template tag is ever added again, this is the line it also has to reach.
    try:
        _sha = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True,
                              timeout=5, cwd=src).stdout.strip() or 'unknown'
    except Exception:
        _sha = 'unknown'
    html = html.replace('{{ build }}', _sha)
    html = html.replace("{{ 'true' if nostr_only else 'false' }}", 'false')
    # A BUNDLE CANNOT KNOW WHETHER SIGNUP IS OPEN, because it has not chosen an instance yet — one
    # bundle serves every one of them. So this resolves to the OPEN branch (the template's own
    # `default(true)`) and the real answer arrives at runtime from /client/config, exactly like
    # `nostr_only` above. Baking a closed signup here would hide the button permanently, on the one
    # screen a person with no account has.
    html = re.sub(r"\{%\s*if not registration_enabled\|default\(true\)\s*%\}.*?\{%\s*endif\s*%\}",
                  '', html, flags=re.S)
    left = re.search(r'\{\{.*?\}\}|\{%.*?%\}', html, flags=re.S)
    if left:
        raise SystemExit('build-www: unrendered template tag in client.html: ' + left.group(0)[:80])

    return html


if __name__ == '__main__':
    print(render(sys.argv[1], os.environ.get('PC_VER', '0')), end='')
