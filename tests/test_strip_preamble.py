"""strip_preamble — enforce "output only the text" instead of merely asking for it.

Run: venv-unified/bin/python -m unittest tests.test_llm_text

Reported from use, pressing ✨ AI Enhancer: "Added nonsense to the beginning: Here's one that hits
the key points naturally:---". The endpoint's prompt already said not to. Six prompts in this
codebase say some version of not to. This is the enforcement.

The tests that matter most are the ones asserting what is LEFT ALONE — a cleaner that eats a user's
first line is worse than the preamble it removes.
"""

import unittest

from app.services.text_utils import strip_preamble


class TestStripsScaffolding(unittest.TestCase):
    def test_the_reported_one(self):
        out = strip_preamble(
            "Here's one that hits the key points naturally:\n"
            "---\n"
            "The new bill changes how rents are set.\n\nIt matters because…")
        self.assertTrue(out.startswith("The new bill"), out)
        self.assertNotIn("hits the key points", out)
        self.assertNotIn("---", out)

    def test_a_chain_of_two(self):
        out = strip_preamble("Sure thing:\nHere is the post:\nActual content here.")
        self.assertEqual(out, "Actual content here.")

    def test_a_whole_answer_in_a_fence(self):
        self.assertEqual(strip_preamble("```\nJust the post.\n```"), "Just the post.")
        self.assertEqual(strip_preamble("```markdown\nJust the post.\n```"), "Just the post.")

    def test_a_trailing_offer_to_revise(self):
        out = strip_preamble("The post itself.\n\n---\nLet me know if you'd like it shorter!")
        self.assertEqual(out, "The post itself.")

    def test_quotes_around_the_whole_thing(self):
        self.assertEqual(strip_preamble('"The whole post."'), "The whole post.")


class TestLeavesRealContentAlone(unittest.TestCase):
    """Each of these is a way the cleaner could destroy someone's actual writing."""

    def test_prose_that_merely_starts_like_a_lead_in(self):
        src = "Here's why the rent bill matters more than the headlines suggest.\n\nIt caps…"
        self.assertEqual(strip_preamble(src), src)

    def test_a_colon_line_that_is_not_a_lead_in(self):
        src = "Breaking:\nThe council voted 7-2 to approve it."
        self.assertEqual(strip_preamble(src), src)

    def test_a_long_first_line_ending_in_a_colon(self):
        src = ("Three things came out of last night's council meeting that nobody covering it seems "
               "to have noticed, and here they are:\nOne…")
        self.assertEqual(strip_preamble(src), src)

    def test_a_single_line_is_never_stripped(self):
        """Even when it reads exactly like scaffolding — if that is all there is, it is the answer,
        and returning an empty composer is a worse outcome than returning a bad line."""
        self.assertEqual(strip_preamble("Here's the post:"), "Here's the post:")

    def test_a_fence_inside_a_post_survives(self):
        src = "Try this:\n\n```\nnpm install\n```\n\nThat fixed it for me."
        self.assertEqual(strip_preamble(src), src)

    def test_a_rule_inside_the_body_survives(self):
        src = "First half.\n\n---\n\nSecond half."
        self.assertEqual(strip_preamble(src), src)

    def test_a_question_ending_is_not_an_offer(self):
        src = "The vote is Thursday.\n\nWhat do you make of it?"
        self.assertEqual(strip_preamble(src), src)

    def test_empty_and_none_are_survivable(self):
        self.assertEqual(strip_preamble(""), "")
        self.assertIsNone(strip_preamble(None))

    def test_it_never_returns_empty_for_non_empty_input(self):
        for src in ["Here's the post:", "---", "```\n```", '""', "Sure:\n---"]:
            with self.subTest(src=src):
                self.assertTrue(strip_preamble(src).strip(), "emptied %r" % src)


if __name__ == "__main__":
    unittest.main()
