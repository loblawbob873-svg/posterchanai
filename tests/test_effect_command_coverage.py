"""Every effect reaches every interface with its ATTACHMENT — no hand-copied name lists.

Run: venv-unified/bin/python -m unittest tests.test_effect_command_coverage

Renaming one effect (`anyways` → `lookingaway`) broke it in AI chat: the web UI decided which
commands get the upload's raw bytes from a literal list of 99 names that nobody updated, so the new
name rendered with NOTHING attached and answered "attach an image". The same literal was copied into
Telegram three more times, and those copies had already silently lost `goon` and `hag`.

So the assertions here are about COVERAGE, not about one effect: any name in the effect sets must be
attachment-gated, matchable on Telegram, and skipped by the pre-command OCR — whatever it is called
tomorrow. The alias cases are separate, because an alias is resolved at a different moment on each
interface (parse_command on the web, an explicit lookup on Telegram) and that is where it goes wrong.
"""
import unittest

from app.routers.telegram import messages as tg
from app.services.command_service import CommandService as CS

EFFECTS = set(CS.MOTION_EFFECTS) | set(CS.ANIMATED_EFFECTS)
# Old names that still have to work; each must resolve to a real effect.
EFFECT_ALIASES = {k: v for k, v in CS.COMMAND_ALIASES.items() if v in EFFECTS}


class TestAnimatedAreCompressed(unittest.TestCase):
    def test_animated_effects_are_also_motion_effects(self):
        """Auto-compress is keyed on MOTION_EFFECTS alone, while the outro end-card is keyed on
        MOTION_EFFECTS *or* ANIMATED_EFFECTS. So an effect added only to ANIMATED_EFFECTS gets the
        watermark but ships UNCOMPRESSED — a full-resolution video, silently. Nothing in the code
        says the two sets are related, so this is the thing that says it."""
        missing = sorted(set(CS.ANIMATED_EFFECTS) - set(CS.MOTION_EFFECTS))
        self.assertEqual(missing, [], f"animated but never compressed: {missing}")


class TestAttachmentGate(unittest.TestCase):
    def test_every_effect_is_handed_the_upload(self):
        missing = sorted(n for n in EFFECTS if not CS.wants_attachments(n))
        self.assertEqual(missing, [], "effects that would run with no image attached")

    def test_every_effect_alias_is_handed_the_upload(self):
        # The alias never reaches execute_command's resolve if the gate rejected it first.
        missing = sorted(n for n in EFFECT_ALIASES if not CS.wants_attachments(n))
        self.assertEqual(missing, [])

    def test_media_tools_are_gated_too(self):
        for name in ("compress", "clip", "convert", "translate", "ocr", "bill", "flashcards"):
            self.assertTrue(CS.wants_attachments(name), name)

    def test_plain_commands_are_not(self):
        # The gate decodes and copies every upload, so it must not fire on a chat command.
        for name in ("help", "search", "geni", "news", "node", "reminders", "", None):
            self.assertFalse(CS.wants_attachments(name), name)


class TestTelegramCoverage(unittest.TestCase):
    def test_every_effect_and_alias_is_matchable(self):
        # Telegram matches command words literally — a word that is not in this list falls through
        # to the LLM, which just chats about the effect instead of rendering it.
        words = set(tg._TG_COMMANDS)
        self.assertEqual(sorted(EFFECTS - words), [])
        self.assertEqual(sorted(set(EFFECT_ALIASES) - words), [])

    def test_first_match_wins_is_unambiguous(self):
        # The matcher breaks on the first hit, so a word may not be another's leading token.
        clashes = [(a, b) for a in tg._TG_COMMANDS for b in tg._TG_COMMANDS
                   if a != b and b.startswith(a + " ")]
        self.assertEqual(clashes, [])

    def test_effects_skip_the_pre_command_ocr(self):
        # Effects work on the file, never on text read out of it: OCR here is pure upload latency.
        self.assertEqual(sorted(EFFECTS - tg._TG_RAW_MEDIA_COMMANDS), [])


