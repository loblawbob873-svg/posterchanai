"""`--fg` and `--border` are not tokens of this stylesheet, and reading them cost a start menu.

The PosterChanOS shell block wrote `color: var(--fg, #d6e2ff)` and `border: 1px solid
var(--border, #333)`. Neither property is declared in a single rule in client.css — the real tokens
are `--text` and `--line`, which every theme sets. Two silent shapes came out of that:

  * WITH the literal fallback, the colour is hard-coded past every theme. `.os-app` is ALSO the
    desktop start menu's app row, so on the five light themes the entries were pale blue on
    near-white — measured at 1.14-1.40:1 against the panel (Cherry Blossom 1.22, Professional 1.21,
    Windows 98 1.40, Windows XP 1.14, Anime Girl 1.17), i.e. a start menu that opens full of apps
    and reads as EMPTY. That is the "the start menu is blank" report.
  * WITHOUT one (13 of the 25 uses) the declaration is invalid at computed-value time and does
    nothing at all, so a dozen hover and emphasis rules were dead.

Neither shape shows up in a console, a screenshot on the default theme, or any existing check. So
the two names are simply banned: if one comes back, so has the bug.

The measured half is `scripts/check_os_theme_contrast.py`, which reads the real contrast off a real
browser for all nine themes. This file is the cheap floor that runs in every pytest pass.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHEETS = [os.path.join(ROOT, "static", "css", "client.css"),
          os.path.join(ROOT, "static", "css", "rtl.css")]
BANNED = ("--fg", "--border")


def _strip_comments(src):
    return re.sub(r"/\*.*?\*/", "", src, flags=re.S)


class PhantomCustomPropertyTests(unittest.TestCase):
    def test_the_two_phantom_tokens_stay_gone(self):
        bad = []
        for path in SHEETS:
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as fh:
                src = _strip_comments(fh.read())
            defined = set(re.findall(r"(--[A-Za-z0-9_-]+)\s*:", src))
            for name in BANNED:
                if name in defined:
                    continue          # if someone actually DEFINES it, it is no longer a phantom
                for m in re.finditer(r"var\(\s*" + re.escape(name) + r"\s*[,)]", src):
                    bad.append("%s:%d  %s" % (os.path.basename(path),
                                              src[:m.start()].count("\n") + 1, m.group(0)))
        self.assertFalse(bad, "client CSS reads a custom property it never defines:\n  "
                              + "\n  ".join(bad)
                              + "\nUse --text instead of --fg and --line instead of --border. A "
                                "literal fallback here hard-codes a dark-theme colour past every "
                                "theme (that is what made the start menu unreadable on the light "
                                "ones); no fallback makes the declaration invalid and silent.")


if __name__ == "__main__":
    unittest.main()