class TestAliasResolution(unittest.TestCase):
    def test_aliases_point_at_real_effects(self):
        for alias, target in EFFECT_ALIASES.items():
            self.assertIn(target, EFFECTS, alias)
            self.assertNotIn(alias, EFFECTS, f"{alias} is both an alias and an effect")

    def test_syslogs_reaches_the_health_report_instead_of_the_model(self):
        """An UNKNOWN command falls through to the LLM, which invents an answer. `syslogs` did, and
        the model replied with fabricated entries ("User 'Poster-Chan' connected from IP
        192.168.1.105 (New York, NY)") that read exactly like real output for a host that logged no
        such thing. A command the model cannot serve must never reach the model."""
        parse = CS.parse_command
        for word in ("syslogs", "syslog", "healthreport"):
            self.assertEqual(parse(CS.__new__(CS), word), ("logs", ""), word)
            self.assertEqual(parse(CS.__new__(CS), f"{word} nas"), ("logs", "nas"), word)
            self.assertIn(word, tg._TG_COMMANDS, f"{word} must be matchable on Telegram too")

    def test_health_alone_is_not_a_command(self):
        """Matching is bare-word-or-word-plus-space, so a `health` alias would answer "health check
        on the server" with a status board instead of a reply."""
        self.assertEqual(CS.parse_command(CS.__new__(CS), "health check on the server")[0], None)

    def test_anyways_still_reaches_the_monkey_puppet(self):
        # The one people have typed for months, named explicitly so a future rename can't quietly
        # drop it: the alias may point somewhere else, but it may not stop resolving.
        self.assertEqual(CS.COMMAND_ALIASES.get("anyways"), "lookingaway")
        self.assertTrue(CS.wants_attachments("anyways"))


class TestTelegramEffectKeyboards(unittest.TestCase):
    """The Effects keyboards are the OTHER hand-written name list, and the one a user actually taps.

    Each button fires `media:zq:<name>`, which is handed straight to execute_command — so a typo or a
    renamed effect makes the button answer with a chat reply from the LLM instead of rendering, and
    nothing in the keyboard code would notice. Sizes are asserted too: Telegram silently rejects a
    callback_data over 64 bytes, and the layout is rows of 2 by construction.
    """

    def _entries(self):
        from app.routers.telegram._common import _FX_CHARACTERS, _FX_MEMES, _FX_SOUNDS, _FX_THEMES
        return {"themes": _FX_THEMES, "sounds": _FX_SOUNDS, "memes": _FX_MEMES,
                "characters": _FX_CHARACTERS}

    def test_every_button_runs_a_real_effect(self):
        for cat, entries in self._entries().items():
            for label, name in entries:
                resolved = CS.COMMAND_ALIASES.get(name, name)
                self.assertIn(resolved, EFFECTS, f"{cat}: '{label}' -> {name} is not an effect")
                self.assertTrue(CS.wants_attachments(name),
                                f"{cat}: '{label}' -> {name} would render with no image")

    def test_no_effect_is_offered_in_two_categories(self):
        seen: dict = {}
        for cat, entries in self._entries().items():
            for _label, name in entries:
                self.assertNotIn(name, seen, f"{name} is in both {seen.get(name)} and {cat}")
                seen[name] = cat

    def test_callback_data_fits_telegrams_limit(self):
        for cat, entries in self._entries().items():
            for _label, name in entries:
                self.assertLessEqual(len(f"media:zq:{name}".encode()), 64, f"{cat}: {name}")

    def test_character_keyboards_only_offer_installed_art(self):
        """Both character keyboards are filtered by _character_path, because a name whose PNG is
        missing renders an error from the effects list — and, in the `char <name>` picker, is not
        consumed by the arg parser at all, so the token leaks into the effect's own argument."""
        from app.routers.telegram.keyboards import (_character_prompt_keyboard,
                                                    _media_fx_characters_keyboard)
        from app.services.effects_service.character import _character_path
        for kb in (_media_fx_characters_keyboard(), _character_prompt_keyboard()):
            for row in kb["inline_keyboard"]:
                self.assertLessEqual(len(row), 2, "keyboards are rows of 2")
                for btn in row:
                    name = btn["callback_data"].rsplit(":", 1)[1]
                    if name in ("none", "effects"):     # Back / No character
                        continue
                    self.assertTrue(_character_path(name), f"{name}: art missing but offered")


if __name__ == "__main__":
    unittest.main()
